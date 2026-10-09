"""验证云端认证和合成追踪，不调用真实模型、业务数据库或供应商。"""

import argparse
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import sys
import time
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from business_travel.memory_pg import MemoryIdentity
from business_travel.observability import Telemetry, configuration_problem, observation, record_event
from business_travel.settings import Settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--poll-attempts", type=int, default=10)
    parser.add_argument("--poll-interval", type=float, default=3)
    parser.add_argument("--capture-content", action="store_true",
                        help="用合成正文验证脱敏内容模式；默认仍验证纯元数据模式")
    args = parser.parse_args()
    if not 1 <= args.poll_attempts <= 20 or not 1 <= args.poll_interval <= 10:
        parser.error("轮询次数须为1至20，间隔须为1至10秒")
    # Do not print SDK HTTP bodies, credential URLs, or raw cloud responses.
    logging.disable(logging.CRITICAL)
    settings = replace(Settings.from_env(), langfuse_capture_content=args.capture_content)
    report = {"enabled": settings.langfuse_enabled, "auth_verified": False,
              "cloud_received": False, "synthetic_only": True, "capture_content": args.capture_content}
    problem = configuration_problem(settings)
    if problem or not settings.langfuse_enabled:
        report["configuration_problem"] = problem or "观测未开启"
        print(json.dumps(report, ensure_ascii=False))
        return 2
    service = Telemetry(settings)
    stage = "initialize"
    try:
        client = service._client()
        if client is None:
            raise RuntimeError("client unavailable")
        stage = "authenticate"
        report["auth_verified"] = bool(client.api.projects.get(
            request_options={"timeout_in_seconds": 15, "max_retries": 0}).data)
        if not report["auth_verified"]:
            raise RuntimeError("no project")
        print(json.dumps({"auth_verified": True}), flush=True)
        stage = "emit"
        from langchain_core.language_models.fake_chat_models import FakeListChatModel
        from langchain_core.messages import HumanMessage
        from langgraph.graph import END, START, MessagesState, StateGraph

        marker = "synthetic-private-" + uuid4().hex
        identity = MemoryIdentity("synthetic-company", marker)
        started = datetime.now(timezone.utc) - timedelta(minutes=1)
        model = FakeListChatModel(responses=["synthetic answer" if args.capture_content else marker])
        prompt = (json.dumps({"question": "synthetic question", "password": marker})
                  if args.capture_content else marker)

        def node(state):
            with observation("slot_extraction"):
                answer = model.invoke(state["messages"])
                if args.capture_content:
                    with observation("query_train", kind="tool") as tool_span:
                        tool_span.content(input={"city": "synthetic city", "api_key": marker},
                                          output={"result": "synthetic result"})
                record_event("slot_commit", status="clarify", slots_committed=False)
                return {"messages": [answer]}

        builder = StateGraph(MessagesState)
        builder.add_node("prepare", node)
        builder.add_edge(START, "prepare")
        builder.add_edge("prepare", END)
        graph = builder.compile()
        with service.request({}, identity, marker, uuid4().hex) as (root, config):
            if root.raw is None:
                raise RuntimeError("trace unavailable")
            trace_id = root.raw.trace_id
            root.content(input=prompt)
            result = graph.invoke({"messages": [HumanMessage(prompt)]}, config)
            root.content(output=result["messages"][-1])
            root.update(execution_status="completed", checkpoint_committed=False)
        client.flush()
        report["trace_id"] = trace_id
        stage = "read_back"
        expected = {"chat", "slot_extraction", "slot_commit", "chat_model"}
        if args.capture_content:
            expected.add("query_train")
        rows = []
        for attempt in range(args.poll_attempts):
            response = client.api.observations.get_many(
                trace_id=trace_id, from_start_time=started,
                to_start_time=datetime.now(timezone.utc) + timedelta(minutes=1),
                fields="core,basic,io,metadata", limit=100,
                request_options={"timeout_in_seconds": 15, "max_retries": 0})
            rows = response.data
            if expected <= {row.name for row in rows} and all(row.end_time for row in rows):
                break
            if attempt + 1 < args.poll_attempts:
                time.sleep(args.poll_interval)
        by_name = {row.name: row for row in rows}
        report["observation_count"] = len(rows)
        report["cloud_received"] = expected <= set(by_name)
        report["all_ended"] = bool(rows) and all(row.end_time is not None for row in rows)
        payload = json.dumps([row.model_dump(mode="json") for row in rows])
        report["no_private_payload"] = not any(value in payload for value in (
            marker, identity.company_id, settings.langfuse_secret_key, settings.langfuse_hash_key))
        report["no_input_output"] = bool(rows) and all(
            getattr(row, field, None) in (None, "", "null", "{}", {})
            for row in rows for field in ("input", "output"))
        report["nested_model"] = bool(report["cloud_received"] and
            by_name["chat_model"].parent_observation_id == by_name["slot_extraction"].id)
        checks = ("auth_verified", "cloud_received", "all_ended", "no_private_payload",
                  "no_input_output", "nested_model")
        if args.capture_content:
            report["redacted_content_received"] = report["cloud_received"] and all(
                text in json.dumps(getattr(by_name[name], field, None))
                for name, field, text in (
                    ("chat_model", "input", "synthetic question"),
                    ("chat_model", "input", "[REDACTED]"),
                    ("chat_model", "output", "synthetic answer"),
                    ("query_train", "input", "synthetic city"),
                    ("query_train", "output", "synthetic result"),
                    ("chat", "output", "synthetic answer")))
            checks = tuple(key for key in checks if key != "no_input_output") + ("redacted_content_received",)
        report["passed"] = all(report[key] for key in checks)
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report["passed"] else 1
    except Exception as error:
        report.update(passed=False, failed_stage=stage, error_type=type(error).__name__)
        status = getattr(error, "status_code", None)
        if type(status) is int and 400 <= status <= 599:
            report["http_status"] = status
        print(json.dumps(report, ensure_ascii=False))
        return 1
    finally:
        service.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())

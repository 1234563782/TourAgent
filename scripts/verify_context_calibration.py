"""真实模型与Langfuse用量校准验收；仅合成输入，不访问业务数据库或旅游供应商。"""

import argparse
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import sys
import time
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langchain_core.messages import HumanMessage

from business_travel.context_budget import BudgetedModel, request_size, usage_calibration
from business_travel.memory_pg import MemoryIdentity
from business_travel.models import create_model, model_error_details
from business_travel.observability import Telemetry, configuration_problem
from business_travel.settings import Settings


def verify_rows(rows, expected, model_ref):
    calls = [r for r in rows if r.name == "context_budget"
             and (r.metadata or {}).get("budget_stage") == "invoke"]
    models = [r for r in rows if r.name == "chat_model"]
    checks = {"two_calls": len(calls) == len(expected),
              "two_generations": len(models) == len(expected),
              "all_ended": bool(rows) and all(r.end_time is not None for r in rows),
              "no_input_output": bool(rows) and all(
                  getattr(r, key, None) in (None, "", "null", "{}", {})
                  for r in rows for key in ("input", "output"))}
    refs = [(r.metadata or {}).get("call_ref") for r in calls]
    checks["unique_refs"] = len(refs) == len(set(refs)) == len(expected) and all(
        isinstance(value, str) and len(value) == 64 for value in refs)
    comparisons = []
    for item in expected:
        matched = [r for r in calls if (r.metadata or {}).get("context_role") == item["role"]]
        if len(matched) != 1:
            comparisons.append(False)
            continue
        call = matched[0]
        metadata = call.metadata or {}
        children = [r for r in models if r.parent_observation_id == call.id]
        comparisons.append(
            len(children) == 1 and (children[0].metadata or {}).get("input_tokens") == item["input_tokens"]
            and metadata.get("model_ref") == model_ref
            and metadata.get("budget_decision") == "allowed"
            and metadata.get("usage_status") == "available"
            and metadata.get("status") == "ok"
            and all(metadata.get(key) == item[key] for key in (
                "estimated_input_tokens", "input_tokens", "input_estimate_delta"))
            and abs(metadata.get("input_estimate_ratio", -1) - item["input_estimate_ratio"]) < 1e-9)
    checks["exact_usage_and_parent"] = len(comparisons) == len(expected) and all(comparisons)
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--poll-attempts", type=int, default=10)
    parser.add_argument("--poll-interval", type=float, default=3)
    args = parser.parse_args()
    if args.output.exists() or not 1 <= args.poll_attempts <= 20 or not 1 <= args.poll_interval <= 10:
        parser.error("输出文件须不存在；轮询次数1至20，间隔1至10秒。")
    logging.disable(logging.CRITICAL)
    settings = Settings.from_env()
    if settings.missing_model_config() or not settings.langfuse_enabled or configuration_problem(settings):
        print("模型或观测配置不完整，请检查.env；未调用外部服务。")
        return 2
    report = {"scope": "合成中英文输入，两次真实模型调用及云端回读；不代表长期偏差分布",
              "passed": False, "calls": [], "checks": {}}
    service = Telemetry(settings)
    stage = "authenticate"
    try:
        client = service._client()
        if client is None or not client.api.projects.get(
                request_options={"timeout_in_seconds": 15, "max_retries": 0}).data:
            raise RuntimeError("telemetry unavailable")
        stage = "model_calls"
        marker = "synthetic-private-" + uuid4().hex
        started = datetime.now(timezone.utc) - timedelta(minutes=1)
        # Limit acceptance output only; production configuration remains unchanged.
        raw = create_model(replace(settings, context_output_tokens=256))
        tool = {"type": "function", "function": {
            "name": "synthetic_record", "description": "Synthetic schema for usage accounting only.",
            "parameters": {"type": "object", "properties": {"value": {"type": "string"}},
                           "required": ["value"]}}}
        cases = [
            ("supervisor", f"合成测试标记：{marker}。请仅回复收到。"),
            ("planner", f"Synthetic test {marker}. Do not call tools. Reply only OK. "
             '{"city":"Example","dates":["2099-01-01","2099-01-02"],"budget":123}'),
        ]
        with service.request({}, MemoryIdentity("synthetic-company", marker), marker, uuid4().hex) as (root, config):
            if root.raw is None:
                raise RuntimeError("trace unavailable")
            report["trace_id"] = root.raw.trace_id
            for role, text in cases:
                model = BudgetedModel(raw, settings, role)
                if role == "planner":
                    model = model.bind_tools([tool], tool_choice="none")
                messages = [HumanMessage(text)]
                estimate = request_size(messages, model.tools)
                response = model.invoke(messages, config=config)
                usage = usage_calibration(response, estimate)
                report["calls"].append({"role": role, "estimated_input_tokens": estimate, **usage})
                if usage["usage_status"] != "available" or usage.get("input_tokens", 0) <= 0:
                    raise RuntimeError("input usage unavailable for calibration")
            root.update(execution_status="completed", checkpoint_committed=False)
        client.flush()
        stage = "cloud_readback"
        rows = []
        for attempt in range(args.poll_attempts):
            rows = client.api.observations.get_many(
                trace_id=report["trace_id"], from_start_time=started,
                to_start_time=datetime.now(timezone.utc) + timedelta(minutes=1),
                fields="core,basic,io,metadata", limit=100,
                request_options={"timeout_in_seconds": 15, "max_retries": 0}).data
            report["checks"] = verify_rows(rows, report["calls"], service.reference("model", settings.model))
            if all(report["checks"].values()):
                break
            if attempt + 1 < args.poll_attempts:
                time.sleep(args.poll_interval)
        payload = json.dumps([r.model_dump(mode="json") for r in rows])
        secrets = [marker, settings.api_key, settings.langfuse_secret_key, settings.langfuse_hash_key]
        report["checks"]["no_private_payload"] = not any(value in payload for value in secrets if value)
        report["observation_count"] = len(rows)
        report["passed"] = all(report["checks"].values())
    except Exception as error:
        report.update(failed_stage=stage, error=model_error_details(error))
    finally:
        service.shutdown()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

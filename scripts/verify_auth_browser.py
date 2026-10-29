"""隔离数据库与随机账号的浏览器验收；聊天和任务控制模式调用真实模型。"""

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import traceback
import uuid
from dataclasses import replace
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uvicorn
from psycopg import sql
from langchain_core.language_models.chat_models import BaseChatModel

from business_travel.auth import AuthSettings, AuthStore, setup_auth
from business_travel.memory_pg import memory_connection, setup_memory
from business_travel.settings import Settings
from business_travel.web import create_app


def record_event(events, output, event):
    events.append(deepcopy(event))
    output.write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")


class InjectedOutage(BaseChatModel):
    """Fail inside the standard model lifecycle, preserving inherited callbacks."""

    @property
    def _llm_type(self):
        return "acceptance-outage"

    def bind_tools(self, *values, **options):
        return self

    def _generate(self, *values, **options):
        raise ConnectionError("Synthetic acceptance outage")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", required=True)
    parser.add_argument("--node-modules", required=True)
    parser.add_argument("--browser", required=True)
    parser.add_argument("--chat", action="store_true", help="验收真实模型聊天，仅写临时账号")
    parser.add_argument("--conversations", action="store_true", help="验收多会话，使用隔离测试图")
    parser.add_argument("--run-recovery", action="store_true", help="隔离图及数据库验收失败执行核实分叉")
    parser.add_argument("--resources", action="store_true", help="隔离图及真实数据库验收配额拒绝与前端重试状态")
    parser.add_argument("--task-controls", action="store_true", help="真实模型验收缺字段任务的继续和取消")
    parser.add_argument("--policy-rag", action="store_true", help="只读真实政策，隔离账号验收混合检索问答")
    parser.add_argument("--university-policy", action="store_true", help="验收高校政策、附表及多轮追问")
    parser.add_argument("--hotel-flow", action="store_true", help="真实酒店查询、条件更正及政策核验闭环")
    parser.add_argument("--failed-turn-flow", action="store_true", help="隔离服务注入故障，真实模型验收更正恢复")
    parser.add_argument("--failure-stage", choices=("slots", "reviewer"), default="reviewer",
                        help="仅故障恢复验收使用的模型故障位置")
    parser.add_argument("--langfuse", action="store_true", help="回读本批真实请求的匿名云端追踪")
    args = parser.parse_args()
    if sum((args.chat, args.conversations, args.task_controls, args.policy_rag,
            args.hotel_flow, args.failed_turn_flow, args.run_recovery, args.resources,
            args.university_policy)) > 1:
        parser.error("一次只能选择一种验收模式")
    if args.university_policy:
        args.policy_rag = True
    root = Path(__file__).resolve().parent.parent
    output = root / ".verification" / (
        ("university-browser-" if args.university_policy else
         "resources-browser-" if args.resources else "run-recovery-browser-" if args.run_recovery else
         "failed-turn-browser-" if args.failed_turn_flow else "hotel-flow-browser-" if args.hotel_flow else
         "policy-rag-browser-" if args.policy_rag else "task-controls-browser-" if args.task_controls else
         "conversations-browser-" if args.conversations else
         "chat-browser-" if args.chat else "auth-browser-") + uuid.uuid4().hex
        if args.chat or args.conversations or args.task_controls or args.policy_rag
        or args.hotel_flow or args.failed_turn_flow or args.run_recovery or args.resources else "auth")
    output.mkdir(parents=True, exist_ok=True)
    schema = "travel_auth_browser_" + uuid.uuid4().hex
    configured = Settings.from_env()
    if args.university_policy:
        manifest = json.loads((root / "policies/hfut-2026/manifest.json").read_text(encoding="utf-8"))
        configured = replace(configured, policy_key=manifest["policy_key"])
    if args.langfuse:
        from business_travel.observability import Telemetry, configuration_problem
        if not configured.langfuse_enabled or configuration_problem(configured):
            parser.error("Langfuse 未开启或配置不完整，请检查观测配置")
    # This historical acceptance checks metadata privacy, not content export.
    settings = replace(configured, memory_schema=schema, langfuse_capture_content=False)
    if args.resources:
        settings = replace(settings, resource_user_daily_requests=1)
    server = None
    thread = None
    listener = socket.socket()
    patches = ExitStack()
    traces = []
    try:
        if args.langfuse:
            original_request = Telemetry.request

            @contextmanager
            def observed_request(service, graph_config, identity, session, request_id):
                started = datetime.now(timezone.utc)
                with original_request(service, graph_config, identity, session, request_id) as pair:
                    root_observation, _ = pair
                    traces.append((
                        service.client,
                        root_observation.raw.trace_id if root_observation.raw else None,
                        root_observation.raw.id if root_observation.raw else None,
                        started,
                        tuple(value for value in (
                            identity.user_id, session, request_id,
                            configured.langfuse_secret_key, configured.langfuse_hash_key,
                        ) if value and len(value) >= 8),
                    ))
                    yield pair

            patches.enter_context(patch.object(Telemetry, "request", new=observed_request))
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        config = AuthSettings(origin=origin)
        setup_memory(settings)
        setup_auth(settings)
        password = secrets.token_urlsafe(24)
        store = AuthStore(settings, config)
        store.create_user("browser_test", password)
        if args.run_recovery:
            from langchain_core.messages import AIMessage, HumanMessage
            from langgraph.graph import StateGraph, START, END
            from business_travel.graph import AgentState

            execution_count = 0

            def recovery_fixture(factory, graph_settings, memory, policy, *, checkpointer, **options):
                def respond(state):
                    nonlocal execution_count
                    execution_count += 1
                    text = next(m.content for m in reversed(state["messages"]) if isinstance(m, HumanMessage))
                    if execution_count == 2:
                        memory.save_trip({"id": uuid.uuid4().hex, "note": "合成故障前已保存的草案"})
                        raise RuntimeError("Synthetic failure after persistence")
                    return {"messages": [AIMessage("合成回答：" + text)]}
                builder = StateGraph(AgentState)
                builder.add_node("respond", respond)
                builder.add_node("finish", lambda state: {})
                builder.add_edge(START, "respond")
                builder.add_edge("respond", "finish")
                builder.add_edge("finish", END)
                return builder.compile(checkpointer=checkpointer)

            patches.enter_context(patch("business_travel.web.build_assistant", side_effect=recovery_fixture))
        if args.conversations or args.resources:
            from tests.test_conversations_live import fixture_graph
            store.create_user("browser_other", password)
            patches.enter_context(patch("business_travel.web.build_assistant",
                side_effect=lambda *a, **kw: fixture_graph(kw["checkpointer"])))
        if args.failed_turn_flow:
            from business_travel.graph import build_assistant
            from business_travel.policy_pg import PostgresPolicyStore
            from langchain_core.messages import HumanMessage

            failures = []
            checkpoints = []
            checks = []
            turn_number = 0

            def build_with_failure(factory, *values, **options):
                def wrapped_factory(role):
                    nonlocal turn_number
                    if role == "slots":
                        turn_number += 1
                    if role == args.failure_stage and turn_number in (2, 5):
                        failures.append({"turn": turn_number, "stage": role})
                        record = {"failures": failures, "checkpoints": checkpoints, "checks": checks}
                        (output / "recovery-events.json").write_text(
                            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
                        return InjectedOutage()
                    return factory(role)
                return build_assistant(wrapped_factory, *values, **options)

            # Observe checkpoint publication, not a guessed browser rendering.
            from business_travel.memory_pg import ScopedPostgresSaver
            original_put = ScopedPostgresSaver.put
            original_policy_check = PostgresPolicyStore.check

            def observed_put(instance, config, checkpoint, metadata, new_versions):
                result = original_put(instance, config, checkpoint, metadata, new_versions)
                values = checkpoint.get("channel_values", {})
                if "slots" in values or "pending_turns" in values:
                    snapshot = deepcopy({
                        "turn": len([m for m in values.get("messages", []) if isinstance(m, HumanMessage)]),
                        "slots": values.get("slots", {}), "tasks": values.get("tasks", {}),
                        "pending_turns": values.get("pending_turns", []),
                        "slot_error": values.get("slot_error"),
                    })
                    checkpoints.append(snapshot)
                    (output / "recovery-events.json").write_text(json.dumps(
                        {"failures": failures, "checkpoints": checkpoints, "checks": checks},
                        ensure_ascii=False, indent=2), encoding="utf-8")
                return result

            def observed_policy_check(instance, facts):
                result = original_policy_check(instance, facts)
                checks.append({"turn": turn_number, "facts": facts.model_dump(mode="json")})
                return result

            patches.enter_context(patch("business_travel.web.build_assistant", side_effect=build_with_failure))
            patches.enter_context(patch.object(ScopedPostgresSaver, "put", new=observed_put))
            patches.enter_context(patch.object(PostgresPolicyStore, "check", new=observed_policy_check))
        if args.task_controls or args.policy_rag or args.hotel_flow or args.failed_turn_flow:
            from business_travel.slots import extract_slots
            diagnostics = []

            def observed_extraction(*values, **options):
                result = extract_slots(*values, **options)
                diagnostics.append(deepcopy({key: result[key] for key in
                                    ("status", "reason", "error_category", "http_status", "validation_errors", "tasks")
                                    if key in result}))
                (output / "extraction.json").write_text(
                    json.dumps(diagnostics, ensure_ascii=False, indent=2), encoding="utf-8")
                return result

            patches.enter_context(patch("business_travel.graph.extract_slots",
                                       side_effect=observed_extraction))
        if args.hotel_flow:
            from business_travel import context_budget
            from business_travel.graph import _reply
            from business_travel.task_results import read_task_result
            from business_travel.planning import run_planner
            from business_travel.policy_pg import PostgresPolicyStore
            from business_travel.tuniu import TuniuProvider
            from business_travel.providers import FlyAIProvider

            events = []
            budgets = []
            original_budget = context_budget.require_budget

            def observed_budget(messages, tools, config, role, **options):
                record = {
                    "role": role,
                    "estimated": context_budget.request_size(messages, tools),
                    "limit": context_budget.input_limit(config, role),
                    "tools": context_budget.encoded_size(list(tools)),
                    "messages": [context_budget.encoded_size(context_budget.message_data(m))
                                 for m in messages],
                }
                budgets.append(record)
                (output / "context-budget.json").write_text(
                    json.dumps(budgets, indent=2), encoding="utf-8")
                return original_budget(messages, tools, config, role, **options)

            patches.enter_context(patch.object(context_budget, "require_budget", new=observed_budget))
            original_history = context_budget.select_history

            def observed_history(messages, config, available, **options):
                try:
                    return original_history(messages, config, available, **options)
                except context_budget.ContextBudgetExceeded as error:
                    budgets.append({"stage": "history_selection", "role": error.role,
                                    "estimated": error.estimated, "limit": error.limit})
                    (output / "context-budget.json").write_text(
                        json.dumps(budgets, indent=2), encoding="utf-8")
                    raise

            patches.enter_context(patch.object(context_budget, "select_history", new=observed_history))

            def record_hotel_event(event):
                record_event(events, output / "hotel-events.json", event)

            def observed_search(original):
                def search(instance, kind, trip, inbound=False):
                    event = {"stage": "search", "kind": kind, "inbound": inbound,
                             "request": trip.model_dump(mode="json")}
                    try:
                        result = original(instance, kind, trip, inbound)
                        event["result"] = result.model_dump(mode="json")
                        return result
                    except Exception as error:
                        event["error_category"] = type(error).__name__
                        raise
                    finally:
                        record_hotel_event(event)
                return search

            for provider_class in (TuniuProvider, FlyAIProvider):
                patches.enter_context(patch.object(
                    provider_class, "search", new=observed_search(provider_class.search)))

            def observed_plan(*values, **options):
                result = run_planner(*values, **options)
                record_hotel_event({"stage": "plan", "result": result})
                return result

            def observed_result(state, task_id, expected_revision):
                result = read_task_result(state, task_id, expected_revision)
                task = state.get("tasks", {}).get(task_id) or {}
                subject = state.get("slots", {}).get("subjects", {}).get(task.get("subject_id")) or {}
                record_hotel_event({
                    "stage": "read_task_result", "result": result,
                    "request": {"task_id": task_id, "expected_revision": expected_revision},
                    "reference_state": {
                        "task_exists": bool(task), "task_revision": task.get("revision"),
                        "task_status": task.get("status"), "result_ref": task.get("result_ref"),
                        "subject_exists": bool(subject), "subject_revision": subject.get("revision"),
                        "subject_validation": subject.get("validation"),
                        "result_revision": (subject.get("result") or {}).get("revision"),
                    },
                })
                return result

            def observed_reply(call_id, name, data, **updates):
                result = _reply(call_id, name, data, **updates)
                if name in {"plan_trip", "consult_policy"}:
                    record_hotel_event({"stage": "business_result", "tool": name,
                                  "result": json.loads(result.update["messages"][0].content)})
                return result

            original_check = PostgresPolicyStore.check

            def observed_check(instance, facts):
                result = original_check(instance, facts)
                record_hotel_event({"stage": "policy_check", "facts": facts.model_dump(mode="json"),
                              "result": result})
                return result

            patches.enter_context(patch("business_travel.graph.run_planner", side_effect=observed_plan))
            patches.enter_context(patch("business_travel.graph.stored_task_result",
                                       side_effect=observed_result))
            patches.enter_context(patch("business_travel.graph._reply", side_effect=observed_reply))
            patches.enter_context(patch.object(PostgresPolicyStore, "check", new=observed_check))
        if args.policy_rag:
            from business_travel.policy_rag import explain_policy, _submit, _validate_citations
            from pydantic import ValidationError
            explanations = []
            stages = []

            def observe_call(stage, function, *values, **options):
                event = {"stage": stage, "turn": len(explanations) + 1}
                if args.university_policy and stage in {"PolicyAnswer", "PolicyAnswerReview"}:
                    event["test_input"] = {key: deepcopy(values[4].get(key)) for key in (
                        "question", "user_request", "facts", "lodging_lookup", "validation_feedback",
                        "policy_query", "request_context", "requirements")}
                try:
                    result = function(*values, **options)
                    event["status"] = "ok"
                    if hasattr(result, "model_dump"):
                        event["proposal"] = result.model_dump(mode="json")
                    return result
                except Exception as error:
                    event.update(status="failed", category=type(error).__name__)
                    if isinstance(error, ValidationError):
                        allowed = {"answer_requires_only_cited_claims", "empty_claim",
                                   "non_answer_must_not_include_claims", "clarify_requires_question",
                                   "retrieve_requires_query"}
                        event["validation"] = [
                            {"type": item["type"], "loc": item["loc"],
                             "constraint": str(item.get("ctx", {}).get("error", ""))
                             if str(item.get("ctx", {}).get("error", "")) in allowed else "other"}
                            for item in error.errors(include_input=False, include_url=False)]
                    raise
                finally:
                    stages.append(event)
                    (output / "policy-stages.json").write_text(
                        json.dumps(stages, ensure_ascii=False, indent=2), encoding="utf-8")

            patches.enter_context(patch("business_travel.policy_rag._submit",
                side_effect=lambda *v, **kw: observe_call(v[2].__name__, _submit, *v, **kw)))
            patches.enter_context(patch("business_travel.policy_rag._validate_citations",
                side_effect=lambda *v, **kw: observe_call("citations", _validate_citations, *v, **kw)))

            def observed_explanation(*values, **options):
                result = explain_policy(*values, **options)
                explanations.append({**result, "test_request": {
                    "question": values[3],
                    "facts": options.get("facts", {}),
                    "user_request": options.get("user_request"),
                    "policy_query": options.get("policy_query"),
                    "request_context": options.get("request_context")},
                    "test_lookup": result.get("lodging_lookup"),
                    "test_requirements": result.get("requirements")})
                (output / "policy-explanations.json").write_text(
                    json.dumps(explanations, ensure_ascii=False, indent=2), encoding="utf-8")
                return result

            patches.enter_context(patch("business_travel.graph.explain_policy",
                                       side_effect=observed_explanation))
        app = create_app(settings, config)
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False,
                                               proxy_headers=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + 15
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        if not server.started:
            raise RuntimeError("Browser test server did not start")
        # Do not pass database/provider credentials to the browser subprocess.
        env = {key: os.environ[key] for key in
               ("PATH", "SystemRoot", "WINDIR", "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA")
               if key in os.environ}
        env.update(NODE_PATH=str(Path(args.node_modules).resolve()), AUTH_TEST_ORIGIN=origin,
                   AUTH_TEST_PASSWORD=password, AUTH_TEST_OUTPUT=str(output),
                   AUTH_TEST_BROWSER=str(Path(args.browser).resolve()))
        suite = ("verify_university_browser.cjs" if args.university_policy else
                 "verify_resources_browser.cjs" if args.resources else
                 "verify_run_recovery_browser.cjs" if args.run_recovery else
                 "verify_failed_turn_browser.cjs" if args.failed_turn_flow else
                 "verify_hotel_flow_browser.cjs" if args.hotel_flow else
                 "verify_policy_rag_browser.cjs" if args.policy_rag else
                 "verify_task_controls_browser.cjs" if args.task_controls else
                 "verify_conversations_browser.cjs" if args.conversations else
                 "verify_chat_browser.cjs" if args.chat else "verify_auth_browser.cjs")
        result = subprocess.run([str(Path(args.node).resolve()), str(root / "scripts" / suite)],
                                env=env, cwd=root,
                                timeout=960 if args.policy_rag or args.hotel_flow or args.failed_turn_flow else
                                360 if args.chat or args.task_controls else 120)
        print("浏览器证据目录:", output.relative_to(root))
        if args.langfuse:
            from scripts.verify_business_traces import verify_business_traces
            summary_path = output / "summary.json"
            summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}
            private = [turn[key] for turn in summary.get("turns", [])
                       for key in ("question", "answer") if turn.get(key)]
            cloud = verify_business_traces(traces, output / "langfuse.json", private)
            print("真实请求云端验收:", json.dumps({
                "passed": cloud["passed"], "request_count": cloud["request_count"],
                "error_type": cloud.get("error_type"),
            }, ensure_ascii=False))
            if not cloud["passed"]:
                return 1
        return result.returncode
    finally:
        if server:
            server.should_exit = True
        if thread:
            thread.join(timeout=15)
            if thread.is_alive():
                frames = sys._current_frames()
                diagnostic = {
                    "schema": schema,
                    "connections": len(server.server_state.connections),
                    "tasks": len(server.server_state.tasks),
                    "threads": [{"name": item.name, "stack": [
                        {"file": frame.filename, "function": frame.name, "line": frame.lineno}
                        for frame in traceback.extract_stack(frames[item.ident])]}
                        for item in threading.enumerate() if item.ident in frames],
                }
                (output / "shutdown-failure.json").write_text(
                    json.dumps(diagnostic, ensure_ascii=False, indent=2), encoding="utf-8")
                raise RuntimeError("Browser server did not stop; test schema was retained")
        listener.close()
        patches.close()
        if settings.memory_schema != schema or not schema.startswith("travel_auth_browser_"):
            raise AssertionError("Not a disposable browser schema")
        with memory_connection(settings) as connection:
            connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


if __name__ == "__main__":
    raise SystemExit(main())

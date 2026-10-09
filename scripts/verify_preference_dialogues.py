"""隔离真实模型对话验收；仅使用临时记忆，不写正式库或调用差旅供应商。"""

import argparse
import json
import sys
import tempfile
import uuid
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from business_travel.graph import build_assistant
from business_travel.memory import TravelMemory
from business_travel.models import create_model
from business_travel.policy import PolicyStore
from business_travel.settings import Settings


class UnavailableProvider:
    def search(self, *args, **kwargs):
        raise RuntimeError("供应商不在本次验收范围内")

    def weather(self, *args, **kwargs):
        raise RuntimeError("天气不在本次验收范围内")


def check_memory(case, snapshot):
    """Check declarative synthetic expectations independently of the assistant's answer."""
    expectation = case.get("memory_expectation")
    if expectation is None:
        return ["missing_memory_expectation"]
    items = snapshot["items"]
    failures = []
    if len(items) != expectation["count"]:
        failures.append("memory_count_mismatch")
    for expected in expectation.get("records", []):
        matches = [item for item in items if
                   ("type" not in expected or item["type"] == expected["type"])
                   and expected["value_contains"] in (
                       item["value"] if isinstance(item["value"], str) else " ".join(item["value"]))]
        if len(matches) != 1:
            failures.append("memory_record_mismatch")
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--case", action="append", help="仅运行指定 ID，可重复指定")
    args = parser.parse_args()
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    if args.case:
        cases = [case for case in cases if case["id"] in args.case]
        if not cases:
            parser.error("未找到指定用例")
    settings = Settings.from_env()
    if not settings.api_key or not settings.base_url or not settings.model:
        print("模型配置缺失，未执行。")
        return 2
    settings = replace(settings, timeout=min(settings.timeout, 30), slot_max_attempts=1)
    output = Path(__file__).resolve().parent.parent / ".verification" / ("preference-dialogues-" + uuid.uuid4().hex)
    output.mkdir(parents=True)
    results = []
    for case in cases:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            memory = TravelMemory(root, root / "legacy", "synthetic")
            if case.get("seed"):
                memory.save_preferences(case["seed"])
            model = create_model(settings)
            graph = build_assistant(lambda role: model, settings, memory, PolicyStore(None, None),
                checkpointer=InMemorySaver(), provider=UnavailableProvider(), information=UnavailableProvider())
            config = {"configurable": {"thread_id": "synthetic:case"}, "recursion_limit": 20}
            entry = {"id": case["id"], "question": case["question"], "expected": case["expected"]}
            try:
                result = graph.invoke({"messages": [HumanMessage(content=case["question"])]}, config)
                entry["interrupted"] = bool(result.get("__interrupt__"))
                entry["slot_error"] = result.get("slot_error")
                if result.get("__interrupt__"):
                    entry["confirmation"] = [item.value for item in result["__interrupt__"]]
                    entry["error_category"] = "UnexpectedPreferenceConfirmation"
                entry["answer"] = result["messages"][-1].content
                entry["tool_results"] = [{"name": message.name, "content": message.content}
                    for message in result["messages"] if isinstance(message, ToolMessage)]
                entry["tool_calls"] = [call for message in result["messages"]
                    if isinstance(message, AIMessage) for call in message.tool_calls]
                entry["preferences"] = memory.preference_snapshot()
                failures = check_memory(case, entry["preferences"])
                if entry["slot_error"]:
                    failures.append("slot_error")
                for item in entry["tool_results"]:
                    payload = json.loads(item["content"])
                    if payload.get("status") in {"error", "review_unavailable", "conflict"}:
                        failures.append(item["name"] + ":" + payload["status"])
                if failures:
                    entry["error_category"] = "DialogueExecutionFailed"
                    entry["failures"] = failures
            except Exception as error:
                entry["error_category"] = type(error).__name__
            results.append(entry)
            (output / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
            print(case["id"], "passed" if "error_category" not in entry else entry["error_category"], flush=True)
            error = entry.get("slot_error") or {}
            if error.get("reason") == "slot_model_unavailable" or entry.get("error_category") in {
                "APIConnectionError", "AuthenticationError", "PermissionDeniedError", "APITimeoutError"
            }:
                print("模型连接或鉴权不可用，停止后续请求。")
                break
    print("证据目录:", output.relative_to(Path(__file__).resolve().parent.parent))
    return 0 if len(results) == len(cases) and not any(entry.get("error_category") for entry in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

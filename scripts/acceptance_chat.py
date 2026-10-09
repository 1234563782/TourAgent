"""显式运行真实对话验收，隔离记忆并保存问题、回答和调用轨迹。"""

import argparse
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langchain_core.messages import HumanMessage
from business_travel.models import create_model
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from business_travel.graph import build_assistant
from business_travel.memory import TravelMemory
from business_travel.policy import PolicyStore
from business_travel.settings import Settings


class TracedModel:
    def __init__(self, model, role, events):
        self.model, self.role, self.events = model, role, events

    def bind_tools(self, tools, **kwargs):
        return TracedModel(self.model.bind_tools(tools, **kwargs), self.role, self.events)

    def invoke(self, messages):
        result = self.model.invoke(messages)
        self.events.append({
            "role": self.role, "answer": result.content,
            "tool_calls": result.tool_calls,
            "tool_results": [
                {"name": message.name, "content": message.content}
                for message in messages if message.type == "tool"
            ],
        })
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ids", nargs="+", help="仅重放指定案例ID，原始问题保持不变")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("输出目录已存在，请使用新目录以保留原始验收证据。")
    settings = Settings.from_env()
    missing = settings.missing_model_config() + settings.missing_policy_config()
    if missing:
        print(json.dumps({"missing": missing}))
        return 2
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    if args.ids:
        if not set(args.ids) <= {case["id"] for case in cases}:
            parser.error("指定案例ID不存在")
        cases = [case for case in cases if case["id"] in args.ids]
    args.output.mkdir(parents=True)
    settings = replace(settings, storage_dir=args.output / "memory",
                       legacy_memory_dir=args.output / "legacy")
    secrets = [settings.api_key, settings.embedding_api_key, settings.policy_database_url,
               settings.tuniu_api_key, settings.amap_key,
               *[settings.tuniu_token(kind) for kind in ("flight", "train", "hotel")]]
    model = create_model(settings)
    policy = PolicyStore.from_settings(settings)
    records = []
    for case in cases:
        events = []
        memory = TravelMemory(settings.storage_dir, settings.legacy_memory_dir, case["id"])
        graph = build_assistant(lambda role: TracedModel(model, role, events),
                                settings, memory, policy, checkpointer=InMemorySaver())
        config = {"configurable": {"thread_id": case["id"]},
                  "recursion_limit": settings.max_steps}
        record = {"id": case["id"], "expected": case["expected"], "turns": []}
        for query in case["questions"]:
            events.clear()
            start = time.monotonic()
            turn = {"question": query}
            try:
                result = graph.invoke({"messages": [HumanMessage(content=query)]}, config)
                if result.get("__interrupt__"):
                    turn["confirmation_requested"] = [
                        pending.value for pending in result["__interrupt__"]]
                    turn["confirmation_response"] = case.get("confirm", False)
                    result = graph.invoke(Command(resume=turn["confirmation_response"]), config)
                turn["answer"] = result["messages"][-1].content
                turn["draft"] = result.get("draft")
                turn["slots"] = result.get("slots", {})
                turn["slot_error"] = result.get("slot_error")
                current_results = []
                for message in reversed(result["messages"]):
                    if message.type == "human":
                        break
                    if message.type == "tool":
                        current_results.append({"name": message.name, "content": message.content})
                turn["business_results"] = list(reversed(current_results))
                turn["status"] = "completed"
            except Exception as error:
                turn.update(status="error", error_category=type(error).__name__)
            turn.update(seconds=round(time.monotonic() - start, 2), events=list(events),
                        memory=memory.read())
            record["turns"].append(turn)
            print(json.dumps({"id": case["id"], "turn": len(record["turns"]),
                              "status": turn["status"], "seconds": turn["seconds"]}), flush=True)
            if turn["status"] == "error":
                break
        records.append(record)
        text = json.dumps(records, ensure_ascii=False, indent=2, default=str)
        for secret in secrets:
            if secret:
                text = text.replace(secret, "[REDACTED]")
        (args.output / "results.json").write_text(text, encoding="utf-8")
        if any(turn["status"] == "error" for turn in record["turns"]):
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

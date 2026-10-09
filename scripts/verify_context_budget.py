"""真实模型上下文抽查：合成历史、临时记忆，不访问正式用户记录或供应商。"""

import argparse
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from business_travel.context_budget import request_size, input_limit
from business_travel.graph import build_assistant
from business_travel.memory import TravelMemory
from business_travel.models import create_model, model_error_details
from business_travel.policy import PolicyStore
from business_travel.settings import Settings


class AuditedModel:
    def __init__(self, model, role, calls):
        self.model, self.role, self.calls = model, role, calls

    def bind_tools(self, tools, **options):
        return AuditedModel(self.model.bind_tools(tools, **options), self.role, self.calls)

    def invoke(self, messages, *args, **options):
        self.calls.append({"role": self.role, "message_count": len(messages),
                           "estimated_message_tokens": request_size(messages)})
        return self.model.invoke(messages, *args, **options)


class ForbiddenProvider:
    def __getattr__(self, name):
        raise AssertionError("No supplier or map queries belong in this acceptance")


def run(settings, report, turns):
    with TemporaryDirectory(prefix="travel_context_") as directory:
        root = Path(directory)
        settings = replace(settings, storage_dir=root, legacy_memory_dir=root / "legacy")
        memory = TravelMemory(root, root / "legacy", "synthetic_context")
        calls = report["model_calls"]
        model = create_model(settings)
        graph = build_assistant(lambda role: AuditedModel(model, role, calls), settings, memory,
            PolicyStore(None, None), checkpointer=InMemorySaver(),
            provider=ForbiddenProvider(), information=ForbiddenProvider())
        config = {"configurable": {"thread_id": "synthetic_context"}, "recursion_limit": settings.max_steps}
        questions = [
            "这是一笔酒店住宿报销，实际金额是287元。请按已知条件核对，其他条件都尚未提供。",
            "更正刚才那笔住宿费：金额还没有核实，撤回287元；不要将未知当成0元，其他条件不变。",
            "请只读取本会话message_index=0的原始消息，告诉我当时的完整原话；"
            "这不是恢复当时的金额，也不要重新核验或保存任何东西。",
        ]
        first_subject = None
        previous = []
        for index, question in enumerate(questions):
            incoming = []
            if index == 1:
                for number in range(turns):
                    incoming.extend([
                        HumanMessage(f"合成背景第{number}条：" + "仅作背景记录，不修改业务事实，不授权任何操作。" * 16),
                        AIMessage("这只是合成背景，不改变事项。"),
                    ])
            incoming.append(HumanMessage(question))
            report["turns"].append(record := {"question": question, "checks": {}})
            raw_history = [*previous, *incoming]
            record["uncompressed_message_estimate"] = request_size(raw_history)
            result = graph.invoke({"messages": incoming}, config)
            record["slot_error"] = result.get("slot_error")
            record["answer"] = result["messages"][-1].content
            record["checks"]["slot_review_passed"] = not result.get("slot_error")
            record["checks"]["original_messages_preserved"] = result["messages"][:len(raw_history)] == raw_history
            if result.get("slot_error"):
                record["status"] = "failed"
                return False
            subject = result.get("slots", {}).get("expense", {})
            if index == 0:
                record["checks"]["actual_amount_stored"] = str(subject.get("values", {}).get("amount")) == "287"
                first_subject = deepcopy(subject)
            elif index == 1:
                record["checks"]["summary_created"] = result.get("context_summary") is not None
                record["checks"]["amount_retracted_not_zero"] = (
                    "amount" in subject.get("values", {}) and subject["values"]["amount"] is None)
                record["checks"]["same_subject"] = subject.get("id") == first_subject.get("id")
                record["checks"]["source_index_preserved"] = (
                    subject.get("evidence", {}).get("amount", {}).get("user_message") == turns + 1)
                record["checks"]["raw_history_exceeds_budget"] = (
                    request_size(raw_history) > input_limit(settings, "slots"))
                first_subject = deepcopy(subject)
            else:
                record["checks"]["facts_unchanged"] = subject == first_subject
                reads = [json.loads(message.content) for message in result["messages"][len(previous):]
                         if isinstance(message, ToolMessage) and message.name == "read_conversation_history"]
                record["checks"]["exact_original_read"] = any(
                    item.get("complete") is True and item.get("message_index") == 0
                    and json.loads(item["text"])["content"] == questions[0] for item in reads)
            record["checks"]["no_memory_write"] = memory.read()["trip_history"] == [] and memory.read()["preferences"] == []
            record["status"] = "passed" if all(record["checks"].values()) else "failed"
            print(json.dumps({"turn": index + 1, "status": record["status"]}), flush=True)
            if record["status"] != "passed":
                return False
            previous = result["messages"]
        return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--turns", type=int, default=120, help="注入合成历史轮数，不调用模型生成")
    args = parser.parse_args()
    if args.output.exists() or not 1 <= args.turns <= 1000:
        parser.error("输出文件必须不存在，历史轮数应为1至1000。")
    settings = Settings.from_env()
    if missing := settings.missing_model_config():
        print(json.dumps({"missing": missing}))
        return 2
    report = {"scope": "真实模型、合成历史、临时记忆；不测政策正确性或供应商，不代表泛化可靠率",
              "status": "failed", "turns": [], "model_calls": []}
    try:
        if run(settings, report, args.turns):
            report["status"] = "passed"
    except Exception as error:
        report["error"] = model_error_details(error)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"status": report["status"], "turns": len(report["turns"])}))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

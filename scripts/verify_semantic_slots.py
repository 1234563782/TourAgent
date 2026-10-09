"""真实模型槽位探测；不连接数据库或业务供应商，不等同于端到端验收。"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langchain_core.messages import AIMessage, HumanMessage

from business_travel.models import create_model
from business_travel.settings import Settings
from business_travel.slots import extract_slots
from scripts.acceptance_chat import TracedModel
from scripts.slot_fixture import AtomicProposalFixture


def compare(result, expected, expected_error=None):
    if expected_error:
        if result.get("status") != "error" or result.get("reason") != "slot_validation_failed":
            return ["错误提案没有被校验拒绝"]
        return [] if any(expected_error in str(error) for error in result.get("validation_errors", [])) else [
            "未观察到指定的语义拒绝原因"]
    if result.get("status") != "ok":
        return ["提取状态不是ok"]
    actual = result.get("tasks", [])
    if len(actual) != len(expected):
        return ["任务数不符"]
    errors = []
    for index, (task, requirement) in enumerate(zip(actual, expected)):
        if task["intent"] != requirement["intent"]:
            errors.append(f"任务{index + 1}意图不符")
        values = (task.get("subject") or {}).get("values", {})
        for field, value in requirement["values"].items():
            if values.get(field) != value:
                errors.append(f"任务{index + 1}字段{field}不符")
    return errors


class FixedProposal:
    """故意注入错误提案，仅用于测真实复核模型，不冒充真实提取。"""

    def __init__(self, proposal):
        self.proposal = proposal
        self.fixture = AtomicProposalFixture()
        self.tool_choice = None

    def bind_tools(self, tools, **kwargs):
        self.tool_choice = kwargs.get("tool_choice")
        return self

    def invoke(self, messages):
        response = AIMessage(content="", tool_calls=[
            {"name": "SlotSubmission", "args": self.proposal, "id": "injected-proposal", "type": "tool_call"}])
        return self.fixture.reply(self.tool_choice, response, messages)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=Path("tests/semantic_cases.json"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ids", nargs="+")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("输出目录已存在，不覆盖历史证据")
    settings = Settings.from_env()
    if settings.missing_model_config():
        print(json.dumps({"missing": settings.missing_model_config()}))
        return 2
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    if args.ids:
        if not set(args.ids) <= {case["id"] for case in cases}:
            parser.error("未知案例ID")
        cases = [case for case in cases if case["id"] in args.ids]
    args.output.mkdir(parents=True)
    model = create_model(settings)
    records = []
    for case in cases:
        events = []
        messages = [(HumanMessage if message["role"] == "human" else AIMessage)(
            content=message["content"]) for message in case["messages"]]
        start = time.monotonic()
        extractor = FixedProposal(case["proposal"]) if "proposal" in case else TracedModel(model, "slots", events)
        result = extract_slots(extractor, messages, {}, 1 if "proposal" in case else settings.slot_max_attempts,
            reviewer=TracedModel(model, "reviewer", events), context_limit=settings.slot_context_limit,
            task_limit=settings.slot_task_limit)
        errors = compare(result, case.get("expected", []), case.get("expected_error"))
        record = {**case, "result": result, "events": events, "errors": errors,
                  "passed": not errors, "mode": "review_only" if "proposal" in case else "extract_and_review",
                  "seconds": round(time.monotonic() - start, 2)}
        records.append(record)
        text = json.dumps(records, ensure_ascii=False, indent=2)
        for secret in (settings.api_key, settings.embedding_api_key, settings.policy_database_url,
                       settings.memory_database_url, settings.tuniu_api_key, settings.amap_key):
            if secret:
                text = text.replace(secret, "[REDACTED]")
        (args.output / "results.json").write_text(text, encoding="utf-8")
        print(json.dumps({"id": case["id"], "passed": not errors, "status": result["status"],
                          "seconds": record["seconds"]}, ensure_ascii=False), flush=True)
        if result.get("reason") in {"slot_model_unavailable", "semantic_review_unavailable"}:
            return 2
    return 0 if all(record["passed"] for record in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())

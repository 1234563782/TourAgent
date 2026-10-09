"""隔离比较单次提取与提取加复核；不调用业务工具或写入用户记忆。"""

import argparse
from copy import deepcopy
from dataclasses import fields
from datetime import date
import hashlib
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langchain_core.messages import AIMessage, HumanMessage

from business_travel.models import create_model
from business_travel.settings import Settings
from business_travel.slots import (
    propose_slots, _snapshot, conversation_context,
    extract_slots, stage_submission, submission_tasks, normalize_task_operation,
)
from scripts.slot_command_experiment import command_extract
from scripts.typed_scenario_experiment import typed_extract
from business_travel.slots import ExpensePatch, TripPatch


def validate_case_fields(cases):
    """Fail before paid calls when an expectation uses fields outside the domain contract."""
    for case in cases:
        for turn in case["turns"]:
            for expected in turn["expected"]:
                fields = TripPatch.model_fields if expected["intent"] == "plan_trip" else ExpensePatch.model_fields
                invalid = (set(expected["values"]) | set(expected.get("absent", []))) - fields.keys()
                invalid |= set(expected.get("scenario", {})) - ExpensePatch.model_fields.keys()
                if invalid:
                    raise ValueError(f"{case['id']}: unknown expected fields: {sorted(invalid)}")


class MeteredModel:
    def __init__(self, model, events, role):
        self.model, self.events, self.role = model, events, role

    def bind_tools(self, tools, **kwargs):
        return MeteredModel(self.model.bind_tools(tools, **kwargs), self.events, self.role)

    def invoke(self, messages):
        start = time.monotonic()
        event = {"role": self.role}
        try:
            response = self.model.invoke(messages)
            event.update(tool_calls=response.tool_calls, usage=response.usage_metadata,
                         answer=response.content)
            return response
        except Exception as error:
            event["error_category"] = type(error).__name__
            raise
        finally:
            event["seconds"] = round(time.monotonic() - start, 3)
            self.events.append(event)


def direct_extract(model, messages, current, settings):
    """仅用于实验；机械暂存结果从不标成已复核或写入生产状态。"""
    users = [message.content for message in messages if isinstance(message, HumanMessage)]
    payload = {
        "today": date.today().isoformat(), "current_user_index": len(users) - 1,
        "current_message": users[-1], "current_slots": _snapshot(current),
        "conversation": conversation_context(messages, settings.slot_context_limit),
        "task_limit": settings.slot_task_limit,
        "task_context": [], "pending_tasks": [], "pending_turns": [],
    }
    try:
        submission, _, _ = propose_slots(model, messages, current, payload,
                                        context_limit=settings.slot_context_limit)
    except Exception as error:
        return {"status": "error", "reason": "slot_model_unavailable",
                "error_category": type(error).__name__}
    try:
        if submission.task_controls:
            raise ValueError("task_controls_require_production_reviewed_ledger")
        updated = deepcopy(current)
        records = []
        for operation in submission_tasks(submission, settings.slot_task_limit):
            task = normalize_task_operation(operation, [], current)
            updated = stage_submission(task, updated, users)
            domain = "trip" if task.intent == "plan_trip" else "expense"
            records.append({
                "intent": task.intent, "question": task.question or users[-1],
                "subject": deepcopy(updated[domain]) if task.intent != "other" else None,
            })
        return {"status": "ok", "slots": updated, "tasks": records,
                "experimental_unreviewed": True}
    except (ValueError, KeyError, TypeError) as error:
        return {"status": "error", "reason": "slot_validation_failed",
                "error_category": type(error).__name__}


def score(result, expected, previous=None):
    """严格核对指定事实及禁止推断的字段，不用另一个模型当裁判。"""
    errors = []
    checks = correct = 0
    actual = result.get("tasks", [])
    if result.get("status") != "ok":
        errors.append("未产出可用状态")
    if len(actual) != len(expected):
        errors.append("任务数量不符")
    for index, requirement in enumerate(expected):
        task = actual[index] if index < len(actual) else {}
        values = (task.get("subject") or {}).get("values", {})
        targets = {"intent": requirement["intent"], **requirement["values"]}
        for field, value in targets.items():
            checks += 1
            observed = task.get("intent") if field == "intent" else values.get(field)
            matched = (result.get("status") == "ok" and observed == value
                       and (field == "intent" or field in values))
            correct += int(matched)
            if not matched:
                errors.append(f"任务{index + 1}.{field}: 预期{value!r}，实际{observed!r}")
        for field in requirement.get("absent", []):
            checks += 1
            matched = result.get("status") == "ok" and field not in values
            correct += int(matched)
            if not matched:
                errors.append(f"任务{index + 1}.{field}: 不应推断")
        for field, value in requirement.get("scenario", {}).items():
            checks += 1
            scenario = task.get("scenario", {}).get("values", {})
            matched = (result.get("status") == "ok" and field in scenario
                       and scenario[field] == value)
            correct += int(matched)
            if not matched:
                errors.append(f"任务{index + 1}.scenario.{field}: 假设值未正确保留")
        if requirement.get("same_subject"):
            checks += 1
            domain = "trip" if requirement["intent"] == "plan_trip" else "expense"
            prior = (previous or {}).get(domain, {})
            subject = task.get("subject") or {}
            matched = (result.get("status") == "ok" and bool(prior.get("id"))
                       and subject.get("id") == prior["id"]
                       and subject.get("revision", 0) >= prior["revision"])
            correct += int(matched)
            if not matched:
                errors.append(f"任务{index + 1}: 未延续原事项")
    return {"passed": not errors, "errors": errors, "checks": checks, "correct": correct,
            "wrong_accepted": result.get("status") == "ok" and bool(errors)}


def summarize(records):
    summary = {}
    for strategy in sorted({record["strategy"] for record in records}):
        rows = [row for row in records if row["strategy"] == strategy]
        events = [event for row in rows for event in row["events"]]
        usage = [event["usage"] for event in events if event.get("usage")]
        summary[strategy] = {
            "turns": len(rows), "passed": sum(row["passed"] for row in rows),
            "wrong_accepted": sum(row["wrong_accepted"] for row in rows),
            "clarifications": sum(row["result"]["status"] == "clarify" for row in rows),
            "errors": sum(row["result"]["status"] == "error" for row in rows),
            "checks": sum(row["checks"] for row in rows),
            "correct": sum(row["correct"] for row in rows),
            "calls": len(events),
            "mean_seconds": round(sum(row["seconds"] for row in rows) / len(rows), 3),
            "reported_tokens": sum(item.get("total_tokens", 0) for item in usage),
            "usage_missing_calls": len(events) - len(usage),
            "money_cost": None,
        }
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--strategies", nargs="+", choices=["direct", "reviewed", "commands", "typed"],
                        default=["direct", "reviewed"])
    args = parser.parse_args()
    if args.output.exists() or not 1 <= args.repeats <= 5:
        parser.error("输出目录必须不存在；重复次数应在1至5之间")
    if len(args.strategies) != len(set(args.strategies)):
        parser.error("策略不能重复")
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    if (not cases or len({case["id"] for case in cases}) != len(cases)
            or any("proposal" in case or not case.get("turns") for case in cases)):
        parser.error("必须提供非空、唯一ID的真实输入案例，禁止注入提案")
    try:
        validate_case_fields(cases)
    except ValueError as error:
        parser.error(str(error))
    settings = Settings.from_env()
    if settings.missing_model_config():
        print(json.dumps({"missing": settings.missing_model_config()}))
        return 2
    model = create_model(settings)
    secrets = [getattr(settings, field.name) for field in fields(settings)
               if any(word in field.name for word in ("key", "token", "database_url"))
               and isinstance(getattr(settings, field.name), str) and getattr(settings, field.name)]
    args.output.mkdir(parents=True)
    records = []
    metadata = {
        "model": settings.model, "date": date.today().isoformat(),
        "dataset_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(),
        "repeats": args.repeats, "extraction_attempts": 1,
        "strategies": args.strategies,
        "source_sha256": {
            name: hashlib.sha256((Path(__file__).resolve().parents[1] / name).read_bytes()).hexdigest()
            for name in ("scripts/compare_slot_strategies.py", "scripts/slot_command_experiment.py",
                         "scripts/typed_scenario_experiment.py",
                         "business_travel/slots.py", "business_travel/semantic_review.py",
                         "business_travel/models.py")
        },
        "scope": "理解层实验，不是业务端到端验收；direct结果未经语义复核",
        "pending": ["更强模型对照", "事实关系抽取对照", "完整业务端到端"],
    }

    def save():
        text = json.dumps({"metadata": metadata, "summary": summarize(records),
                           "records": records}, ensure_ascii=False, indent=2)
        for secret in secrets:
            text = text.replace(secret, "[REDACTED]")
        (args.output / "results.json").write_text(text, encoding="utf-8")

    for repeat in range(args.repeats):
        for case_index, case in enumerate(cases):
            # 交替顺序，减少固定先后次序造成的服务负载偏差。
            strategies = list(args.strategies)
            if (repeat + case_index) % 2:
                strategies.reverse()
            for strategy in strategies:
                current, messages = {}, []
                for turn_index, turn in enumerate(case["turns"]):
                    messages.append(HumanMessage(content=turn["question"]))
                    events = []
                    start = time.monotonic()
                    extractor = MeteredModel(model, events, "extractor")
                    if strategy == "direct":
                        result = direct_extract(extractor, messages, current, settings)
                    elif strategy == "commands":
                        result = command_extract(extractor, messages, current, settings)
                    elif strategy == "typed":
                        result = typed_extract(extractor, messages, current, settings)
                    else:
                        result = extract_slots(
                            extractor, messages, current, 1,
                            reviewer=MeteredModel(model, events, "reviewer"),
                            context_limit=settings.slot_context_limit,
                            task_limit=settings.slot_task_limit,
                        )
                    record = {
                        "id": case["id"], "repeat": repeat + 1, "turn": turn_index + 1,
                        "strategy": strategy, "question": turn["question"],
                        "expected": turn["expected"], "result": result, "events": events,
                        "seconds": round(time.monotonic() - start, 3),
                        **score(result, turn["expected"], current),
                    }
                    records.append(record)
                    save()
                    print(json.dumps({key: record[key] for key in
                                      ("id", "repeat", "turn", "strategy", "passed", "seconds")},
                                     ensure_ascii=False), flush=True)
                    if result.get("reason") in {
                        "slot_model_unavailable", "semantic_review_unavailable",
                    }:
                        return 2
                    if result["status"] == "ok":
                        current = result["slots"]
                    if turn.get("assistant"):
                        messages.append(AIMessage(content=turn["assistant"]))
    return 0 if all(record["passed"] for record in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())

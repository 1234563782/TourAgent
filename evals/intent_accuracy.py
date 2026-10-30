"""Evaluate the live Jev intent classifier against a reviewed JSONL dataset.

The evaluator measures the intent decision layer only. It does not run the graph,
MCP tools, policy retrieval, hotel search, or booking flows. Jev answers with three
outcomes per intent: yes, no, and undecided for probabilities inside the
(1 - threshold, threshold) band. Undecided goals are re-read downstream by TurnRoute,
so `exact_match_accuracy` here is a strict floor; `evals/intent_route_accuracy.py`
scores the product-level answer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid4, uuid5

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from business_travel.jev import INTENTS, JevClient
from business_travel.observability import Telemetry
from business_travel.settings import Settings

BUSINESS_TAGS = frozenset(INTENTS)
CONTROL_TAGS = frozenset({"control", "cancel", "resume", "retry", "discard", "recovery",
                          "update", "update_facts"})
RETIRED_TAGS = frozenset({"memory", "retired_preference_memory"})
CONTROL_STATE_FIELDS = ("pending_tasks", "pending_turns", "task_context")


def validate_dataset(cases: list[dict]) -> dict:
    """Enforce the routing contract as an assertion instead of prose in a prompt.

    Cancel, resume, retry, discard and fact updates act on an existing goal, so they
    produce no new intent (slot_protocol.NewRequest.intent). A label that gives such a
    turn an intent silently breaks the control path: the system opens a new task instead
    of cancelling the old one.
    """
    problems = []
    for row in cases:
        tags = set(row.get("tags", []))
        expected = row["expected_intents"]
        retired = tags & RETIRED_TAGS
        if retired:
            problems.append(f"{row['case_id']}: retired capability tag {sorted(retired)}")
        control_only = (tags & CONTROL_TAGS) and not (tags & BUSINESS_TAGS)
        if control_only and expected:
            problems.append(f"{row['case_id']}: control/recovery/update turn must expect no "
                            f"intent, got {expected}")
        if not expected and not any(field in row for field in CONTROL_STATE_FIELDS):
            problems.append(f"{row['case_id']}: empty intent needs the target state "
                            f"({', '.join(CONTROL_STATE_FIELDS)}) it acts on")
    if problems:
        raise ValueError("dataset violates the intent contract:\n  " + "\n  ".join(problems))
    return {"cases": len(cases),
            "empty_intent_cases": [row["case_id"] for row in cases if not row["expected_intents"]],
            "multi_intent_cases": sum(1 for row in cases if len(row["expected_intents"]) > 1),
            "context_cases": sum(1 for row in cases
                                 if any(field in row for field in CONTROL_STATE_FIELDS)),
            "label_counts": {"+".join(key) if key else "(none)": value for key, value
                             in Counter(tuple(row["expected_intents"]) for row in cases).items()}}



def load_cases(path: Path) -> list[dict]:
    rows = []
    state_list_fields = ("conversation", "task_context", "pending_tasks", "pending_turns")
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid JSON at {path}:{line_number}") from error
        expected = row.get("expected_intents")
        if (not isinstance(row.get("case_id"), str) or not isinstance(row.get("message"), str)
                or not row["message"].strip() or not isinstance(expected, list)
                or any(intent not in INTENTS for intent in expected)
                or len(expected) != len(set(expected))):
            raise ValueError(f"invalid intent case at {path}:{line_number}")
        for field in state_list_fields:
            if field in row and not isinstance(row[field], list):
                raise ValueError(f"invalid {field} in intent case at {path}:{line_number}")
        rows.append(row)
    if not rows or len({row["case_id"] for row in rows}) != len(rows):
        raise ValueError("dataset is empty or contains duplicate case IDs")
    validate_dataset(rows)
    return rows


def fingerprint(cases: list[dict]) -> str:
    payload = "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                       for row in cases)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def evaluate_case(client: JevClient, case: dict, telemetry: Telemetry | None = None) -> dict:
    state = {
        "user_message": case["message"],
        "conversation": case.get("conversation") or [case["message"]],
        "current_slots": {},
        "task_context": [],
        "pending_tasks": [],
        "pending_turns": [],
    }
    for field in ("current_slots", "task_context", "pending_tasks", "pending_turns", "clarification"):
        if field in case:
            state[field] = case[field]
    started = time.perf_counter()
    trace_id = None
    identity = SimpleNamespace(user_id="intent-evaluation")
    request_id = uuid4().hex
    if telemetry is None:
        decision = client.classify(state, request_id=request_id)
    else:
        with telemetry.request({}, identity, "intent-accuracy", request_id) as root:
            decision = client.classify(state, request_id=request_id)
            trace_id = root.raw.trace_id if root.raw is not None else None
    hint = decision.hint() if decision.status == "ok" else {}
    actual = hint.get("intents", [])
    # A Noul probability inside the 0.2-0.8 band is "undecided", not "no". TurnRoute
    # re-judges those goals downstream from the current words, so scoring undecided as
    # absence understates the classifier. Keep the three outcomes separate.
    undecided = hint.get("undecided_intents", [])
    rejected = hint.get("rejected_intents", [])
    expected = case["expected_intents"]
    expected_set, actual_set = set(expected), set(actual)
    true_positive = len(expected_set & actual_set)
    false_positive = len(actual_set - expected_set)
    false_negative = len(expected_set - actual_set)
    # Missing goals the classifier left undecided were delegated, not rejected.
    undecided_missed = sorted((expected_set - actual_set) & set(undecided))
    return {
        "case_id": case["case_id"],
        "message": case["message"],
        "tags": case.get("tags", []),
        "expected_intents": expected,
        "actual_intents": actual,
        "undecided_intents": undecided,
        "rejected_intents": rejected,
        "undecided_missed": undecided_missed,
        "status": decision.status,
        "error_code": decision.error_code,
        "model": decision.model,
        "model_version": decision.model_version,
        "passed": decision.status == "ok" and actual_set == expected_set,
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "noul": {key: answer.get("noul") for key, answer in decision.answers.items()
                  if key.startswith("has_") or key == "requires_task_routing"},
        "usage": decision.usage,
        "elapsed_ms": round((time.perf_counter() - started) * 1000),
        "trace_id": trace_id,
    }


def summarize(rows: list[dict], cases: list[dict], dataset_path: Path,
              dataset_sha256: str, threshold: float) -> dict:
    labels = list(INTENTS)
    counts = {label: {"tp": 0, "fp": 0, "fn": 0, "undecided": 0} for label in labels}
    evaluated = [row for row in rows if row["status"] == "ok"]
    for row in evaluated:
        for label in labels:
            counts[label]["tp"] += int(label in row["expected_intents"] and label in row["actual_intents"])
            counts[label]["fp"] += int(label not in row["expected_intents"] and label in row["actual_intents"])
            counts[label]["fn"] += int(label in row["expected_intents"] and label not in row["actual_intents"])
            counts[label]["undecided"] += int(label in row.get("undecided_intents", []))
    per_intent = {}
    for label, values in counts.items():
        precision = values["tp"] / (values["tp"] + values["fp"]) if values["tp"] + values["fp"] else 0
        recall = values["tp"] / (values["tp"] + values["fn"]) if values["tp"] + values["fn"] else 0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0
        per_intent[label] = {**values, "precision": round(precision, 4), "recall": round(recall, 4),
                             "f1": round(f1, 4)}
    tp = sum(values["tp"] for values in counts.values())
    fp = sum(values["fp"] for values in counts.values())
    fn = sum(values["fn"] for values in counts.values())
    micro_precision = tp / (tp + fp) if tp + fp else 0
    micro_recall = tp / (tp + fn) if tp + fn else 0
    total_tokens = sum(row.get("usage", {}).get("total_tokens", 0) for row in rows)
    # Branch-level abstention accounting. undecided labels are delegated to TurnRoute,
    # so the strict metric above is a floor, not the classifier's standalone quality.
    decided_rows = [row for row in evaluated if not row.get("undecided_intents")]
    undecided_rows = [row for row in evaluated if row.get("undecided_intents")]
    undecided_caused = [row for row in evaluated
                        if not row["passed"] and row.get("undecided_missed")]
    decided_passed = sum(row["passed"] for row in decided_rows)
    return {
        "dataset": str(dataset_path),
        "dataset_sha256": dataset_sha256,
        "case_count": len(cases),
        "attempted": len(rows),
        "evaluated": len(evaluated),
        "blocked": len(rows) - len(evaluated),
        "exact_match_count": sum(row["passed"] for row in evaluated),
        "exact_match_accuracy": (round(sum(row["passed"] for row in evaluated) / len(evaluated), 4)
                                 if evaluated else None),
        "decided_cases": len(decided_rows),
        "decided_exact_match_accuracy": round(decided_passed / len(decided_rows), 4) if decided_rows else None,
        "undecided_cases": len(undecided_rows),
        "undecided_label_count": sum(len(row.get("undecided_intents", [])) for row in evaluated),
        "undecided_caused_failures": [row["case_id"] for row in undecided_caused],
        "hard_miss_cases": [row["case_id"] for row in evaluated
                            if not row["passed"] and not row.get("undecided_missed")],
        "micro_precision": round(micro_precision, 4),
        "micro_recall": round(micro_recall, 4),
        "micro_f1": round(2 * micro_precision * micro_recall / (micro_precision + micro_recall), 4)
        if micro_precision + micro_recall else 0,
        "per_intent": per_intent,
        "status_counts": dict(Counter(row["status"] for row in rows)),
        "total_tokens": total_tokens,
        "average_tokens": round(total_tokens / len(evaluated), 2) if evaluated else None,
        "threshold": threshold,
        "interpretation": "exact_match_accuracy scores undecided as absent (strict floor, no TurnRoute). "
                          "decided_exact_match_accuracy covers only cases where all four labels were "
                          "yes/no. Neither is full graph or business-answer accuracy.",
    }


def upload_scores(settings: Settings, rows: list[dict], summary: dict) -> int:
    if not settings.langfuse_enabled:
        raise ValueError("Langfuse is disabled; use TRAVEL_LANGFUSE_ENABLED=1")
    if not all((settings.langfuse_base_url, settings.langfuse_public_key,
                settings.langfuse_secret_key)):
        raise ValueError("Langfuse configuration is incomplete")
    from langfuse import Langfuse

    client = Langfuse(public_key=settings.langfuse_public_key, secret_key=settings.langfuse_secret_key,
                      base_url=settings.langfuse_base_url, environment="intent-evaluation", timeout=30)
    uploaded = 0
    try:
        for row in rows:
            if not row.get("trace_id"):
                continue
            client.create_score(
                name="intent_exact_match", value=int(row["passed"]), data_type="NUMERIC",
                trace_id=row["trace_id"],
                score_id=str(uuid5(NAMESPACE_URL, summary["dataset_sha256"] + ":" + row["case_id"])),
                comment="Exact ordered intent-set match against reviewed case label.",
                metadata={"case_id": row["case_id"], "dataset_sha256": summary["dataset_sha256"]},
            )
            uploaded += 1
        client.flush()
    finally:
        client.flush()
    return uploaded


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=ROOT / "evals" / "intent_accuracy_cases.jsonl")
    parser.add_argument("--output", type=Path, default=ROOT / ".verification" / "intent-accuracy-latest.json")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--langfuse", action="store_true", help="export traces and exact-match scores")
    parser.add_argument("--validate-only", action="store_true",
                        help="check dataset schema and the intent contract offline; no network calls")
    args = parser.parse_args(argv)
    cases = load_cases(args.dataset)
    if args.validate_only:
        print(json.dumps({"dataset": str(args.dataset), "sha256": fingerprint(cases),
                          **validate_dataset(cases)}, ensure_ascii=False, indent=2))
        return 0
    if args.limit:
        if args.limit < 1:
            parser.error("--limit must be positive")
        cases = cases[:args.limit]
    settings = Settings.from_env(ROOT / ".env")
    client = JevClient(settings)
    if not client.available():
        parser.error("JEV unavailable; check JEV_ENABLED and JEV_API_KEY")
    telemetry = Telemetry(settings) if args.langfuse else None
    rows = []
    try:
        for case in cases:
            row = evaluate_case(client, case, telemetry)
            rows.append(row)
            print(json.dumps({"case_id": row["case_id"], "passed": row["passed"],
                              "expected": row["expected_intents"], "actual": row["actual_intents"],
                              "tokens": row["usage"].get("total_tokens")}, ensure_ascii=False), flush=True)
    finally:
        if telemetry:
            telemetry.shutdown()
    summary = summarize(rows, cases, args.dataset, fingerprint(cases), settings.jev_intent_threshold)
    report = {"summary": summary, "cases": rows}
    if args.langfuse:
        try:
            summary["langfuse_uploaded_scores"] = upload_scores(settings, rows, summary)
        except Exception as error:
            summary["langfuse_upload_error"] = type(error).__name__
            report["summary"] = summary
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not summary["evaluated"]:
        return 2
    return 0 if summary["exact_match_accuracy"] == 1 else 1


if __name__ == "__main__":
    raise SystemExit(main())

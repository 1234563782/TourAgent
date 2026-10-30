"""Evaluate the intent decision the way the product actually makes it.

JEV answers each of the four intent questions with a probability. A probability above
the accept edge, or below the symmetric reject edge, is already a decision. Anything in
between is `undecided`, and the product hands those goals to the TurnRoute model, which
reads the current words and returns tasks. The reference answer for such a case is
therefore the TurnRoute result, not an empty JEV intent set.

Two stages are reported separately:

* `jev_only_accuracy`   - what JEV alone would have scored (undecided counted as absent)
* `route_accuracy`      - the final answer after undecided goals reach TurnRoute

The evaluator never runs the graph, tools, policy retrieval or booking flows.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from business_travel.jev import INTENT_QUESTIONS, INTENTS, JevClient
from business_travel.models import create_model
from business_travel.settings import Settings
from business_travel.slot_protocol import TurnRoute, route_prompt
from business_travel.slots import _invoke_protocol
from evals.intent_accuracy import fingerprint, load_cases

DEFAULT_TASK_LIMIT = 8
NEW_TASK = "new_task"


def build_state(case: dict) -> dict:
    """Mirror the state the graph passes to JevClient.classify."""
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
    return state


def build_payload(case: dict, hint: dict) -> dict:
    """Mirror the payload slots.py hands to route_prompt; jev_route stays empty so the
    model must re-read the words instead of replaying a server-side route."""
    return {
        "today": case.get("today", ""),
        "current_user_index": len(case.get("conversation") or [case["message"]]) - 1,
        "current_message": case["message"],
        "current_slots": case.get("current_slots", {}),
        "conversation": case.get("conversation") or [case["message"]],
        "task_limit": case.get("task_limit", DEFAULT_TASK_LIMIT),
        "pending_tasks": case.get("pending_tasks", []),
        "pending_turns": case.get("pending_turns", []),
        "task_context": case.get("task_context", []),
        "jev_hint": hint,
        "jev_route": {},
    }


def route_intents(model, case: dict, hint: dict) -> list[str]:
    """Flatten one TurnRoute call into the intent set the turn produces.

    Only new_task carries a new goal. task_controls, recovery and update_facts act on an
    existing goal and therefore contribute no intent, which is why they must not be
    relabelled as `other`.
    """
    payload = build_payload(case, hint)
    route, _ = _invoke_protocol(model, TurnRoute, route_prompt(payload), payload, [], None, 0)
    return [task.intent for task in route.tasks
            if getattr(task, "operation", None) == NEW_TASK]


def evaluate_case(client: JevClient, model, case: dict) -> dict:
    started = time.perf_counter()
    expected = case["expected_intents"]
    row = {"case_id": case["case_id"], "message": case["message"],
           "tags": case.get("tags", []), "expected_intents": expected,
           "expected_empty_because": case.get("label_note", "")}
    try:
        decision = client.classify(build_state(case), request_id=f"route-{case['case_id']}")
    except Exception as error:  # noqa: BLE001 - a transport failure is a result, not a crash
        return {**row, "status": "error", "reason": "jev_" + type(error).__name__,
                "jev_intents": [], "jev_undecided": [], "final_intents": [], "decided_by": "-",
                "passed": False}
    if decision.status != "ok":
        return {**row, "status": "error", "reason": decision.error_code or decision.status,
                "jev_intents": [], "jev_undecided": [], "final_intents": [], "decided_by": "-",
                "passed": False}
    hint = decision.hint()
    jev_intents = list(hint.get("intents", []))
    undecided = list(hint.get("undecided_intents", []))
    row.update({"status": "ok", "reason": None, "jev_intents": jev_intents,
                "jev_undecided": undecided,
                "noul": {key: answer.get("noul") for key, answer in decision.answers.items()
                         if key in INTENT_QUESTIONS.values()}})
    row["jev_passed"] = set(jev_intents) == set(expected)
    if not undecided:
        final, decided_by = jev_intents, "jev"
    else:
        try:
            final, decided_by = route_intents(model, case, hint), "turnroute"
        except Exception as error:  # noqa: BLE001
            return {**row, "reason": "route_" + type(error).__name__, "final_intents": [],
                    "decided_by": "route-error", "passed": False}
    row.update({"final_intents": final, "decided_by": decided_by,
                "passed": set(final) == set(expected),
                "elapsed_ms": round((time.perf_counter() - started) * 1000)})
    return row


def summarize_route(rows: list[dict], cases: list[dict], dataset: Path, digest: str) -> dict:
    evaluated = [row for row in rows if row["status"] == "ok"]
    delegated = [row for row in evaluated if row["decided_by"] == "turnroute"]
    counts = {label: {"tp": 0, "fp": 0, "fn": 0} for label in INTENTS}
    for row in evaluated:
        for label in INTENTS:
            counts[label]["tp"] += int(label in row["expected_intents"] and label in row["final_intents"])
            counts[label]["fp"] += int(label not in row["expected_intents"] and label in row["final_intents"])
            counts[label]["fn"] += int(label in row["expected_intents"] and label not in row["final_intents"])
    per_intent = {}
    for label, values in counts.items():
        precision = values["tp"] / (values["tp"] + values["fp"]) if values["tp"] + values["fp"] else 0
        recall = values["tp"] / (values["tp"] + values["fn"]) if values["tp"] + values["fn"] else 0
        per_intent[label] = {**values, "precision": round(precision, 4), "recall": round(recall, 4),
                             "f1": round(2 * precision * recall / (precision + recall), 4)
                             if precision + recall else 0}
    tp = sum(value["tp"] for value in counts.values())
    fp = sum(value["fp"] for value in counts.values())
    fn = sum(value["fn"] for value in counts.values())
    micro_precision = tp / (tp + fp) if tp + fp else 0
    micro_recall = tp / (tp + fn) if tp + fn else 0
    return {
        "dataset": str(dataset), "dataset_sha256": digest,
        "case_count": len(cases), "attempted": len(rows), "evaluated": len(evaluated),
        "blocked": len(rows) - len(evaluated),
        "route_accuracy": round(sum(row["passed"] for row in evaluated) / len(evaluated), 4)
        if evaluated else None,
        "jev_only_accuracy": round(sum(row["jev_passed"] for row in evaluated) / len(evaluated), 4)
        if evaluated else None,
        "decided_by": dict(Counter(row["decided_by"] for row in evaluated)),
        "delegated_cases": len(delegated),
        "undecided_resolved": sum(row["passed"] for row in delegated),
        "undecided_resolve_rate": round(sum(row["passed"] for row in delegated) / len(delegated), 4)
        if delegated else None,
        "rescued_cases": [row["case_id"] for row in evaluated
                          if not row["jev_passed"] and row["passed"]],
        "regressed_cases": [row["case_id"] for row in evaluated
                            if row["jev_passed"] and not row["passed"]],
        "missed_cases": [row["case_id"] for row in evaluated if not row["passed"]],
        "per_intent": per_intent,
        "micro_precision": round(micro_precision, 4), "micro_recall": round(micro_recall, 4),
        "micro_f1": round(2 * micro_precision * micro_recall / (micro_precision + micro_recall), 4)
        if micro_precision + micro_recall else 0,
        "interpretation": "route_accuracy is the product-level intent decision: JEV decides "
                          "confident cases and TurnRoute re-reads the words for undecided goals. "
                          "jev_only_accuracy counts those same goals as absent.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=ROOT / "evals" / "intent_accuracy_cases.jsonl")
    parser.add_argument("--output", type=Path,
                        default=ROOT / ".verification" / "intent-route-accuracy.json")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--jev-report", type=Path, default=None,
                        help="optional intent_accuracy.py report to reuse instead of scoring JEV alone")
    args = parser.parse_args(argv)
    cases = load_cases(args.dataset)
    if args.limit:
        if args.limit < 1:
            parser.error("--limit must be positive")
        cases = cases[:args.limit]
    settings = Settings.from_env(ROOT / ".env")
    client = JevClient(settings)
    if not client.available():
        parser.error("JEV unavailable; check JEV_ENABLED and JEV_API_KEY")
    model = create_model(settings)
    rows = []
    for case in cases:
        row = evaluate_case(client, model, case)
        rows.append(row)
        print(json.dumps({"case_id": row["case_id"], "decided_by": row.get("decided_by"),
                          "expected": row["expected_intents"], "jev": row.get("jev_intents"),
                          "undecided": row.get("jev_undecided"), "final": row.get("final_intents"),
                          "passed": row.get("passed")}, ensure_ascii=False), flush=True)
    summary = summarize_route(rows, cases, args.dataset, fingerprint(cases))
    summary["jev_threshold"] = settings.jev_intent_threshold
    summary["jev_reject_edge"] = round(1 - settings.jev_intent_threshold, 4)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"summary": summary, "cases": rows},
                                      ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not summary["evaluated"]:
        return 2
    return 0 if summary["route_accuracy"] == 1 else 1


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())

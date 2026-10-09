"""One-time deterministic metadata migration for the v1 JSONL datasets."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent
EDGE_IDS = {
    "routing-003", "routing-004", "routing-005", "routing-008", "routing-009", "routing-010",
    "rag-005", "tool-005", "tool-007", "tool-008", "memory-007",
    "recovery-002", "recovery-003", "recovery-004", "recovery-005", "recovery-006",
}
ADVERSARIAL_IDS = {"tool-002", "memory-004", "idempotency-002"}
REGRESSION_IDS = {
    "routing-006", "routing-007", "rag-007", "rag-010", "memory-008",
    "recovery-001", "idempotency-001", "resource-001", "resource-002", "resource-003",
    "resource-004", "resource-005", "budget-001", "budget-002", "budget-003",
}
CRITICAL_BEHAVIORS = {
    "cross_user_memory_leak", "duplicate_state_machine_execution", "double_charge",
    "let_second_worker_bypass_slot", "use_process_local_counter_as_authority",
    "execute_both_calls", "read_stale_result", "overwrite_latest_facts", "double_commit",
}


def case_type(case: dict) -> str:
    ident = case["case_id"]
    if case["oracle_type"] == "fault_injection":
        return "fault_injection"
    if ident in ADVERSARIAL_IDS:
        return "adversarial"
    if ident in REGRESSION_IDS:
        return "regression"
    if ident in EDGE_IDS:
        return "edge"
    return "golden"


def expected_outcome(case: dict) -> str:
    expected = case["expected"]
    status = str(expected.get("status", ""))
    if any(token in status for token in ("reject", "conflict", "clarify", "insufficient", "failed", "429")):
        return "safe_reject"
    if case["oracle_type"] == "fault_injection":
        return "fault_contained"
    return "state_match"


def severity(case: dict) -> str:
    forbidden = set(case.get("forbidden_behaviors", []))
    if forbidden & CRITICAL_BEHAVIORS:
        return "critical"
    if case["category"] in {"tool_safety", "recovery_idempotency", "concurrency_budget"}:
        return "high"
    if case["category"] == "policy_rag":
        return "high" if case["oracle_type"] == "fault_injection" else "medium"
    return "medium"


def main() -> None:
    for path in sorted(ROOT.glob("*.jsonl")):
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            case = json.loads(line)
            case["case_type"] = case_type(case)
            case["severity"] = severity(case)
            case["source"] = "project_regression_suite"
            case["expected_outcome"] = expected_outcome(case)
            rows.append(case)
        path.write_text(
            "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows),
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()

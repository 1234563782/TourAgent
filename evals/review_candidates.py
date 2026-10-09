"""Prepare review packets and validate explicit annotations, without agent execution."""

import argparse
from collections import Counter
import json
from pathlib import Path

from evals.extract_candidates import digest, validate_references


def read_rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def make_queue(candidates, proposals):
    queue = []
    for kind, items in (("historical_candidate", candidates), ("synthetic_proposal", proposals)):
        for item in items:
            historical = kind == "historical_candidate"
            evidence = item.get("evidence", [])
            outcomes = sorted({e["observed_outcome"] for e in evidence if "observed_outcome" in e})
            row = {
                "candidate_id": item["candidate_id"] if historical else item["id"],
                "candidate_fingerprint": digest(item),
                "kind": kind,
                "conversation": item["conversation"] if historical else
                [{"role": "user", "content": text} for text in item["conversation"]],
                "required_context": item.get("required_context", item.get("required_setup")),
                "migration_flags": item.get("migration_flags", []),
                "historical_outcomes": outcomes,
                "historical_expectations_not_gold": [
                    {"expected": e["historical_expected"], "source": e["source"]}
                    for e in evidence if e.get("historical_expected")
                ],
                "proposed_contract_not_gold": item.get("expected_contract", []),
                "sources": [e["source"] for e in evidence if "source" in e]
                if historical else [item["basis"]],
                "review_status": "pending",
                "priority": 0 if any("failed" in s or "error" in s for s in outcomes) else 1,
            }
            queue.append(row)
    if len({row["candidate_id"] for row in queue}) != len(queue):
        raise ValueError("duplicate candidate id")
    return sorted(queue, key=lambda row: (row["priority"], row["candidate_id"]))


def template(row):
    return {
        "candidate_id": row["candidate_id"],
        "candidate_fingerprint": row["candidate_fingerprint"],
        "decision": "pending",
        "reviewer": "",
        "rationale": "",
        "category": "",
        "initial_state": None,
        "policy": {"applicability": "unresolved", "sources": []},
        "turn_assertions": [],
        "resolved_migration_flags": [],
    }


def nonempty_text(value):
    return isinstance(value, str) and bool(value.strip())


def validate_decision(root, row, decision):
    if decision.get("candidate_fingerprint") != row["candidate_fingerprint"]:
        raise ValueError("candidate changed; re-review required")
    status = decision.get("decision")
    if status not in {"pending", "approve", "reject"}:
        raise ValueError("invalid review decision")
    if status == "pending":
        return
    if not all(nonempty_text(decision.get(key)) for key in ("reviewer", "rationale")):
        raise ValueError("reviewer and rationale required")
    if status == "reject":
        return
    if not nonempty_text(decision.get("category")):
        raise ValueError("category required")
    initial = decision.get("initial_state")
    if not isinstance(initial, dict) or not all(
        nonempty_text(initial.get(key)) for key in
        ("identity_isolation", "conversation_state", "memory_state", "clock", "providers", "fault_injection")
    ):
        raise ValueError("explicit replay initial_state required")
    policy = decision.get("policy", {})
    if policy.get("applicability") not in {"required", "not_applicable"}:
        raise ValueError("policy applicability unresolved")
    if policy["applicability"] == "required":
        refs = policy.get("sources")
        if not isinstance(refs, list) or not refs or any(
            not isinstance(ref, dict) or not {"path", "sha256", "pointer"} <= ref.keys()
            for ref in refs
        ):
            raise ValueError("policy source references required")
        validate_references(root, refs)
    elif not nonempty_text(policy.get("reason")):
        raise ValueError("policy not_applicable reason required")
    if set(row["migration_flags"]) - set(decision.get("resolved_migration_flags", [])):
        raise ValueError("migration flags unresolved")
    expected_turns = {i for i, message in enumerate(row["conversation"], 1) if message["role"] == "user"}
    assertions = decision.get("turn_assertions")
    if not isinstance(assertions, list):
        raise ValueError("turn assertions required")
    seen = set()
    for assertion in assertions:
        turn = assertion.get("turn")
        if type(turn) is not int or turn not in expected_turns or turn in seen:
            raise ValueError("invalid or duplicate assertion turn")
        seen.add(turn)
        for key in ("must", "must_not"):
            values = assertion.get(key)
            if not isinstance(values, list) or not values or not all(map(nonempty_text, values)):
                raise ValueError("nonempty must and must_not required")
        if not nonempty_text(assertion.get("observable")):
            raise ValueError("observable required; answer text alone may not prove state")
    if seen != expected_turns:
        raise ValueError("every user turn requires assertions")
    validate_references(root, row["sources"])


def apply_decisions(root, queue, decisions):
    by_id = {row["candidate_id"]: row for row in queue}
    seen, approved = set(), []
    counts = Counter()
    for decision in decisions:
        key = decision.get("candidate_id")
        if key not in by_id or key in seen:
            raise ValueError("unknown or duplicate review id")
        seen.add(key)
        row = by_id[key]
        validate_decision(root, row, decision)
        counts[decision["decision"]] += 1
        if decision["decision"] == "approve":
            approved.append({
                "candidate_id": key, "conversation": row["conversation"],
                "sources": row["sources"], "annotation": decision,
                "review_status": "approved_annotation",
                "execution_status": "not_run", "replay_ready": False,
                "adapter_status": "not_bound",
            })
    counts["pending"] += len(queue) - len(seen)
    return approved, dict(counts)


def write_rows(path, rows):
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    root = args.root.resolve()
    queue = make_queue(read_rows(root / "evals/candidates/candidates.jsonl"),
                       read_rows(root / "evals/coverage/proposed_cases.jsonl"))
    validate_references(root, queue)
    output = root / "evals/review"
    decisions_path = output / "decisions.jsonl"
    decisions = read_rows(decisions_path) if decisions_path.exists() else []
    approved, counts = apply_decisions(root, queue, decisions)
    output.mkdir(parents=True, exist_ok=True)
    # This editable file is initialized once; regenerated artifacts never replace it.
    if not decisions_path.exists():
        write_rows(decisions_path, [template(row) for row in queue])
    write_rows(output / "queue.jsonl", queue)
    write_rows(output / "approved_annotations.jsonl", approved)
    lines = ["# Candidate Review Queue", "",
             "Annotations only. Approval does not bind an execution adapter or establish model accuracy.",
             "Edit decisions.jsonl; rerun python -m evals.review_candidates to validate.",
             "Turn numbers are 1-based conversation positions, including assistant messages.",
             "must/must_not are human contracts, not executable scoring expressions.",
             "Review policy versions, initial state, every user turn, and historical failure attribution.",
             "Do not copy historical answers as gold; do not merge different contexts automatically.", ""]
    for row in queue:
        lines += [f"## {row['candidate_id']}", "",
                  f"- Kind: {row['kind']}; historical outcomes: {', '.join(row['historical_outcomes']) or 'none'}",
                  f"- Migration flags: {', '.join(row['migration_flags']) or 'none'}"]
        for i, message in enumerate(row["conversation"], 1):
            text = message["content"].replace("\n", " ")
            lines.append(f"- Turn {i} ({message['role']}): {text}")
        lines.append("")
    (output / "queue.md").write_text("\n".join(lines), encoding="utf-8")
    summary = {"candidates": len(queue), "review_decisions": counts,
               "approved_annotations": len(approved), "executable_cases": 0}
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

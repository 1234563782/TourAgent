"""Extract historical candidates without executing agents or promoting gold labels."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def source_ref(root, path, pointer=""):
    resolved = path.resolve()
    relative = resolved.relative_to(root.resolve()).as_posix()
    return {
        "path": relative,
        "sha256": hashlib.sha256(resolved.read_bytes()).hexdigest(),
        "pointer": pointer,
    }


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def normalize_input(row):
    if "messages" in row:
        messages = [
            {"role": {"human": "user", "ai": "assistant"}.get(m["role"], m["role"]),
             "content": m["content"]}
            for m in row["messages"]
        ]
    elif "turns" in row:
        messages = [{"role": "user", "content": t["question"]} for t in row["turns"]]
    else:
        questions = row.get("questions", [row.get("question")])
        messages = [{"role": "user", "content": q} for q in questions]
    if not messages or any(
        m["role"] not in {"user", "assistant", "system"}
        or not isinstance(m["content"], str) or not m["content"].strip()
        for m in messages
    ):
        raise ValueError("missing or invalid input messages")
    return messages


def historical_expected(row):
    result = {k: row[k] for k in ("expected", "memory_expectation") if k in row}
    if "turns" in row:
        result["turns"] = [
            {"turn": i + 1, "expected": turn["expected"]}
            for i, turn in enumerate(row["turns"]) if "expected" in turn
        ]
    return result


class Collector:
    def __init__(self):
        self.candidates = {}
        self.extracted_records = 0

    def add(self, messages, context, evidence, flags=()):
        identity = {"conversation": messages, "required_context": context}
        key = digest(identity)
        self.extracted_records += 1
        item = self.candidates.setdefault(key, {
            "candidate_id": "history-" + key[:20],
            **identity,
            "review_status": "pending",
            "reviewed_expected": None,
            "split": "development_candidate",
            "replay_ready": False,
            "migration_flags": [],
            "evidence": [],
        })
        item["migration_flags"] = sorted(set(item["migration_flags"]) | set(flags))
        if evidence not in item["evidence"]:
            item["evidence"].append(evidence)
        return item


def fixture_context(row):
    return {key: row[key] for key in ("seed", "confirm") if key in row}


def extract_fixture(collector, root, path):
    rows = read_json(path)
    if not isinstance(rows, list):
        raise ValueError("fixture must be a list")
    for index, row in enumerate(rows):
        flags = ["policy_and_protocol_review_required"]
        if path.name in {"acceptance_cases.json", "acceptance_rules_cases.json"}:
            flags.append("legacy_simulated_company_policy")
        if path.name == "acceptance_cases.json":
            if row["id"] in {"memory_accept", "memory_reject"}:
                flags.append("legacy_memory_confirmation_protocol")
            if row["id"] == "historical_policy":
                flags.append("legacy_expense_date_policy_selection")
        collector.add(normalize_input(row), fixture_context(row), {
            "kind": "test_fixture",
            "source": source_ref(root, path, f"/{index}"),
            "original_case_id": row["id"],
            "historical_expected": historical_expected(row),
            "observed_outcome": "not_run",
        }, flags)
    return len(rows)


def extract_history(collector, root, path):
    data = read_json(path)
    rows = data if isinstance(data, list) else [data]
    count = 0
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or not row.get("turns"):
            continue
        pointer = f"/{index}" if isinstance(data, list) else ""
        messages = normalize_input(row)
        passed, failed = row.get("passed"), row.get("failed")
        outcome = "unknown"
        if row.get("failure") or row.get("errors"):
            outcome = "reported_run_error_requires_triage"
        elif isinstance(failed, list) and failed:
            outcome = "reported_failed_checks"
        elif isinstance(failed, list) and isinstance(passed, list) and passed:
            outcome = "reported_checks_passed"
        # Keep raw answers and checkpoint data in their original local evidence.
        evidence = {
            "kind": "historical_run",
            "source": source_ref(root, path, pointer),
            "original_case_id": row.get("id"),
            "historical_expected": historical_expected(row),
            "observed_outcome": outcome,
            "outcome_scope": "whole_run_not_each_turn",
            "checks": {
                "passed_count": len(passed) if isinstance(passed, list) else None,
                "failed_count": len(failed) if isinstance(failed, list) else None,
                "source": source_ref(root, path, pointer),
            },
            "answer_refs": [
                source_ref(root, path, f"{pointer}/turns/{i}/answer")
                for i, t in enumerate(row["turns"]) if "answer" in t
            ],
        }
        # Unknown harness setup must not be treated as an empty initial state.
        context = {
            "harness_family": re.sub(r"-[0-9a-f]{32}$", "", path.parent.name)
            if "browser-" in path.parent.name else "legacy_acceptance",
            "setup_status": "requires_source_harness_review",
            "historical_answers": "source_references_not_gold",
        }
        collector.add(messages, context, evidence, [
            "policy_and_protocol_review_required",
            "harness_setup_and_fault_injection_review_required",
            "historical_run_not_production_traffic",
        ])
        count += 1
    return count


def attach_reviews(collector, root, paths):
    unmatched = []
    for path in paths:
        for case_id, review in read_json(path).items():
            targets = [
                item for item in collector.candidates.values()
                if any(e.get("original_case_id") == case_id for e in item["evidence"])
            ]
            ref = source_ref(root, path, "/" + case_id.replace("~", "~0").replace("/", "~1"))
            annotation = {
                "kind": "historical_review",
                "source": ref,
                "historical_label": review,
                "association": "case_id_only_not_verified_run",
            }
            for target in targets:
                target["evidence"].append(annotation)
            if not targets:
                unmatched.append(ref)
    return unmatched


def extract_rag_report(collector, root, path):
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    count = 0
    for start, line in enumerate(lines):
        if not line.startswith("问题："):
            continue
        end = start + 1
        while end < len(lines) and lines[end].strip():
            end += 1
        question = "\n".join([line.removeprefix("问题："), *lines[start + 1:end]])
        section_end = end
        while section_end < len(lines) and not lines[section_end].startswith("#"):
            section_end += 1
        ref = source_ref(root, path)
        ref.update({"line_start": start + 1, "line_end": section_end})
        collector.add([{"role": "user", "content": question}], {
            "entrypoint": "explain_policy",
            "setup_status": "facts_supplied_by_harness_requires_review",
        }, {
            "kind": "acceptance_report",
            "source": ref,
            "observed_outcome": "mixed_history_read_section",
            "historical_narrative": "\n".join(lines[end:section_end]).strip(),
            "historical_expected": {},
        }, ["not_chat_e2e", "policy_and_protocol_review_required"])
        count += 1
    if not count:
        raise ValueError("no report questions found")
    return count


def build(root, manifest):
    collector = Collector()
    inventory = []
    errors = []
    unmatched_patterns = [
        pattern for pattern in manifest["history_patterns"] if not list(root.glob(pattern))
    ]
    for kind, paths, extractor in (
        ("fixture", [root / p for p in manifest["fixtures"]], extract_fixture),
        ("historical_run", sorted({
            p for pattern in manifest["history_patterns"] for p in root.glob(pattern)
        }), extract_history),
        ("acceptance_report", [root / manifest["rag_report"]], extract_rag_report),
    ):
        for path in paths:
            try:
                # File-level staging prevents malformed sources being partly imported.
                staged = Collector()
                count = extractor(staged, root, path)
                for item in staged.candidates.values():
                    for evidence in item["evidence"]:
                        collector.add(item["conversation"], item["required_context"],
                                      evidence, item["migration_flags"])
                inventory.append({
                    **source_ref(root, path), "kind": kind,
                    "status": "extracted" if count else "unsupported_or_empty",
                    "records": count,
                })
            except (OSError, ValueError, KeyError, TypeError) as exc:
                errors.append({"path": str(path.relative_to(root)), "error": str(exc)})
    review_paths = sorted(root.glob(manifest["review_pattern"]))
    unmatched = attach_reviews(collector, root, review_paths)
    for path in review_paths:
        inventory.append({**source_ref(root, path), "kind": "historical_review",
                          "status": "annotations_only"})
    for path in sorted(root.glob(manifest["holdout_pattern"])):
        inventory.append({
            **source_ref(root, path), "kind": "holdout",
            "status": "excluded_no_content_import",
        })
    extracted_paths = {row["path"] for row in inventory}
    for pattern in manifest["report_patterns"]:
        for path in sorted(root.glob(pattern)):
            ref = source_ref(root, path)
            if ref["path"] not in extracted_paths:
                inventory.append({**ref, "kind": "report",
                                  "status": "inventory_only_requires_manual_extraction"})
                extracted_paths.add(ref["path"])
    candidates = sorted(collector.candidates.values(), key=lambda c: c["candidate_id"])
    summary = {
        "schema_version": 1,
        "scope": "manifest_sources_only_not_exhaustive",
        "extracted_records": collector.extracted_records,
        "unique_candidates": len(candidates),
        "deduplicated_records": collector.extracted_records - len(candidates),
        "user_turns_in_unique_candidates": sum(
            m["role"] == "user" for c in candidates for m in c["conversation"]
        ),
        "review_pending": len(candidates),
        "reviewed_gold": 0,
        "inventory_status_counts": dict(Counter(row["status"] for row in inventory)),
        "historical_run_outcomes": dict(Counter(
            e["observed_outcome"] for c in candidates for e in c["evidence"]
            if e["kind"] == "historical_run"
        )),
        "unmatched_review_refs": unmatched,
        "unmatched_history_patterns": unmatched_patterns,
        "errors": errors,
        "limitations": [
            "No agent/model/database execution; no current accuracy measurement.",
            "Historical expectations and answers are not current gold labels.",
            "Whole conversations count as cases, not independent turns.",
            "Setup, injected faults and policy versions need human review before replay.",
            "Case-id review links do not establish exact run identity.",
            "Unparsed reports and Python test bodies remain an extraction backlog.",
            "Exact-input deduplication is not semantic deduplication.",
        ],
    }
    return candidates, inventory, summary


def validate_references(root, payload):
    """Check all retained evidence locators against the current source bytes."""
    checked = set()
    cache = {}

    def visit(value):
        if isinstance(value, dict):
            if {"path", "sha256", "pointer"} <= value.keys():
                key = canonical(value)
                if key in checked:
                    return
                path = root / value["path"]
                path.resolve().relative_to(root.resolve())
                if path not in cache:
                    cache[path] = path.read_bytes()
                raw = cache[path]
                if hashlib.sha256(raw).hexdigest() != value["sha256"]:
                    raise ValueError(f"source changed: {value['path']}")
                if value["pointer"]:
                    target = json.loads(raw.decode("utf-8-sig"))
                    for token in value["pointer"].split("/")[1:]:
                        token = token.replace("~1", "/").replace("~0", "~")
                        target = target[int(token)] if isinstance(target, list) else target[token]
                if "line_start" in value:
                    length = len(raw.decode("utf-8-sig").splitlines())
                    if not 1 <= value["line_start"] <= value["line_end"] <= length:
                        raise ValueError(f"invalid source lines: {value['path']}")
                checked.add(key)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    return len(checked)


def write_outputs(output, candidates, inventory, summary):
    output.mkdir(parents=True, exist_ok=True)
    for name, value in (("candidates.jsonl", candidates), ("inventory.jsonl", inventory)):
        (output / name).write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in value),
            encoding="utf-8",
        )
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--manifest", type=Path, default=Path(__file__).with_name("candidate_sources.json"))
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    root = args.root.resolve()
    output = args.output or root / "evals" / "candidates"
    candidates, inventory, summary = build(root, read_json(args.manifest))
    if summary["errors"]:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 1
    summary["validated_source_locators"] = validate_references(root, [candidates, inventory])
    write_outputs(output, candidates, inventory, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

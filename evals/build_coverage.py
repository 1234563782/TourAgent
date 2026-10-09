"""Build a static evidence inventory, never execute tests or promote gold."""

from __future__ import annotations

import argparse
import ast
from collections import Counter, defaultdict
import json
from pathlib import Path

from evals.extract_candidates import canonical, digest, read_json, source_ref, validate_references


def line_ref(root, path, node):
    return {**source_ref(root, path), "line_start": node.lineno, "line_end": node.end_lineno}


def extract_group(root, group):
    path = root / group["path"]
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    if group["environment"] not in {"offline", "isolated_postgres"}:
        raise ValueError("unsupported environment")
    if path.stem.endswith("_live") and group["environment"] != "isolated_postgres":
        raise ValueError("live tests require isolated_postgres")
    found = defaultdict(list)
    for cls in tree.body:
        if isinstance(cls, ast.ClassDef):
            for method in cls.body:
                if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    found[method.name].append((cls, method))
    rows = []
    for name in group["methods"]:
        matches = found[name]
        if len(matches) != 1:
            raise ValueError(f"expected one method: {group['path']}:{name}")
        cls, method = matches[0]
        selector = ".".join((*Path(group["path"]).with_suffix("").parts, cls.name, name))
        assertions = [
            node for node in ast.walk(method)
            if isinstance(node, ast.Assert) or (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr.startswith("assert")
            )
        ]
        rows.append({
            "scenario_id": "engineering-" + digest(selector)[:20],
            "category": group["category"],
            "kind": "existing_engineering_test",
            "selector": selector,
            "environment": group["environment"],
            "preconditions": {
                "harness": "existing test setup, fixtures and mocks; not a natural-language E2E",
                "source": line_ref(root, path, cls),
                "database": "explicit opt-in and isolated schema"
                if group["environment"] == "isolated_postgres" else "no live database",
            },
            "source": line_ref(root, path, method),
            "assertion_refs": [line_ref(root, path, node) for node in
                               sorted(assertions, key=lambda n: (n.lineno, n.col_offset))],
            "execution_status": "not_run",
            "review_status": "pending",
            "reviewed_expected": None,
            "real_model_quality_evidence": False,
        })
    return rows


def recovery_observation(root, path):
    data = read_json(path)
    if not isinstance(data.get("passed"), bool) or not isinstance(data.get("checks"), list):
        raise ValueError(f"unsupported recovery summary: {path.name}")
    if any(not isinstance(check, str) for check in data["checks"]):
        raise ValueError("checks must be strings")
    return {
        "kind": "historical_recovery_batch",
        "category": "recovery_idempotency",
        "source": source_ref(root, path),
        "reported_batch_outcome": "passed" if data["passed"] else "failed",
        "outcome_scope": "whole_batch_not_individual_checks",
        "real_model": data.get("realModel"),
        "checks": [{"description": text, "source": source_ref(root, path, f"/checks/{i}"),
                    "individual_outcome": "unknown"} for i, text in enumerate(data["checks"])],
        "execution_status": "not_run",
    }


def duplicate_groups(candidates):
    grouped = defaultdict(list)
    for item in candidates:
        grouped[canonical(item["conversation"])].append(item["candidate_id"])
    return [
        {"group_id": "same-input-" + digest(key)[:16], "candidate_ids": sorted(ids),
         "reason": "same full conversation; context or provenance differs",
         "decision": "pending_do_not_merge_automatically"}
        for key, ids in sorted(grouped.items()) if len(ids) > 1
    ]


def build(root, manifest, candidates):
    scenarios = [row for group in manifest["groups"] for row in extract_group(root, group)]
    selectors = [row["selector"] for row in scenarios]
    if len(selectors) != len(set(selectors)):
        raise ValueError("duplicate engineering selector")
    observations = [recovery_observation(root, path) for path in
                    sorted(root.glob(manifest["recovery_summary_pattern"]))]
    if not observations:
        raise ValueError("no recovery summaries matched")
    reports = []
    for spec in manifest["reports"]:
        path = root / spec["path"]
        lines = path.read_text(encoding="utf-8-sig").splitlines()
        headings = [i for i, line in enumerate(lines) if line.startswith("## ")]
        for offset, start in enumerate(headings):
            end = headings[offset + 1] if offset + 1 < len(headings) else len(lines)
            reports.append({
                "category": spec["category"], "heading": lines[start],
                "kind": "report_section_reference",
                "source": {**source_ref(root, path), "line_start": start + 1, "line_end": end},
                "status": "linked_not_individually_adjudicated",
            })
    by_method = {row["selector"].rsplit(".", 1)[-1]: row for row in scenarios}
    proposals = []
    for item in manifest["proposals"]:
        basis = by_method[item["basis_method"]]
        proposals.append({
            **item, "kind": "synthetic_proposal", "review_status": "pending",
            "reviewed_expected": None, "replay_ready": False, "execution_status": "not_run",
            "basis": basis["source"],
            "expectation_status": "proposed_contract_requires_human_review_not_gold",
        })
    categories = sorted({row["category"] for row in scenarios})
    matrix = [{
        "category": category,
        "engineering_scenarios": sum(r["category"] == category for r in scenarios),
        "proposed_dialogues": sum(r["category"] == category for r in proposals),
        "current_real_model_evaluation": "not_run",
        "coverage_claim": "selected_evidence_only_not_exhaustive",
    } for category in categories]
    payload = {
        "engineering_scenarios": scenarios,
        "historical_observations": observations,
        "report_links": reports,
        "proposed_cases": proposals,
        "duplicate_review_groups": duplicate_groups(candidates),
        "summary": {
            "schema_version": 1, "scope": "selected_evidence_not_exhaustive",
            "natural_language_candidates_unchanged": len(candidates),
            "engineering_scenarios": len(scenarios),
            "engineering_environment_counts": dict(Counter(r["environment"] for r in scenarios)),
            "historical_recovery_batches": len(observations),
            "historical_batch_outcomes": dict(Counter(r["reported_batch_outcome"] for r in observations)),
            "proposals_pending": len(proposals), "reviewed_gold": 0,
            "matrix": matrix, "unverified_dimensions": manifest["unverified_dimensions"],
            "limitations": [
                "Static AST extraction only; tests, agents and databases were not executed.",
                "Assertion locations are evidence, not independently reviewed gold contracts.",
                "Full source files retain setup/import dependencies; no portable replay fixture is claimed.",
                "Report sections are linked, not automatically classified as product badcases.",
                "Duplicate groups cover exact conversations only, not semantic or prefix duplicates.",
                "Other reports remain in the original inventory backlog.",
            ],
        },
    }
    payload["summary"]["validated_source_locators"] = validate_references(root, payload)
    return payload


def write_outputs(output, payload):
    output.mkdir(parents=True, exist_ok=True)
    for name, rows in payload.items():
        if name != "summary":
            (output / f"{name}.jsonl").write_text(
                "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                encoding="utf-8")
    summary = payload["summary"]
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# Evaluation Coverage Inventory", "",
        "Static evidence only. No current model accuracy or complete coverage is claimed.", "",
        "| Category | Engineering scenarios | Proposed dialogues | Current model run |",
        "| --- | ---: | ---: | --- |",
    ]
    for row in summary["matrix"]:
        lines.append(f"| {row['category']} | {row['engineering_scenarios']} | "
                     f"{row['proposed_dialogues']} | not_run |")
    lines += ["", "## Unverified Dimensions", ""]
    lines += [f"- {text}" for text in summary["unverified_dimensions"]]
    lines += ["", "## Interpretation", ""]
    lines += [f"- {text}" for text in summary["limitations"]]
    lines += ["", "## Rebuild", "", "```powershell",
              r".\.venv\Scripts\python.exe -X utf8 -m evals.build_coverage", "```", ""]
    (output / "coverage.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    root = args.root.resolve()
    manifest = read_json(root / "evals/coverage_sources.json")
    candidates = [json.loads(line) for line in
                  (root / "evals/candidates/candidates.jsonl").read_text(encoding="utf-8").splitlines()]
    payload = build(root, manifest, candidates)
    write_outputs(root / "evals/coverage", payload)
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

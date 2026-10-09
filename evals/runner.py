"""Run deterministic regression evidence for evaluation cases."""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
import unittest
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from evals.dataset import DatasetError, load_cases
from evals.models import CaseResult, EvaluationCase, EvidenceResult


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RESULTS = ROOT / "evals" / "results"


def _detail(result: unittest.TestResult) -> str:
    records = [*result.failures, *result.errors]
    if records:
        return records[0][1][-2000:]
    if result.skipped:
        return "; ".join(reason for _, reason in result.skipped)[:2000]
    return ""


def run_selector(selector: str) -> EvidenceResult:
    stream = io.StringIO()
    started = time.perf_counter()
    suite = unittest.defaultTestLoader.loadTestsFromName(selector)
    result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
    duration_ms = round((time.perf_counter() - started) * 1000)
    if result.errors:
        status = "not_found" if "_FailedTest" in str(result.errors[0][0]) else "error"
    elif result.failures or result.unexpectedSuccesses:
        status = "failed"
    elif result.skipped and len(result.skipped) == result.testsRun:
        status = "skipped"
    else:
        status = "passed"
    return EvidenceResult(
        selector=selector,
        status=status,
        duration_ms=duration_ms,
        detail=_detail(result),
    )


def score_case(case: EvaluationCase, evidence: list[EvidenceResult]) -> CaseResult:
    statuses = {item.status for item in evidence}
    if not evidence:
        status = "unmapped"
    elif statuses & {"failed", "error", "not_found"}:
        status = "failed"
    elif "skipped" in statuses:
        status = "blocked"
    else:
        status = "passed"
    return CaseResult(
        case_id=case.case_id,
        category=case.category,
        case_type=case.case_type,
        severity=case.severity,
        status=status,
        evidence=evidence,
        hard_gate_failed=status == "failed" and case.severity == "critical",
    )


def build_summary(cases: list[EvaluationCase], results: list[CaseResult]) -> dict:
    status_counts = Counter(item.status for item in results)
    by_category: dict[str, Counter] = defaultdict(Counter)
    by_type: dict[str, Counter] = defaultdict(Counter)
    for item in results:
        by_category[item.category][item.status] += 1
        by_type[item.case_type][item.status] += 1
    mapped = sum(item.status != "unmapped" for item in results)
    executed = sum(item.status in {"passed", "failed"} for item in results)
    passed = status_counts["passed"]
    hard_gate_failures = [item.case_id for item in results if item.hard_gate_failed]
    critical_blocked = [
        item.case_id
        for item in results
        if item.severity == "critical" and item.status in {"blocked", "unmapped"}
    ]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "deterministic_test_evidence",
        "case_count": len(cases),
        "mapped_case_count": mapped,
        "mapping_coverage": mapped / len(cases) if cases else 0,
        "executed_case_count": executed,
        "execution_coverage": executed / len(cases) if cases else 0,
        "passed_case_count": passed,
        "evidence_pass_rate": passed / executed if executed else 0,
        "status_counts": dict(status_counts),
        "hard_gate_failures": hard_gate_failures,
        "critical_blocked_cases": critical_blocked,
        "release_gate_passed": not hard_gate_failures and not critical_blocked,
        "by_category": {key: dict(value) for key, value in sorted(by_category.items())},
        "by_case_type": {key: dict(value) for key, value in sorted(by_type.items())},
        "interpretation": (
            "This report executes synthetic regression evidence. It does not measure live-model "
            "language quality or claim direct prompt-level accuracy."
        ),
    }


def write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def run(dataset_dir: Path, mapping_path: Path, output_dir: Path) -> dict:
    cases = load_cases(dataset_dir)
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    unknown = sorted(set(mapping) - {case.case_id for case in cases})
    if unknown:
        raise DatasetError(f"evidence map contains unknown cases: {', '.join(unknown)}")

    cache: dict[str, EvidenceResult] = {}
    results: list[CaseResult] = []
    for case in cases:
        evidence = []
        for selector in mapping.get(case.case_id, []):
            if selector not in cache:
                cache[selector] = run_selector(selector)
            evidence.append(cache[selector].model_copy(deep=True))
        results.append(score_case(case, evidence))

    summary = build_summary(cases, results)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(output_dir / "results.jsonl", (item.model_dump(mode="json") for item in results))
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", type=Path, default=ROOT / "evals")
    parser.add_argument("--mapping", type=Path, default=ROOT / "evals" / "evidence_map.json")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_RESULTS)
    args = parser.parse_args(argv)

    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "tests"))
    try:
        summary = run(args.dataset_dir, args.mapping, args.output_dir)
    except (DatasetError, OSError, json.JSONDecodeError) as exc:
        print(f"evaluation failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if not summary["release_gate_passed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

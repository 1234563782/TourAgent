"""Self-tests for the offline evaluation runner."""

import json
import tempfile
import unittest
from pathlib import Path

from evals.dataset import DatasetError, load_cases
from evals.models import EvidenceResult
from evals.runner import build_summary, score_case


def payload(case_id="case-001"):
    return {
        "case_id": case_id,
        "category": "routing",
        "oracle_type": "state_assertion",
        "case_type": "golden",
        "severity": "high",
        "source": "fixture",
        "expected_outcome": "state_match",
        "conversation": [{"role": "user", "content": "hello"}],
        "expected": {"status": "completed"},
        "tags": [],
    }


class DatasetTests(unittest.TestCase):
    def test_loads_and_rejects_duplicate_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "cases.jsonl"
            path.write_text(json.dumps(payload()) + "\n", encoding="utf-8")
            self.assertEqual(load_cases(root)[0].case_id, "case-001")
            path.write_text(
                json.dumps(payload()) + "\n" + json.dumps(payload()) + "\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(DatasetError, "duplicate case_id"):
                load_cases(root)

    def test_missing_metadata_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value = payload()
            value.pop("severity")
            (root / "cases.jsonl").write_text(json.dumps(value) + "\n", encoding="utf-8")
            with self.assertRaises(DatasetError):
                load_cases(root)


class ScoringTests(unittest.TestCase):
    def setUp(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "cases.jsonl").write_text(json.dumps(payload()) + "\n", encoding="utf-8")
            self.case = load_cases(root)[0]

    def evidence(self, status):
        return EvidenceResult(selector="tests.fixture", status=status, duration_ms=1)

    def test_pass_fail_blocked_and_unmapped(self):
        self.assertEqual(score_case(self.case, [self.evidence("passed")]).status, "passed")
        failed = score_case(self.case, [self.evidence("failed")])
        self.assertEqual(failed.status, "failed")
        self.assertFalse(failed.hard_gate_failed)
        self.assertEqual(score_case(self.case, [self.evidence("skipped")]).status, "blocked")
        self.assertEqual(score_case(self.case, []).status, "unmapped")

    def test_summary_separates_coverage_from_pass_rate(self):
        results = [
            score_case(self.case, [self.evidence("passed")]),
            score_case(self.case.model_copy(update={"case_id": "case-002"}), []),
        ]
        summary = build_summary(
            [self.case, self.case.model_copy(update={"case_id": "case-002"})], results
        )
        self.assertEqual(summary["mapping_coverage"], 0.5)
        self.assertEqual(summary["execution_coverage"], 0.5)
        self.assertEqual(summary["evidence_pass_rate"], 1.0)

    def test_critical_blocked_case_keeps_release_gate_closed(self):
        critical = self.case.model_copy(update={"severity": "critical"})
        summary = build_summary(
            [critical],
            [score_case(critical, [self.evidence("skipped")])],
        )
        self.assertEqual(summary["critical_blocked_cases"], ["case-001"])
        self.assertFalse(summary["release_gate_passed"])


if __name__ == "__main__":
    unittest.main()

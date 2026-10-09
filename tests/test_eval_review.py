"""Annotation approval must not silently become executable gold."""

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evals.extract_candidates import source_ref
from evals.review_candidates import (
    apply_decisions, main, make_queue, read_rows, template, validate_decision, write_rows,
)


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        path = self.root / "evidence.txt"
        path.write_text("evidence\n", encoding="utf-8")
        self.candidate = {
            "candidate_id": "history-example",
            "conversation": [{"role": "user", "content": "question"}],
            "migration_flags": ["old_policy"],
            "evidence": [{"source": source_ref(self.root, path),
                          "historical_expected": {"amount": 420},
                          "observed_outcome": "reported_failed_checks"}],
        }
        self.row = make_queue([self.candidate], [])[0]

    def approved(self):
        return {
            **template(self.row), "decision": "approve", "reviewer": "test reviewer",
            "rationale": "Reviewed against current fixture.", "category": "memory",
            "initial_state": {key: "explicit fixture value" for key in
                              ("identity_isolation", "conversation_state", "memory_state",
                               "clock", "providers", "fault_injection")},
            "policy": {"applicability": "not_applicable", "reason": "Memory-only fixture."},
            "resolved_migration_flags": ["old_policy"],
            "turn_assertions": [{"turn": 1, "must": ["ask"], "must_not": ["write"],
                                 "observable": "state before and after"}],
        }

    def test_pending_is_not_approved(self):
        approved, counts = apply_decisions(self.root, [self.row], [template(self.row)])
        self.assertEqual(approved, [])
        self.assertEqual(counts, {"pending": 1})
        self.assertNotIn("amount", str(template(self.row)))

    def test_approved_annotation_is_still_not_executable(self):
        approved, counts = apply_decisions(self.root, [self.row], [self.approved()])
        self.assertEqual(counts["approve"], 1)
        self.assertFalse(approved[0]["replay_ready"])
        self.assertEqual(approved[0]["execution_status"], "not_run")

    def test_missing_review_requirements_rejected(self):
        for key, value in (
            ("reviewer", ""), ("initial_state", {}), ("turn_assertions", []),
            ("resolved_migration_flags", []), ("policy", {"applicability": "unresolved"}),
            ("candidate_fingerprint", "stale"),
        ):
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_decision(self.root, self.row, {**self.approved(), key: value})

    def test_source_drift_rejected(self):
        (self.root / "evidence.txt").write_text("changed\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "source changed"):
            validate_decision(self.root, self.row, self.approved())

    def test_policy_requires_real_references(self):
        for refs in ([], [{}]):
            with self.subTest(refs=refs), self.assertRaises(ValueError):
                validate_decision(self.root, self.row, {
                    **self.approved(), "policy": {"applicability": "required", "sources": refs}})

    def test_duplicate_unknown_and_duplicate_turn_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate review"):
            apply_decisions(self.root, [self.row], [self.approved(), self.approved()])
        with self.assertRaisesRegex(ValueError, "unknown"):
            apply_decisions(self.root, [self.row], [{**self.approved(), "candidate_id": "unknown"}])
        decision = self.approved()
        decision["turn_assertions"] *= 2
        with self.assertRaisesRegex(ValueError, "duplicate assertion"):
            validate_decision(self.root, self.row, decision)

    def test_rebuild_preserves_decisions_and_input(self):
        candidates_dir = self.root / "evals/candidates"
        coverage_dir = self.root / "evals/coverage"
        candidates_dir.mkdir(parents=True)
        coverage_dir.mkdir(parents=True)
        write_rows(candidates_dir / "candidates.jsonl", [self.candidate])
        write_rows(coverage_dir / "proposed_cases.jsonl", [])
        before = copy.deepcopy(self.candidate)
        with patch("sys.argv", ["review", "--root", str(self.root)]):
            main()
            decisions_path = self.root / "evals/review/decisions.jsonl"
            write_rows(decisions_path, [self.approved()])
            saved = decisions_path.read_bytes()
            main()
            self.assertEqual(saved, decisions_path.read_bytes())
        self.assertEqual(read_rows(candidates_dir / "candidates.jsonl"), [before])
        self.assertEqual(len(read_rows(self.root / "evals/review/approved_annotations.jsonl")), 1)


if __name__ == "__main__":
    unittest.main()

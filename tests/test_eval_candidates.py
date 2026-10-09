"""Offline extraction must preserve provenance without manufacturing gold."""

import json
import tempfile
import unittest
from pathlib import Path

from evals.extract_candidates import (
    Collector, attach_reviews, extract_fixture, extract_history,
    extract_rag_report, build, source_ref, validate_references, read_json,
)


class CandidateExtractionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.collector = Collector()

    def write(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def test_fixture_context_and_expectations_are_not_gold(self):
        path = self.write("cases.json", [{
            "id": "followup", "messages": [
                {"role": "human", "content": "first"},
                {"role": "ai", "content": "which city?"},
                {"role": "human", "content": "Nanjing"},
            ], "expected": {"limit": 420}, "seed": [{"value": "aisle"}],
        }])
        extract_fixture(self.collector, self.root, path)
        item = next(iter(self.collector.candidates.values()))
        self.assertEqual(len(item["conversation"]), 3)
        self.assertEqual(item["conversation"][1]["role"], "assistant")
        self.assertEqual(item["required_context"]["seed"], [{"value": "aisle"}])
        self.assertIsNone(item["reviewed_expected"])
        self.assertFalse(item["replay_ready"])
        self.assertEqual(item["evidence"][0]["source"]["pointer"], "/0")
        self.assertEqual(item["evidence"][0]["historical_expected"]["expected"]["limit"], 420)

    def test_different_seed_and_prior_turns_do_not_merge(self):
        for first, seed in (("a", 1), ("a", 2), ("b", 1)):
            self.collector.add([{"role": "user", "content": first},
                                {"role": "user", "content": "same followup"}],
                               {"seed": seed}, {"kind": "fixture"})
        self.assertEqual(len(self.collector.candidates), 3)

    def test_repeated_runs_preserve_failure_and_success(self):
        for suffix, failed in (("a" * 32, ["bad"]), ("b" * 32, [])):
            path = self.write(f"hotel-flow-browser-{suffix}/summary.json", {
                "passed": ["check"], "failed": failed, "turns": [
                    {"question": "first", "answer": "answer", "checkpoint": {"secret": "not copied"}},
                    {"question": "followup", "answer": "different"},
                ],
            })
            extract_history(self.collector, self.root, path)
        item = next(iter(self.collector.candidates.values()))
        self.assertEqual(len(self.collector.candidates), 1)
        self.assertEqual(len(item["evidence"]), 2)
        self.assertEqual(
            {e["observed_outcome"] for e in item["evidence"]},
            {"reported_failed_checks", "reported_checks_passed"},
        )
        self.assertNotIn("secret", json.dumps(item))
        self.assertEqual(item["evidence"][0]["answer_refs"][1]["pointer"], "/turns/1/answer")

    def test_empty_checks_are_not_pass(self):
        path = self.write("summary.json", {"passed": [], "failed": [],
                                          "turns": [{"question": "q"}]})
        extract_history(self.collector, self.root, path)
        item = next(iter(self.collector.candidates.values()))
        self.assertEqual(item["evidence"][0]["observed_outcome"], "unknown")

    def test_review_link_is_not_claimed_as_exact_run(self):
        path = self.write("fixture.json", [{"id": "a", "question": "q", "expected": "old"}])
        extract_fixture(self.collector, self.root, path)
        review = self.write("review.json", {"a": {"status": "failed"}, "missing": {}})
        unmatched = attach_reviews(self.collector, self.root, [review])
        item = next(iter(self.collector.candidates.values()))
        self.assertEqual(len(unmatched), 1)
        self.assertEqual(item["evidence"][-1]["association"], "case_id_only_not_verified_run")
        self.assertIsNone(item["reviewed_expected"])

    def test_report_multiline_question_keeps_badcase_narrative(self):
        path = self.root / "report.md"
        path.write_text("### Test\n\n问题：first\nsecond\n\nFailed initially.\n"
                        "Reviewer accepted incomplete answer.\n\n## End\n", encoding="utf-8")
        extract_rag_report(self.collector, self.root, path)
        item = next(iter(self.collector.candidates.values()))
        self.assertEqual(item["conversation"][0]["content"], "first\nsecond")
        self.assertIn("incomplete", item["evidence"][0]["historical_narrative"])
        self.assertEqual(item["evidence"][0]["source"]["line_start"], 3)

    def test_holdout_is_inventory_only(self):
        self.write("tests/a_holdout.json", [{"question": "NEVER IMPORT"}])
        report = self.root / "report.md"
        report.write_text("问题：q\n\nresult\n", encoding="utf-8")
        candidates, inventory, summary = build(self.root, {
            "fixtures": [], "history_patterns": [], "review_pattern": "reviews/*.json",
            "holdout_pattern": "tests/*holdout*.json", "report_patterns": [],
            "rag_report": "report.md",
        })
        self.assertNotIn("NEVER IMPORT", json.dumps(candidates))
        self.assertEqual(summary["inventory_status_counts"]["excluded_no_content_import"], 1)
        self.assertEqual(len(inventory), 2)

    def test_source_hash_changes_and_external_paths_rejected(self):
        path = self.write("a.json", {})
        before = source_ref(self.root, path)
        self.write("a.json", {"changed": True})
        self.assertNotEqual(before["sha256"], source_ref(self.root, path)["sha256"])
        with self.assertRaises(ValueError):
            source_ref(self.root / "nested", path)

    def test_references_validate_pointer_and_reject_source_drift(self):
        path = self.write("a.json", [{"answer": "source text"}])
        ref = source_ref(self.root, path, "/0/answer")
        self.assertEqual(validate_references(self.root, [ref, ref]), 1)
        with self.assertRaises(KeyError):
            validate_references(self.root, [dict(ref, pointer="/0/missing")])
        self.write("a.json", [])
        with self.assertRaises(ValueError):
            validate_references(self.root, [ref])

    def test_run_errors_do_not_count_as_passed_checks(self):
        path = self.write("summary.json", {
            "passed": ["first"], "failed": [], "failure": "later crash",
            "turns": [{"question": "q"}],
        })
        extract_history(self.collector, self.root, path)
        item = next(iter(self.collector.candidates.values()))
        self.assertEqual(item["evidence"][0]["observed_outcome"],
                         "reported_run_error_requires_triage")

    def test_actual_manifest_rebuild_is_deterministic_and_isolated(self):
        root = Path(__file__).resolve().parents[1]
        manifest = read_json(root / "evals/candidate_sources.json")
        first = build(root, manifest)
        self.assertEqual(first, build(root, manifest))
        candidates, inventory, summary = first
        self.assertFalse(summary["errors"])
        self.assertFalse(summary["unmatched_history_patterns"])
        self.assertGreater(validate_references(root, [candidates, inventory]), 0)
        self.assertTrue(all(c["reviewed_expected"] is None for c in candidates))
        self.assertTrue(all(c["review_status"] == "pending" for c in candidates))
        self.assertEqual(len({c["candidate_id"] for c in candidates}), len(candidates))
        from evals.dataset import load_cases
        self.assertFalse(
            {c.case_id for c in load_cases(root / "evals")}
            & {c["candidate_id"] for c in candidates}
        )


if __name__ == "__main__":
    unittest.main()

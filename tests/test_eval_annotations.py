"""Content drafts must bind existing cases without creating approval."""

import copy
from pathlib import Path
import unittest

from evals.annotate_candidates import build_drafts
from evals.extract_candidates import read_json, validate_references
from evals.review_candidates import read_rows


ROOT = Path(__file__).resolve().parents[1]


class AnnotationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.specs = read_json(ROOT / "evals/annotation_specs.json")
        cls.candidates = read_rows(ROOT / "evals/candidates/candidates.jsonl")
        cls.proposals = read_rows(ROOT / "evals/coverage/proposed_cases.jsonl")

    def build(self, specs=None):
        return build_drafts(ROOT, specs or self.specs, self.candidates, self.proposals)

    def test_all_drafts_are_complete_pending_and_reproducible(self):
        before = copy.deepcopy(self.candidates)
        drafts = self.build()
        self.assertEqual(len(drafts), 15)
        self.assertEqual(drafts, self.build())
        self.assertEqual(before, self.candidates)
        for draft in drafts:
            self.assertEqual(draft["annotation"]["decision"], "pending")
            self.assertEqual(draft["annotation"]["reviewer"], "")
            self.assertFalse(draft["replay_ready"])
            self.assertEqual(draft["real_model_execution"], "not_run")
            self.assertTrue(draft["annotation"]["turn_assertions"])
            self.assertTrue(draft["engineering_basis"])
        self.assertGreater(validate_references(ROOT, drafts), 0)

    def test_exact_fixture_binding_not_historical_run_name(self):
        drafts = self.build()
        historical = [d for d in drafts if d["candidate_id"].startswith("history-")]
        self.assertEqual(len(historical), 7)
        for row in historical:
            self.assertTrue(any(s["path"] == "tests/preference_dialogue_cases.json"
                                for s in row["candidate_sources"]))

    def test_turn_count_and_duplicate_spec_rejected(self):
        specs = copy.deepcopy(self.specs)
        specs["cases"][0]["turns"] = []
        with self.assertRaisesRegex(ValueError, "turn count"):
            self.build(specs)
        specs = copy.deepcopy(self.specs)
        specs["cases"].append(specs["cases"][0])
        with self.assertRaisesRegex(ValueError, "duplicate annotation"):
            self.build(specs)

    def test_policy_sources_and_untriggered_branch_are_explicit(self):
        drafts = {d["candidate_id"]: d for d in self.build()}
        policy = drafts["gap-season-missing-date"]["annotation"]["policy"]
        self.assertEqual(policy["version"], "2026-project-v1")
        self.assertEqual(len(policy["sources"]), 5)
        self.assertTrue(drafts["gap-stale-result"]["open_questions"])
        self.assertTrue(drafts["gap-ambiguous-target"]["open_questions"])
        self.assertIn("610", str(drafts["gap-review-rejection"]["open_questions"]))


if __name__ == "__main__":
    unittest.main()

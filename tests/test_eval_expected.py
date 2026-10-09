"""Full expected-outcome drafts remain traceable and non-executable."""

from collections import defaultdict
from pathlib import Path
import unittest

from evals.extract_candidates import validate_references
from evals.prepare_expected import build
from evals.review_candidates import make_queue, read_rows


ROOT = Path(__file__).resolve().parents[1]


class ExpectedDraftTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.drafts = build(ROOT)
        cls.queue = make_queue(
            read_rows(ROOT / "evals/candidates/candidates.jsonl"),
            read_rows(ROOT / "evals/coverage/proposed_cases.jsonl"),
        )

    def test_every_candidate_and_user_turn_is_covered(self):
        self.assertEqual(len(self.drafts), 88)
        self.assertEqual({x["candidate_id"] for x in self.drafts},
                         {x["candidate_id"] for x in self.queue})
        for draft in self.drafts:
            turns = {i for i, m in enumerate(draft["conversation"], 1) if m["role"] == "user"}
            assertions = draft["annotation"]["turn_assertions"]
            self.assertEqual({a["turn"] for a in assertions}, turns)
            self.assertTrue(all(a["must"] and a["must_not"] and a["observable"] for a in assertions))
            self.assertEqual(draft["annotation"]["decision"], "pending")
            self.assertFalse(draft["replay_ready"])
            self.assertEqual(draft["real_model_execution"], "not_run")

    def test_existing_drafts_preserved_and_structured_source_not_gold(self):
        earlier = {d["candidate_id"]: d for d in read_rows(ROOT / "evals/review/annotation_drafts.jsonl")}
        current = {d["candidate_id"]: d for d in self.drafts}
        self.assertEqual(len(earlier), 15)
        for key, draft in earlier.items():
            self.assertEqual(current[key]["annotation"], draft["annotation"])
        turns = current["history-553317cbd14e4232e3bc"]["annotation"]["turn_assertions"]
        self.assertEqual(turns[1]["source_expectation_not_gold"][0]["scenario"]["amount"], "623")
        self.assertIn("假设", str(turns[1]["must_not"]))

    def test_exact_duplicate_groups_and_sources(self):
        groups = defaultdict(list)
        for row in self.queue:
            groups[tuple((m["role"], m["content"]) for m in row["conversation"])].append(row["candidate_id"])
        self.assertEqual(sum(len(ids) > 1 for ids in groups.values()), 16)
        self.assertEqual(sum(bool(d["duplicate_ids"]) for d in self.drafts), 32)
        self.assertGreater(validate_references(ROOT, self.drafts), 0)


if __name__ == "__main__":
    unittest.main()

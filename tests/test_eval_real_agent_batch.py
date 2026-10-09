import tempfile
import unittest
from pathlib import Path

from langchain_core.messages import AIMessage, ToolMessage

from evals.real_agent_batch import describe_turn, load_candidates, summarize


class BatchReportTests(unittest.TestCase):
    def test_describe_turn_reports_observation_without_claiming_correctness(self):
        class Memory:
            def preference_snapshot(self):
                return {"items": []}

        state = {
            "messages": [
                ToolMessage(name="consult_policy", content='{"status":"unavailable"}', tool_call_id="1"),
                AIMessage(content="No policy evidence available."),
            ],
            "tasks": {"a": {"status": "pending", "subject_id": "s", "revision": 2}},
        }
        turn = describe_turn(state, Memory())
        self.assertEqual(turn["status"], "completed")
        self.assertEqual(turn["tool_statuses_seen"], [{"name": "consult_policy", "status": "unavailable"}])
        self.assertEqual(turn["tasks"][0]["revision"], 2)

    def test_summary_never_computes_quality_pass_rate(self):
        summary = summarize([{}, {}], [{"observed_turns": 1, "turns": [{"status": "completed"}]}])
        self.assertEqual(summary["remaining"], 1)
        self.assertIsNone(summary["quality_pass_rate"])

    def test_duplicate_candidate_ids_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rows.jsonl"
            path.write_text('{"candidate_id":"x"}\n{"candidate_id":"x"}\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate"):
                load_candidates(path)


if __name__ == "__main__":
    unittest.main()

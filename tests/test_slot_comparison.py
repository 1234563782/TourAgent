"""对照实验计分、轨迹和隔离边界的离线验证。"""

from types import SimpleNamespace
import unittest

from langchain_core.messages import AIMessage, HumanMessage

from scripts.compare_slot_strategies import MeteredModel, direct_extract, score, summarize
from tests.test_slot_protocol import RawModel, reply


class FakeModel:
    def __init__(self, response):
        self.response = response

    def bind_tools(self, tools, **kwargs):
        return self

    def invoke(self, messages):
        return self.response


class SlotComparisonTests(unittest.TestCase):
    def test_direct_does_not_mark_reviewed_or_mutate_input(self):
        model = RawModel([
            reply("TurnRoute", {"tasks": [{"operation": "new_task", "intent": "policy_check"}]}),
            reply("ExpenseExtraction", {"patches": [{"task_index": 0, "expense": {"kind": "hotel"}}]}),
        ])
        current = {}
        result = direct_extract(model, [HumanMessage(content="核对住宿费")], current,
                                SimpleNamespace(slot_context_limit=16, slot_task_limit=4))
        self.assertEqual(current, {})
        self.assertTrue(result["experimental_unreviewed"])
        self.assertEqual(result["slots"]["expense"]["validation"], "pending_context_review")

    def test_missing_null_is_not_explicit_retraction(self):
        result = {"status": "ok", "tasks": [{"intent": "policy_check",
                                            "subject": {"values": {}}}]}
        outcome = score(result, [{"intent": "policy_check",
                                 "values": {"special_approval": None}}])
        self.assertFalse(outcome["passed"])
        self.assertTrue(outcome["wrong_accepted"])

    def test_extra_tasks_fail(self):
        self.assertFalse(score({"status": "ok", "tasks": [
            {"intent": "other"}, {"intent": "other"}]},
            [{"intent": "other", "values": {}}])["passed"])

    def test_forbidden_inference_fails(self):
        result = {"status": "ok", "tasks": [{"intent": "policy_check",
                                            "subject": {"values": {"amount": "800"}}}]}
        self.assertFalse(score(result, [{"intent": "policy_check", "values": {},
                                         "absent": ["amount"]}])["passed"])

    def test_clarification_is_not_wrong_acceptance_or_success(self):
        result = score({"status": "clarify"}, [{"intent": "other", "values": {}}])
        self.assertFalse(result["passed"])
        self.assertFalse(result["wrong_accepted"])

    def test_meter_records_usage(self):
        events = []
        response = AIMessage(content="", usage_metadata={
            "input_tokens": 5, "output_tokens": 3, "total_tokens": 8})
        MeteredModel(FakeModel(response), events, "test").invoke([])
        self.assertEqual(events[0]["usage"]["total_tokens"], 8)

    def test_summary_does_not_invent_cost(self):
        rows = [{"strategy": "direct", "passed": True, "wrong_accepted": False,
                 "checks": 1, "correct": 1, "seconds": 2, "result": {"status": "ok"},
                 "events": [{"usage": None}]}]
        summary = summarize(rows)["direct"]
        self.assertIsNone(summary["money_cost"])
        self.assertEqual(summary["usage_missing_calls"], 1)

    def test_hypothesis_cannot_be_silently_dropped(self):
        result = {"status": "ok", "tasks": [{"intent": "policy_check",
                                           "subject": {"values": {}}}]}
        self.assertFalse(score(result, [{"intent": "policy_check", "values": {},
                                        "scenario": {"amount": "800"}}])["passed"])

    def test_new_subject_with_same_values_is_not_an_update(self):
        result = {"status": "ok", "tasks": [{"intent": "policy_check",
                    "subject": {"id": "new", "revision": 1, "values": {}}}]}
        self.assertFalse(score(result, [{"intent": "policy_check", "values": {},
                                         "same_subject": True}],
                               {"expense": {"id": "old", "revision": 1}})["passed"])


if __name__ == "__main__":
    unittest.main()

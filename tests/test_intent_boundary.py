"""Input boundaries between routing, domain extraction and semantic review."""

import json
import unittest

from tests.test_slot_protocol import SlotProtocolTests, reply


class IntentBoundaryTests(unittest.TestCase):
    def test_other_has_only_route_and_review_no_domain_schema(self):
        harness = SlotProtocolTests()
        result = harness.run_proposal([
            reply("TurnRoute", {"tasks": [harness.new("other")]})
        ])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(harness.model.calls), 1)
        schema = harness.model.bindings[0][0][0]
        self.assertNotIn("expense_date", json.dumps(schema))
        self.assertNotIn("check_in", json.dumps(schema))
        review = json.loads(harness.reviewer.calls[0][-1].content)
        self.assertEqual(review["task_schema"], {})
        self.assertEqual(review["proposal"]["tasks"][0]["intent"], "other")
        self.assertEqual(result["slots"], {})

    def test_extractor_receives_assigned_goal_not_full_route_or_result_inventory(self):
        harness = SlotProtocolTests()
        route = {"tasks": [harness.new("policy_check", "核对住宿"),
                           harness.new("plan_trip", "查南京酒店")]}
        result = harness.run_proposal([
            reply("TurnRoute", route),
            reply("TripExtraction", {"patches": [harness.hotel(1)]}),
            reply("ExpenseExtraction", {"patches": [harness.expense(0)]}),
        ], text="查南京酒店并核对住宿319元")
        self.assertEqual(result["status"], "ok")
        for call, expected in zip(harness.model.calls[1:], (1, 0)):
            payload = json.loads(call[-1].content)
            self.assertEqual(payload["extraction_tasks"][0]["task_index"], expected)
            self.assertEqual(payload["extraction_tasks"][0]["intent"], route["tasks"][expected]["intent"])
            self.assertNotIn("route", payload)
            self.assertNotIn("available_results", payload)
            self.assertNotIn("pending_tasks", payload)
            self.assertIn("conversation", payload)
            self.assertIn("current_slots", payload)
        review = json.loads(harness.reviewer.calls[0][-1].content)
        self.assertEqual(set(review["task_schema"]), {"trip", "expense"})
        self.assertEqual([task["intent"] for task in review["resolved_tasks"]],
                         ["policy_check", "plan_trip"])
        self.assertIn("proposed_slots", review)


if __name__ == "__main__":
    unittest.main()

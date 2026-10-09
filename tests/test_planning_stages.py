"""Candidate selection failures must preserve real retrieval results, never fabricate."""

import json
import unittest
from datetime import date
from dataclasses import replace
from unittest.mock import Mock

from business_travel.contracts import Candidate, SearchResult, TransportRequest, CombinedRequest, HotelRequest
from business_travel.plan_checks import render_draft
from business_travel.planning import run_planner
from tests import test_travel_graph as fixtures
from tests.test_travel_graph import ScriptedModel, call, select_result


class PlanningStageTests(unittest.TestCase):
    setUp = fixtures.GraphTests.setUp
    tearDown = fixtures.GraphTests.tearDown

    def candidate(self, ident="out", **changes):
        return Candidate(**{"id": ident, "kind": "flight", "label": "Synthetic",
                            "source": "fixture", "observed_at": "2099-09-01",
                            "request_date": date(2099, 9, 8), "departure": "2099-09-08 09:00",
                            "arrival": "2099-09-08 11:00", **changes})

    def run_plan(self, model, provider=None, trip=None, info=None):
        provider = provider or Mock()
        if not provider.search.side_effect:
            provider.search.return_value = SearchResult(status="ok", candidates=[self.candidate()])
        return run_planner(model, trip or TransportRequest(**self.trip), provider, info or Mock(),
                           self.policy, self.memory, self.settings)

    def test_selection_timeout_and_binding_failure_preserve_candidates(self):
        for operation in ("invoke", "bind_tools"):
            with self.subTest(operation=operation):
                model = Mock()
                model.bind_tools.return_value = model
                getattr(model, operation).side_effect = TimeoutError("secret-provider-url")
                draft = self.run_plan(model)
                self.assertEqual(draft["status"], "incomplete")
                self.assertEqual(draft["selection_status"], "error")
                self.assertEqual(draft["alternatives"][0]["id"], "out")
                self.assertEqual(draft["selected"], [])
                self.assertIn("已查到", render_draft(draft))
                self.assertNotIn("secret-provider-url", str(draft))

    def test_unknown_duplicate_and_same_component_ids_rejected(self):
        for ids in (["invented"], ["out", "out"], ["out", "second"]):
            provider = Mock()
            provider.search.side_effect = [SearchResult(status="ok", candidates=[
                self.candidate(), self.candidate("second")])]
            draft = self.run_plan(ScriptedModel([call("PlanSelection", {"selected_ids": ids}, "bad")] * 2),
                                  provider)
            self.assertEqual(draft["selection_status"], "invalid_selection")
            self.assertEqual(draft["selected"], [])
            self.assertEqual(len(draft["alternatives"]), 2)

    def test_selection_budget_failure_is_explicit_and_preserves_all_candidates(self):
        self.settings = replace(self.settings, context_role_input_tokens={"planner": 1024})
        model = Mock()
        model.bind_tools.return_value = model
        draft = self.run_plan(model)
        model.invoke.assert_not_called()
        self.assertEqual(draft["selection_status"], "context_budget_exceeded")
        self.assertEqual(draft["selected"], [])
        self.assertEqual(draft["alternatives"][0]["id"], "out")
        self.assertIn("预算", render_draft(draft))

    def test_high_speed_request_filters_regular_trains(self):
        provider = Mock()
        provider.search.side_effect = [SearchResult(status="ok", candidates=[
            self.candidate("slow", kind="train", label="K123"),
            self.candidate("fast", kind="train", label="G123")])]
        trip = TransportRequest(**{**self.trip, "transport": "train", "train_service": "high_speed"})
        draft = self.run_plan(ScriptedModel([select_result]), provider, trip)
        self.assertEqual([item["id"] for item in draft["selected"]], ["fast"])

    def test_query_failure_is_visible_and_redacted(self):
        provider = Mock()
        provider.search.side_effect = TimeoutError("secret-key")
        draft = self.run_plan(ScriptedModel([]), provider)
        self.assertEqual(draft["status"], "incomplete")
        self.assertEqual(draft["query_status"][0]["status"], "error")
        self.assertIn("未取得可用候选", render_draft(draft))
        self.assertNotIn("secret-key", str(draft))

    def test_preferences_reach_selector_but_history_is_not_copied(self):
        self.memory.save_preferences([{"type": "seat", "value": "aisle"}])
        def inspect(messages):
            context = json.loads(messages[-1].content)
            self.assertEqual(context["preferences"], [{"type": "seat", "value": "aisle"}])
            self.assertNotIn("trip_history", context)
            return select_result(messages)
        self.assertEqual(self.run_plan(ScriptedModel([inspect]))["selection_status"], "selected")

    def test_map_failure_does_not_drop_selected_hotel(self):
        provider = Mock()
        provider.search.side_effect = [
            SearchResult(status="ok", candidates=[self.candidate()]),
            SearchResult(status="ok", candidates=[
                self.candidate("hotel", kind="hotel", address="Synthetic address")])]
        info = Mock()
        info.commute.side_effect = TimeoutError("secret-map-key")
        trip = CombinedRequest(**{**self.trip, "check_in": "2099-09-08", "check_out": "2099-09-09",
                              "meeting_address": "Synthetic meeting"})
        draft = self.run_plan(ScriptedModel([call("PlanSelection", {"selected_ids": ["out", "hotel"]}, "ok")]),
                              provider, trip, info)
        self.assertEqual(len(draft["selected"]), 2)
        self.assertEqual(draft["commute_checks"]["hotel"]["status"], "unavailable")
        self.assertIn("未取得有效估算", render_draft(draft))
        self.assertNotIn("secret-map-key", str(draft))

    def test_sold_out_hotel_cannot_be_selected_by_model(self):
        hotel = self.candidate("hotel", kind="hotel", details={
            "hotel_detail": {"status": "ok", "availability": "sold_out", "rates": []}})
        provider = Mock()
        provider.search.side_effect = [SearchResult(status="ok", candidates=[hotel])]
        trip = HotelRequest(destination="B", check_in="2099-09-08", check_out="2099-09-09")
        draft = self.run_plan(ScriptedModel([call("PlanSelection", {"selected_ids": ["hotel"]}, "bad")] * 2),
                              provider, trip)
        self.assertEqual(draft["selected"], [])
        self.assertEqual(draft["alternatives"][0]["id"], "hotel")
        self.assertIn("本次返回的报价库存均为零", render_draft(draft))


if __name__ == "__main__":
    unittest.main()

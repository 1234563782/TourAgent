"""Candidate selection failures must preserve real retrieval results, never fabricate."""

import json
import threading
import time
import unittest
from datetime import date
from dataclasses import replace
from unittest.mock import Mock, patch

from business_travel.contracts import Candidate, SearchResult, TransportRequest, CombinedRequest, HotelRequest
from business_travel.plan_checks import render_draft, check_timeline
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

    def test_parallel_queries_preserve_stage_order_and_reduce_wall_time(self):
        settings = replace(self.settings, planner_query_mode="parallel")
        provider = Mock()
        calls = []
        lock = threading.Lock()

        def search(kind, trip, inbound=False):
            with lock:
                calls.append((kind, inbound))
            time.sleep(0.04)
            return SearchResult(status="ok", candidates=[self.candidate(
                f"{kind}-{inbound}", kind=kind, details={"inbound": inbound})])

        provider.search.side_effect = search
        trip = CombinedRequest(**{**self.trip, "query_scope": "combined",
                                  "check_in": "2099-09-08", "check_out": "2099-09-09",
                                  "return_date": "2099-09-09", "journey": "round_trip",
                                  "transport": "compare"})
        draft = run_planner(ScriptedModel([select_result]), trip, provider, Mock(),
                            self.policy, self.memory, settings)

        self.assertEqual([(row["kind"], row["inbound"]) for row in draft["query_status"]],
                         trip.search_stages())
        self.assertEqual(len(calls), len(trip.search_stages()))
        self.assertLess(max(row["duration_ms"] for row in draft["query_status"]), 250)

    def test_serial_query_mode_remains_available_for_baseline(self):
        settings = replace(self.settings, planner_query_mode="serial")
        provider = Mock()
        order = []

        def search(kind, trip, inbound=False):
            order.append((kind, inbound))
            return SearchResult(status="ok", candidates=[self.candidate(
                f"{kind}-{inbound}", kind=kind, details={"inbound": inbound})])

        provider.search.side_effect = search
        trip = CombinedRequest(**{**self.trip, "query_scope": "combined",
                                  "check_in": "2099-09-08", "check_out": "2099-09-09",
                                  "return_date": "2099-09-09", "journey": "round_trip",
                                  "transport": "compare"})
        draft = run_planner(ScriptedModel([select_result]), trip, provider, Mock(),
                            self.policy, self.memory, settings)

        self.assertEqual(order, trip.search_stages())
        self.assertEqual([(row["kind"], row["inbound"]) for row in draft["query_status"]],
                         trip.search_stages())

    def test_selection_context_has_no_stored_preferences(self):
        def inspect(messages):
            context = json.loads(messages[-1].content)
            self.assertNotIn("preferences", context)
            self.assertNotIn("trip_history", context)
            return select_result(messages)
        self.assertEqual(self.run_plan(ScriptedModel([inspect]))["selection_status"], "selected")

    def test_input_modes_preserve_full_candidates_and_policy_inputs(self):
        for mode in ("full", "compact"):
            with self.subTest(mode=mode):
                self.settings = replace(self.settings, planner_candidate_mode=mode)
                candidate = self.candidate(details={"inbound": False, "baggage": "23kg"})
                alternative = self.candidate("second")
                original = candidate.model_dump(mode="json")
                provider = Mock()
                provider.search.side_effect = [SearchResult(
                    status="ok", candidates=[candidate, alternative])]
                trip = TransportRequest(**self.trip)

                def inspect(messages):
                    projected = json.loads(messages[-1].content)["candidates"][0]
                    self.assertEqual(projected["timeline_check"], check_timeline([candidate], trip))
                    self.assertEqual(projected["recommendation_check"]["candidate_id"], candidate.id)
                    if mode == "full":
                        self.assertEqual(projected, {
                            **original,
                            "timeline_check": check_timeline([candidate], trip),
                            "recommendation_check": {
                                "candidate_id": candidate.id,
                                "component": "outbound",
                                "status": "unverified",
                                "reason": "planning_policy_not_configured",
                            },
                        })
                    else:
                        self.assertNotIn("source", projected)
                        self.assertNotIn("observed_at", projected)
                        self.assertEqual(projected["details"]["baggage"], "23kg")
                    return call("PlanSelection", {"selected_ids": ["out"]}, "select")

                with patch.object(self.policy, "check_candidate",
                                  return_value={"status": "unverified"}) as check:
                    draft = self.run_plan(ScriptedModel([inspect]), provider, trip)
                self.assertEqual(draft["selected"], [original])
                self.assertEqual(draft["alternatives"], [alternative.model_dump(mode="json")])
                self.assertEqual(check.call_args.args[0].model_dump(mode="json"), original)
                self.assertEqual(candidate.model_dump(mode="json"), original)

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

    def test_available_hotel_is_required_after_model_returns_partial_selection(self):
        transport = self.candidate("out")
        hotel = self.candidate("hotel", kind="hotel", address="Hotel address", details={
            "hotel_detail": {"status": "ok", "availability": "available_snapshot", "rates": []}})
        provider = Mock()
        provider.search.side_effect = [
            SearchResult(status="ok", candidates=[transport]),
            SearchResult(status="ok", candidates=[hotel]),
        ]
        trip = CombinedRequest(**{**self.trip, "query_scope": "combined",
                                  "check_in": "2099-09-08", "check_out": "2099-09-09"})
        model = ScriptedModel([
            call("PlanSelection", {"selected_ids": ["out"]}, "first"),
            call("PlanSelection", {"selected_ids": ["out", "hotel"]}, "second"),
        ])

        draft = self.run_plan(model, provider, trip)

        self.assertEqual([item["id"] for item in draft["selected"]], ["out", "hotel"])
        self.assertEqual(draft["selection_attempts"], 2)
        self.assertEqual(draft["selection_status"], "selected")
        self.assertEqual(draft["missing_components"], [])

    def test_unavailable_hotels_do_not_trigger_required_hotel_reselection(self):
        transport = self.candidate("out")
        hotel = self.candidate("hotel", kind="hotel", details={
            "hotel_detail": {"status": "ok", "availability": "sold_out", "rates": []}})
        provider = Mock()
        provider.search.side_effect = [
            SearchResult(status="ok", candidates=[transport]),
            SearchResult(status="ok", candidates=[hotel]),
        ]
        trip = CombinedRequest(**{**self.trip, "query_scope": "combined",
                                  "check_in": "2099-09-08", "check_out": "2099-09-09"})
        draft = self.run_plan(
            ScriptedModel([call("PlanSelection", {"selected_ids": ["out"]}, "only")]),
            provider, trip,
        )

        self.assertEqual([item["id"] for item in draft["selected"]], ["out"])
        self.assertEqual(draft["selection_attempts"], 1)
        self.assertEqual(draft["missing_components"], ["hotel"])
        self.assertEqual(draft["hotel_inventory_status"], "sold_out")
        self.assertIn("暂无可用库存", render_draft(draft))

    def test_available_hotel_is_code_selected_after_model_still_omits_it(self):
        transport = self.candidate("out")
        hotel = self.candidate("hotel", kind="hotel", address="Hotel address", details={
            "hotel_detail": {"status": "ok", "availability": "available_snapshot", "rates": []}})
        provider = Mock()
        provider.search.side_effect = [
            SearchResult(status="ok", candidates=[transport]),
            SearchResult(status="ok", candidates=[hotel]),
        ]
        trip = CombinedRequest(**{**self.trip, "query_scope": "combined",
                                  "check_in": "2099-09-08", "check_out": "2099-09-09"})
        draft = self.run_plan(
            ScriptedModel([
                call("PlanSelection", {"selected_ids": ["out"]}, "first"),
                call("PlanSelection", {"selected_ids": ["out"]}, "second"),
            ]),
            provider, trip,
        )

        self.assertEqual([item["id"] for item in draft["selected"]], ["out", "hotel"])
        self.assertEqual(draft["selection_status"], "selected_with_hotel_fallback")
        self.assertEqual(draft["hotel_inventory_status"], "available")
        self.assertEqual(draft["missing_components"], [])
        self.assertIn("系统已根据库存和候选排序补选", render_draft(draft))

    def test_unknown_hotel_inventory_is_not_reported_as_no_inventory(self):
        hotel = self.candidate("hotel", kind="hotel", details={
            "hotel_detail": {"status": "error", "availability": "unknown"}})
        provider = Mock()
        provider.search.side_effect = [SearchResult(status="ok", candidates=[hotel])]
        trip = HotelRequest(destination="B", check_in="2099-09-08", check_out="2099-09-09")

        draft = self.run_plan(
            ScriptedModel([call("PlanSelection", {"selected_ids": []}, "only")]),
            provider, trip,
        )

        self.assertEqual(draft["hotel_inventory_status"], "unknown")
        self.assertNotIn("暂无可用库存", render_draft(draft))

    def test_hotels_are_sorted_by_availability_before_candidate_limit(self):
        available = self.candidate("available", kind="hotel", label="Z Hotel", quote={
            "amount": 500, "currency": "CNY", "basis": "night", "status": "indicative"},
            details={"hotel_detail": {"status": "ok", "availability": "available_snapshot"}})
        cheaper_unknown = self.candidate("unknown", kind="hotel", label="A Hotel", quote={
            "amount": 1, "currency": "CNY", "basis": "night", "status": "indicative"})
        sold_out = self.candidate("sold", kind="hotel", label="B Hotel", details={
            "hotel_detail": {"status": "ok", "availability": "sold_out"}})
        provider = Mock()
        provider.search.side_effect = [SearchResult(
            status="ok", candidates=[sold_out, cheaper_unknown, available])]
        trip = HotelRequest(destination="B", check_in="2099-09-08", check_out="2099-09-09")
        self.settings = replace(self.settings, max_candidates=2)

        draft = self.run_plan(
            ScriptedModel([call("PlanSelection", {"selected_ids": ["available"]}, "select")]),
            provider, trip,
        )
        self.assertEqual([item["id"] for item in draft["alternatives"]], ["unknown"])
        self.assertEqual(draft["selected"][0]["id"], "available")

    def test_standard_hotel_is_selected_before_over_standard_hotel(self):
        within = self.candidate("within", kind="hotel", label="Within Hotel", quote={
            "amount": 900, "currency": "CNY", "basis": "night", "status": "indicative"},
            details={"hotel_detail": {"status": "ok", "availability": "available_snapshot"}})
        over = self.candidate("over", kind="hotel", label="Over Hotel", quote={
            "amount": 1755, "currency": "CNY", "basis": "night", "status": "indicative"},
            details={"hotel_detail": {"status": "ok", "availability": "available_snapshot"}})
        provider = Mock()
        provider.search.return_value = SearchResult(status="ok", candidates=[over, within])
        policy = Mock()
        policy.recommendation_profile.return_value = {
            "status": "ok", "transport_standard": "一类", "lodging_standard": "部级",
            "lodging_cap": "1100", "lodging_currency": "CNY", "lodging_unit": "person_day",
            "allowed_transport": {},
        }
        policy.check_candidate.return_value = {"status": "unverified"}
        trip = HotelRequest(destination="北京", check_in="2099-09-08", check_out="2099-09-09")
        draft = run_planner(
            ScriptedModel([
                call("PlanSelection", {"selected_ids": ["over"]}, "over-first"),
                call("PlanSelection", {"selected_ids": ["within"]}, "within-second"),
            ]), trip, provider, Mock(), policy, self.memory, self.settings,
        )

        self.assertEqual([item["id"] for item in draft["selected"]], ["within"])
        self.assertEqual([item["id"] for item in draft["alternatives"]], ["over"])
        checks = {item["candidate_id"]: item for item in draft["recommendation_checks"]}
        self.assertEqual(checks["within"]["status"], "within_standard")
        self.assertEqual(checks["over"]["status"], "over_standard")
        self.assertIn("按当前搜索起价估算", render_draft(draft))

    def test_over_standard_hotel_is_kept_as_backup_when_no_standard_hotel_exists(self):
        over = self.candidate("over", kind="hotel", label="Over Hotel", quote={
            "amount": 1755, "currency": "CNY", "basis": "night", "status": "indicative"},
            details={"hotel_detail": {"status": "ok", "availability": "available_snapshot"}})
        backup = self.candidate("backup", kind="hotel", label="Backup Hotel", quote={
            "amount": 1900, "currency": "CNY", "basis": "night", "status": "indicative"},
            details={"hotel_detail": {"status": "ok", "availability": "available_snapshot"}})
        provider = Mock()
        provider.search.return_value = SearchResult(status="ok", candidates=[over, backup])
        policy = Mock()
        policy.recommendation_profile.return_value = {
            "status": "ok", "transport_standard": "一类", "lodging_standard": "部级",
            "lodging_cap": "1100", "lodging_currency": "CNY", "lodging_unit": "person_day",
            "allowed_transport": {},
        }
        policy.check_candidate.return_value = {"status": "unverified"}
        trip = HotelRequest(destination="北京", check_in="2099-09-08", check_out="2099-09-09")
        draft = run_planner(
            ScriptedModel([call("PlanSelection", {"selected_ids": ["over"]}, "over")]),
            trip, provider, Mock(), policy, self.memory, self.settings,
        )

        self.assertEqual(draft["selected"][0]["id"], "over")
        text = render_draft(draft)
        self.assertIn("暂未找到符合部级的酒店", text)
        self.assertIn("超标准备选", text)

    def test_transport_selection_pool_prefers_allowed_seat_class(self):
        first = self.candidate("first", kind="train", label="G1 / 一等座",
                               transport_mode="high_speed_train", seat_class="一等座")
        second = self.candidate("second", kind="train", label="G2 / 二等座",
                                transport_mode="high_speed_train", seat_class="二等座")
        provider = Mock()
        provider.search.return_value = SearchResult(status="ok", candidates=[first, second])
        policy = Mock()
        policy.recommendation_profile.return_value = {
            "status": "ok", "transport_standard": "三类", "lodging_standard": "其他人员",
            "allowed_transport": {"high_speed_train": ["二等座"]},
            "known_transport_classes": {"high_speed_train": ["商务座", "一等座", "二等座"]},
        }
        policy.check_candidate.return_value = {"status": "unverified"}
        trip = TransportRequest(**{**self.trip, "transport": "train"})
        draft = run_planner(
            ScriptedModel([call("PlanSelection", {"selected_ids": ["second"]}, "second")]),
            trip, provider, Mock(), policy, self.memory, self.settings,
        )

        self.assertEqual(draft["selected"][0]["id"], "second")
        checks = {item["candidate_id"]: item for item in draft["recommendation_checks"]}
        self.assertEqual(checks["first"]["status"], "over_standard")
        self.assertEqual(checks["second"]["status"], "within_standard")


if __name__ == "__main__":
    unittest.main()

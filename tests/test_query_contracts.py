"""Frozen P1 behavior matrix; synthetic model responses do not test language quality."""

import unittest
from datetime import date
from unittest.mock import Mock, patch

from langchain_core.messages import HumanMessage
from pydantic import ValidationError

from business_travel.contracts import (
    Candidate, HotelRequest, SearchResult, TransportRequest, parse_query_request,
)
from business_travel.planning import run_planner
from business_travel.plan_checks import render_draft
from business_travel.policy_pg import PostgresPolicyStore
from business_travel.providers import FlyAIProvider, normalize_flyai
from business_travel.slots import SlotError, SlotTask, TaskClarification, stage_submission
from business_travel.tuniu import ProviderFailure, normalize_tuniu, search_arguments
from tests import test_travel_graph as fixtures
from tests.test_travel_graph import ScriptedModel, select_result, slot_response


class QueryContractTests(unittest.TestCase):
    def test_planning_defaults_to_mainland_business_trip_when_unspecified(self):
        request = parse_query_request({
            "query_scope": "hotel", "destination": "Synthetic City",
            "check_in": "2099-10-03", "check_out": "2099-10-05",
        })
        self.assertTrue(request.mainland_business_trip)
        self.assertEqual(parse_query_request(
            {"query_scope": "hotel", "destination": "Synthetic City",
             "check_in": "2099-10-03", "check_out": "2099-10-05"},
            default_mainland_business_trip=False,
        ).mainland_business_trip, False)

    def test_explicit_mainland_business_trip_value_overrides_default(self):
        request = parse_query_request({
            "query_scope": "hotel", "destination": "Synthetic City",
            "check_in": "2099-10-03", "check_out": "2099-10-05",
            "mainland_business_trip": False,
        })
        self.assertFalse(request.mainland_business_trip)

    def test_hotel_needs_no_transport_facts(self):
        request = parse_query_request({
            "query_scope": "hotel", "destination": "Synthetic City",
            "check_in": "2099-10-03", "check_out": "2099-10-05",
        })
        self.assertIsInstance(request, HotelRequest)
        self.assertIsNone(request.origin)
        self.assertIsNone(request.departure_date)
        self.assertEqual(search_arguments("hotel", request, False), {
            "cityName": "Synthetic City", "checkIn": "2099-10-03", "checkOut": "2099-10-05",
            "adultNum": 1, "childNum": 0, "childAges": [],
        })
        with self.assertRaises(ProviderFailure):
            search_arguments("flight", request, False)

    def test_partial_hotel_reports_only_hotel_fields(self):
        for values, missing in (
            ({}, {"destination", "check_in", "check_out"}),
            ({"destination": "Test"}, {"check_in", "check_out"}),
            ({"destination": "Test", "check_in": "2099-10-03"}, {"check_out"}),
        ):
            with self.subTest(values=values), self.assertRaises(ValidationError) as caught:
                parse_query_request({"query_scope": "hotel", **values})
            self.assertEqual({e["loc"][0] for e in caught.exception.errors()}, missing)

    def test_transport_and_round_trip_required_fields(self):
        values = {"query_scope": "transport", "origin": "A", "destination": "B",
                  "departure_date": "2099-10-03"}
        request = parse_query_request(values)
        self.assertIsInstance(request, TransportRequest)
        self.assertIsNone(request.check_in)
        with self.assertRaises(ValidationError) as caught:
            parse_query_request({**values, "journey": "round_trip"})
        self.assertEqual(caught.exception.errors()[0]["loc"], ("return_date",))
        self.assertEqual(parse_query_request({**values, "journey": "round_trip",
                         "return_date": "2099-10-05"}).return_date, date(2099, 10, 5))

    def test_combined_requires_both_components(self):
        with self.assertRaises(ValidationError) as caught:
            parse_query_request({"query_scope": "combined", "destination": "B"})
        self.assertEqual({e["loc"][0] for e in caught.exception.errors()},
                         {"origin", "departure_date", "check_in", "check_out"})

    def test_missing_scope_never_infers_an_execution_contract(self):
        with self.assertRaises(ValidationError):
            parse_query_request({"destination": "B", "check_in": "2099-10-03",
                                 "check_out": "2099-10-05"})
        with self.assertRaises(ValidationError) as caught:
            parse_query_request({"origin": "A", "destination": "B", "departure_date": "2099-10-03"})
        self.assertEqual(caught.exception.errors()[0]["loc"], ("query_scope",))

    def test_invalid_scope_and_hotel_date_order_rejected(self):
        for patch in ({"query_scope": "invented"}, {"query_scope": []},
                      {"check_out": "2099-10-03"}):
            with self.subTest(patch=patch), self.assertRaises(ValidationError):
                parse_query_request({"query_scope": "hotel", "destination": "B",
                                     "check_in": "2099-10-03", "check_out": "2099-10-05", **patch})

    def test_direct_contract_construction_also_projects_inactive_fields(self):
        request = HotelRequest(destination="B", check_in="2099-10-03", check_out="2099-10-05",
                               departure_date="2000-01-01", return_date="1999-01-01")
        self.assertIsNone(request.departure_date)
        self.assertIsNone(request.return_date)
        self.assertEqual(request.search_stages(), [("hotel", False)])

    def test_component_stage_matrix(self):
        for mode, kinds in (("flight", ["flight"]), ("train", ["train"]),
                            ("compare", ["train", "flight"])):
            for scope in ("transport", "combined"):
                for journey in ("one_way", "round_trip"):
                    with self.subTest(mode=mode, scope=scope, journey=journey):
                        request = parse_query_request({
                            "query_scope": scope, "journey": journey, "transport": mode,
                            "origin": "A", "destination": "B", "departure_date": "2099-10-03",
                            "return_date": "2099-10-05",
                            "check_in": "2099-10-03", "check_out": "2099-10-05",
                        })
                        expected = [(kind, False) for kind in kinds]
                        if journey == "round_trip":
                            expected += [(kind, True) for kind in kinds]
                        if scope == "combined":
                            expected += [("hotel", False)]
                        self.assertEqual(request.search_stages(), expected)

    def test_query_scope_is_classification_and_facts_still_require_quotes(self):
        staged = stage_submission(
            SlotTask(intent="plan_trip", trip={"query_scope": "hotel"}, evidence={}),
            {}, ["查南京的酒店。"])
        self.assertEqual(staged["trip"]["values"]["query_scope"], "hotel")
        self.assertNotIn("query_scope", staged["trip"]["evidence"])
        self.assertEqual(staged["trip"]["provenance"]["query_scope"]["source"], "model_inference")
        with self.assertRaisesRegex(SlotError, "destination:cite_exact_current_message"):
            stage_submission(SlotTask(
                intent="plan_trip",
                trip={"query_scope": "hotel", "destination": "南京"},
                evidence={"destination": {"user_message": 0, "quote": "北京"}},
            ), {}, ["查南京的酒店。"])

    def test_unstated_journey_is_not_stored(self):
        staged = stage_submission(SlotTask(
            intent="plan_trip",
            trip={"query_scope": "transport", "journey": "one_way", "destination": "北京"},
            evidence={"destination": {"user_message": 0, "quote": "北京"}},
        ), {}, ["来一张去北京的票"])
        self.assertNotIn("journey", staged["trip"]["values"])
        self.assertEqual(staged["trip"]["values"]["destination"], "北京")
        stated = stage_submission(SlotTask(
            intent="plan_trip",
            trip={"query_scope": "transport", "journey": "one_way", "destination": "北京"},
            evidence={"destination": {"user_message": 0, "quote": "北京"},
                      "journey": {"user_message": 0, "quote": "单程"}},
        ), {}, ["单程去北京"])
        self.assertEqual(stated["trip"]["values"]["journey"], "one_way")

    def test_new_task_without_scope_asks_instead_of_retrying(self):
        for value in ({}, {"query_scope": None}):
            with self.subTest(value=value), self.assertRaisesRegex(
                    TaskClarification, "请说明要查酒店、交通，还是两者都查"):
                stage_submission(SlotTask(intent="plan_trip", trip=value), {}, ["查酒店"])

    def test_explicit_scope_cannot_be_retracted_to_legacy_default(self):
        current = {"trip": {"id": "t", "domain": "trip", "revision": 1,
                           "values": {"query_scope": "hotel"}, "history": [], "evidence": {}}}
        with self.assertRaisesRegex(SlotError, "query_scope_required"):
            stage_submission(SlotTask(intent="plan_trip", action="update", target_id="t", base_revision=1,
                                      trip={"query_scope": None},
                                      evidence={"query_scope": {"user_message": 0, "quote": "先不确定"}}),
                             current, ["先不确定"])

    def test_hotel_normalizers_work_without_transport_fields(self):
        request = HotelRequest(destination="B", check_in="2099-10-03", check_out="2099-10-05")
        results = [
            normalize_flyai({"status": 0, "data": {"itemList": [
                {"name": "Synthetic", "address": "Test address"}]}}, "hotel", request, 10),
            normalize_tuniu({"success": True, "hotels": [
                {"hotelId": "synthetic", "hotelName": "Synthetic", "lowestPrice": 300}]},
                "hotel", request, 10),
        ]
        for result in results:
            self.assertEqual(result.status, "ok")
            self.assertEqual(result.candidates[0].request_date, request.check_in)
            self.assertFalse(result.candidates[0].inventory_verified)


class QueryGraphTests(unittest.TestCase):
    setUp = fixtures.GraphTests.setUp
    tearDown = fixtures.GraphTests.tearDown
    build = fixtures.GraphTests.build

    def test_hotel_clarification_then_dates_queries_only_hotel(self):
        provider = Mock()
        provider.search.return_value = SearchResult(status="ok", candidates=[Candidate(
            id="hotel-1", kind="hotel", label="Synthetic Hotel", source="fixture",
            observed_at="2099-10-01", request_date=date(2099, 10, 3),
        )])
        graph = self.build([], provider=provider, slots=[
            slot_response({"query_scope": "hotel", "destination": "B"}, "plan_trip",
                          quotes={"query_scope": "酒店"}),
            slot_response({"check_in": "2099-10-03", "check_out": "2099-10-05"},
                          "plan_trip", update=True),
        ])
        first = graph.invoke({"messages": [HumanMessage(content="查B酒店")]}, self.config)
        provider.search.assert_not_called()
        answer = first["messages"][-1].content
        self.assertIn("入住日期", answer)
        self.assertIn("退房日期", answer)
        self.assertNotIn("出发地", answer)
        self.assertNotIn("出发日期", answer)
        second = graph.invoke({"messages": [
            HumanMessage(content="2099-10-03入住，2099-10-05退房")]}, self.config)
        provider.search.assert_called_once()
        self.assertEqual(provider.search.call_args.args[0], "hotel")
        self.assertIsNone(provider.search.call_args.args[1].origin)
        self.assertEqual(second["draft"]["missing_components"], [])
        self.assertEqual(second["draft"]["status"], "draft")
        self.assertEqual(second["draft"]["subject_id"], first["slots"]["trip"]["id"])
        self.assertGreater(second["draft"]["revision"], first["slots"]["trip"]["revision"])
        self.assertNotIn("outbound", second["messages"][-1].content)

    def test_transport_ignores_unrequested_old_hotel_fields(self):
        request = parse_query_request({
            **self.trip, "query_scope": "transport", "check_in": "2000-01-02",
            "check_out": "2000-01-01",
        })
        provider = Mock()
        provider.search.return_value = SearchResult(status="empty")
        draft = run_planner(ScriptedModel([]), request, provider, Mock(),
                            self.policy, self.memory, self.settings)
        self.assertEqual([c.args[0] for c in provider.search.call_args_list], ["flight"])
        self.assertEqual(draft["missing_components"], ["outbound"])
        self.assertIsNone(draft["request"]["check_in"])

    def test_legacy_subject_without_goal_is_preserved_and_clarified(self):
        provider = Mock()
        provider.search.return_value = SearchResult(status="empty")
        graph = self.build([], provider=provider, slots=[slot_response(
            {"departure_date": "2099-09-10"}, "plan_trip", update=True)])
        legacy = {"id": "legacy-trip", "domain": "trip", "revision": 1,
                  "values": dict(self.trip), "evidence": {}, "history": [], "result": None}
        result = graph.invoke({
            "slots": {"trip": legacy},
            "messages": [HumanMessage(content="出发日期改为2099-09-10")],
        }, self.config)
        provider.search.assert_not_called()
        self.assertIn("哪一个事项", result["messages"][-1].content)
        self.assertEqual(result["slots"]["trip"], legacy)
        self.assertFalse(result.get("tasks"))

    def test_hotel_rejects_past_check_in_before_query(self):
        provider = Mock()
        graph = self.build([], provider=provider, slots=[slot_response({
            "query_scope": "hotel", "destination": "B", "check_in": "2000-01-01",
            "check_out": "2000-01-02",
        }, "plan_trip", quotes={"query_scope": "酒店"})])
        result = graph.invoke({"messages": [
            HumanMessage(content="B酒店，2000-01-01入住，2000-01-02退房")]}, self.config)
        provider.search.assert_not_called()
        self.assertIn("入住日期已过去", result["messages"][-1].content)

    def test_round_trip_clarifies_return_date_without_hotel_requirements(self):
        provider = Mock()
        graph = self.build([], provider=provider, slots=[slot_response({
            **self.trip, "query_scope": "transport", "journey": "round_trip",
        }, "plan_trip", quotes={"query_scope": "往返机票", "journey": "往返"})])
        result = graph.invoke({"messages": [HumanMessage(
            content=fixtures.PLAN_TEXT + "，查往返机票")]}, self.config)
        provider.search.assert_not_called()
        self.assertIn("返程日期", result["messages"][-1].content)
        self.assertNotIn("入住日期", result["messages"][-1].content)

    def test_scope_change_replaces_draft_and_keeps_unrelated_stored_facts(self):
        provider = Mock()

        def query(kind, trip, inbound):
            day = trip.check_in if kind == "hotel" else trip.departure_date
            return SearchResult(status="ok", candidates=[Candidate(
                id=kind + str(day), kind=kind, label="Synthetic " + kind, source="fixture",
                observed_at="2099-09-01", request_date=day,
                departure=None if kind == "hotel" else str(day) + " 09:00",
                arrival=None if kind == "hotel" else str(day) + " 11:00",
            )])

        provider.search.side_effect = query
        graph = self.build([], provider=provider, slots=[
            slot_response({**self.trip, "query_scope": "combined",
                           "check_in": "2099-09-08", "check_out": "2099-09-10"},
                          "plan_trip", quotes={"query_scope": "交通和酒店"}),
            slot_response({"query_scope": "hotel"}, "plan_trip",
                          quotes={"query_scope": "只查酒店"}, update=True),
        ])
        first = graph.invoke({"messages": [HumanMessage(content=fixtures.PLAN_TEXT
            + "，交通和酒店，2099-09-08入住2099-09-10退房")]}, self.config)
        second = graph.invoke({"messages": [HumanMessage(content="改成只查酒店")]}, self.config)
        self.assertEqual([c.args[0] for c in provider.search.call_args_list], ["flight", "hotel", "hotel"])
        self.assertNotEqual(first["draft"]["id"], second["draft"]["id"])
        self.assertGreater(second["draft"]["revision"], first["draft"]["revision"])
        self.assertEqual(second["slots"]["trip"]["values"]["origin"], self.trip["origin"])
        self.assertIsNone(second["draft"]["request"]["origin"])
        self.assertNotIn("origin", second["draft"]["fact_context"]["fields"])
        self.assertEqual(second["draft"]["missing_components"], [])
        self.assertEqual([c["kind"] for c in second["draft"]["selected"]], ["hotel"])

    def test_incomplete_query_clears_previous_draft(self):
        provider = Mock()
        provider.search.return_value = SearchResult(status="empty")
        graph = self.build([], provider=provider, slots=[
            slot_response({**self.trip, "query_scope": "transport"}, "plan_trip",
                          quotes={"query_scope": "交通"}),
            slot_response({"query_scope": "combined"}, "plan_trip",
                          quotes={"query_scope": "交通和酒店"}, update=True),
        ])
        graph.invoke({"messages": [HumanMessage(content=fixtures.PLAN_TEXT + "查交通")]}, self.config)
        result = graph.invoke({"messages": [HumanMessage(content="改查交通和酒店")]}, self.config)
        provider.search.assert_called_once()
        self.assertIsNone(result["draft"])
        self.assertIn("入住日期", result["messages"][-1].content)
        self.assertIsNone(result["slots"]["trip"]["result"])

    def test_no_grade_does_not_block_hotel_retrieval(self):
        store = object.__new__(PostgresPolicyStore)
        store.check = Mock(return_value={"status": "insufficient_information",
                                        "missing_fields": ["employee_grade"], "approved": False})
        provider = Mock()
        provider.search.return_value = SearchResult(status="ok", candidates=[Candidate(
            id="h", kind="hotel", label="Synthetic", observed_at="2099-10-01", request_date="2099-10-03",
        )])
        request = HotelRequest(destination="B", check_in="2099-10-03", check_out="2099-10-04")
        draft = run_planner(ScriptedModel([select_result]), request, provider, Mock(),
                            store, self.memory, self.settings)
        provider.search.assert_called_once()
        self.assertEqual(draft["selected"][0]["id"], "h")
        self.assertEqual(draft["policy_checks"][0]["status"], "insufficient_information")
        self.assertIsNone(store.check.call_args.args[0].employee_grade)
        self.assertEqual(store.check.call_args.args[0].expense_date, request.check_in)

    def test_empty_and_failure_have_different_answers_and_no_selection(self):
        request = HotelRequest(destination="B", check_in="2099-10-03", check_out="2099-10-05")
        answers = {}
        for status in ("empty", "error", "unavailable"):
            provider = Mock()
            provider.search.return_value = SearchResult(status=status)
            model = Mock()
            draft = run_planner(model, request, provider, Mock(), self.policy, self.memory, self.settings)
            model.bind_tools.assert_not_called()
            self.assertEqual(draft["missing_components"], ["hotel"])
            self.assertEqual(draft["status"], "incomplete")
            self.assertEqual(draft["selection_attempts"], 0)
            self.assertGreaterEqual(draft["query_status"][0]["duration_ms"], 0)
            self.assertGreaterEqual(draft["timing_ms"]["total"], 0)
            answers[status] = render_draft(draft)
        self.assertIn("查询成功", answers["empty"])
        for status in ("error", "unavailable"):
            self.assertIn("查询失败或服务不可用", answers[status])
            self.assertNotIn("查询成功", answers[status])

    def test_failed_response_or_wrong_component_cannot_enter_selection(self):
        request = HotelRequest(destination="B", check_in="2099-10-03", check_out="2099-10-05")
        for status, kind in (("error", "hotel"), ("ok", "flight")):
            provider = Mock()
            provider.search.return_value = SearchResult(status=status, candidates=[Candidate(
                id="unexpected", kind=kind, label="Synthetic", observed_at="2099-10-01",
                request_date=request.check_in,
            )])
            model = Mock()
            draft = run_planner(model, request, provider, Mock(), self.policy, self.memory, self.settings)
            model.bind_tools.assert_not_called()
            self.assertEqual(draft["selected"], [])
            self.assertEqual(draft["alternatives"], [])

    def test_flyai_rejects_unrequested_component_before_subprocess(self):
        request = HotelRequest(destination="B", check_in="2099-10-03", check_out="2099-10-05")
        with patch("business_travel.providers.subprocess.run") as execute:
            result = FlyAIProvider(self.settings).search("flight", request)
        execute.assert_not_called()
        self.assertEqual(result.error_code, "unrequested_search_component")

    def test_failed_selection_preserves_stage_timing_and_attempt_count(self):
        request = HotelRequest(destination="B", check_in="2099-10-03", check_out="2099-10-05")
        provider = Mock()
        provider.search.return_value = SearchResult(status="ok", candidates=[Candidate(
            id="h", kind="hotel", label="Synthetic", observed_at="2099-10-01", request_date=request.check_in,
        )])
        model = Mock()
        model.bind_tools.side_effect = TimeoutError("credential-bearing-url")
        draft = run_planner(model, request, provider, Mock(), self.policy, self.memory, self.settings)
        self.assertEqual(draft["selection_attempts"], 1)
        self.assertEqual(draft["selection_status"], "error")
        self.assertGreaterEqual(draft["timing_ms"]["selection"], 0)
        self.assertGreaterEqual(draft["timing_ms"]["checks"], 0)
        self.assertNotIn("credential-bearing-url", str(draft))


if __name__ == "__main__":
    unittest.main()

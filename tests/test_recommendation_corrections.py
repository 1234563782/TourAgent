"""Regressions from the real recommendation output, without vendor traffic."""

import copy
import json
import unittest
from pathlib import Path
from unittest.mock import Mock

from langchain_core.messages import HumanMessage

from business_travel.contracts import Candidate, SearchResult, parse_query_request
from business_travel.policy_rules import ExpenseFacts
from business_travel.university_policy import UniversityRules, check_university_expense
from business_travel.plan_checks import render_draft
from business_travel.planning import _recommendation_check
from business_travel.tuniu import hotel_detail_arguments, search_arguments
from tests.test_plan_presentation import draft, main_text
from tests import test_travel_graph as fixtures


class RecommendationCorrectionTests(unittest.TestCase):
    def test_single_guest_defaults_reach_both_supplier_requests(self):
        values = dict(query_scope="hotel", destination="北京", check_in="2099-10-12",
                      check_out="2099-10-14")
        original = copy.deepcopy(values)
        trip = parse_query_request(values)
        hotel = Candidate(id="hotel", kind="hotel", label="Hotel", observed_at="2099-10-01",
                          request_date=trip.check_in, details={"hotelId": 1})
        search = search_arguments("hotel", trip, False)
        detail = hotel_detail_arguments(hotel, trip)
        self.assertEqual((trip.room_occupants, trip.hotel_rooms, trip.hotel_adults,
                          trip.hotel_child_ages), (1, 1, 1, []))
        self.assertEqual((search["adultNum"], search["childNum"]), (1, 0))
        self.assertEqual((detail["roomNum"], detail["adultNum"], detail["childNum"]), (1, 1, 0))
        self.assertEqual(values, original)

    def test_group_or_partial_party_is_not_replaced_with_single_guest(self):
        for party in ({"room_occupants": 2}, {"hotel_adults": 3},
                      {"hotel_rooms": 2, "hotel_adults": 3, "hotel_child_ages": [6]}):
            with self.subTest(party=party):
                trip = parse_query_request(dict(query_scope="hotel", destination="北京",
                    check_in="2099-10-12", check_out="2099-10-14", **party))
                for field, value in party.items():
                    self.assertEqual(getattr(trip, field), value)
                if "room_occupants" not in party:
                    self.assertIsNone(trip.room_occupants)

    def test_default_switch_preserves_supplier_default_baseline(self):
        trip = parse_query_request(dict(query_scope="hotel", destination="北京",
            check_in="2099-10-12", check_out="2099-10-14"), default_single_guest=False)
        self.assertIsNone(trip.hotel_adults)
        self.assertIsNone(trip.room_occupants)
        self.assertNotIn("adultNum", search_arguments("hotel", trip, False))

    def test_estimate_main_output_and_explanations_at_end(self):
        data = draft()
        data["request"].update(transport="train", academic_title="院士")
        data["recommendation_profile"] = {
            "status": "ok", "transport_standard": "一类", "lodging_standard": "部级",
            "lodging_cap": "1100", "lodging_currency": "CNY", "lodging_unit": "person_day",
            "allowed_transport": {"high_speed_train": ["商务座", "一等座", "二等座"],
                                  "regular_train": ["软卧"], "flight": ["经济舱"]}}
        data["recommendation_checks"] = [
            {"candidate_id": "internal-hotel", "status": "within_standard", "cap": "1100"}]
        data["query_defaults"] = {"mainland_business_trip": True, "hotel_rooms": 1,
                                  "hotel_adults": 1, "hotel_child_ages": []}
        data["assumptions"] = ["未明确说明是否为大陆因公出差，本次按默认口径处理"]
        text = render_draft(data)
        main = main_text(text)
        self.assertIn("按当前搜索起价估算", main)
        self.assertNotIn("标准匹配：符合", main)
        self.assertNotIn("不能确认合规", main)
        self.assertNotIn("逐晚含税价格", main)
        self.assertNotIn("默认处理", main)
        self.assertNotIn("飞机可乘", main)
        self.assertIn("1100元/人·天", main)
        self.assertLess(main.index("交通参考"), main.index("差旅标准参考"))
        self.assertIn("不能确认合规", text)
        self.assertIn("是否大陆因公出差：是（默认）", text)
        self.assertIn("房间数：1（默认）", text)
        self.assertGreater(text.index("查看本次查询条件"), text.index("住宿参考"))

    def test_unmapped_train_seat_is_unknown_rather_than_over_standard(self):
        profile = {"status": "ok", "transport_standard": "一类",
                   "allowed_transport": {"high_speed_train": ["商务座", "一等座", "二等座"]},
                   "known_transport_classes": {"high_speed_train": ["商务座", "一等座", "二等座"]}}
        for seat in ("一等卧", "二等卧", "无座"):
            with self.subTest(seat=seat):
                candidate = Candidate(id="train", kind="train", label="D10 / " + seat,
                    observed_at="2099-10-01", request_date="2099-10-12",
                    transport_mode="high_speed_train", seat_class=seat)
                check = _recommendation_check(candidate, profile)
                self.assertEqual(check["status"], "unknown")
                self.assertEqual(check["reason"], "transport_class_mapping_required")

    def test_room_offer_shown_without_inventing_nightly_price_or_stock(self):
        data = draft()
        rates = data["selected"][2]["details"]["hotel_detail"]["rates"]
        rates[0].update(bed_type="双床", currency="CNY")
        rates.insert(0, {**rates[0], "room_name": "售罄房型", "amount": "1", "remaining_count": 0})
        original = copy.deepcopy(data)
        main = main_text(render_draft(data))
        for term in ("房型参考：标准房", "床型：双床", "早餐/餐食：不含早餐", "房型参考报价：300 CNY"):
            self.assertIn(term, main)
        self.assertNotIn("售罄房型", main)
        self.assertNotIn("元/晚", main)
        self.assertEqual(data, original)
        for rate in rates:
            rate["remaining_count"] = None
        self.assertNotIn("房型参考：", main_text(render_draft(data)))

    def test_formal_policy_also_keeps_unmapped_seats_unverified(self):
        path = Path(__file__).resolve().parents[1] / "policies/hfut-2026/reviewed-rules.json"
        rules = UniversityRules.model_validate(json.loads(path.read_text(encoding="utf-8")))
        for seat, title, expected in (("一等卧", "院士", "insufficient_information"),
                                      ("商务座", "正高级", "over_standard"),
                                      ("商务座", "院士", "within_checked_rules")):
            with self.subTest(seat=seat, title=title):
                facts = ExpenseFacts(kind="transport", expense_date="2026-10-18", destination="北京",
                    mainland_business_trip=True, academic_title=title,
                    transport_mode="high_speed_train", seat_class=seat)
                result = check_university_expense(facts, rules, [])
                self.assertEqual(result["status"], expected)
                self.assertFalse(result["approved"])


class RecommendationDefaultGraphTests(unittest.TestCase):
    setUp = fixtures.GraphTests.setUp
    tearDown = fixtures.GraphTests.tearDown
    build = fixtures.GraphTests.build

    def test_defaults_reach_queries_without_becoming_user_evidence(self):
        provider = Mock()
        provider.search.return_value = SearchResult(status="empty")
        values = dict(destination="北京", check_in="2099-10-12", check_out="2099-10-14")
        graph = self.build([], provider=provider, slots=[
            fixtures.slot_response(values, "plan_trip", scope="hotel")])
        result = graph.invoke({"messages": [HumanMessage(content="北京2099-10-12入住2099-10-14退房查酒店")]},
                              self.config)
        request = provider.search.call_args.args[1]
        self.assertEqual((request.hotel_rooms, request.hotel_adults, request.hotel_child_ages), (1, 1, []))
        self.assertEqual(result["draft"]["query_defaults"]["hotel_adults"], 1)
        self.assertNotIn("hotel_adults", result["slots"]["trip"]["values"])
        self.assertNotIn("hotel_adults", result["draft"]["fact_context"]["fields"])
        self.assertIn("房间数：1（默认）", result["messages"][-1].content)

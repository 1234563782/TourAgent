"""Bounded read-only hotel enrichment; fixtures do not contact the supplier."""

import asyncio
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

from business_travel.contracts import HotelRequest, TransportRequest
from business_travel.plan_checks import render_hotel_detail
from business_travel.slots import TripPatch, _snapshot
from business_travel.slot_protocol import TurnRoute, protocol_tool
from business_travel.context_budget import encoded_size
from langchain_core.utils.function_calling import convert_to_openai_tool
from business_travel.tuniu import (
    TuniuProvider, ProviderFailure, hotel_detail_arguments, normalize_hotel_detail,
    normalize_tuniu, search_arguments,
)
from tests.test_tuniu import settings
from business_travel.settings import Settings


TRIP = HotelRequest(destination="Synthetic City", check_in="2099-10-12", check_out="2099-10-14")


def search_payload():
    return {"success": True, "hotels": [
        {"hotelId": i, "hotelName": f"Hotel {i}", "lowestPrice": 200 + i} for i in range(1, 5)]}


def detail_payload(hotel_id=1, count=3, price="358.50"):
    return {"hotelId": hotel_id, "displayHint": "SECRET_INSTRUCTION", "roomTypes": [{
        "roomTypeName": "Standard", "bedType": "Twin", "ratePlans": [{
            "ratePlanName": "Flexible", "rmbPrices": price, "count": count,
            "mealText": "Breakfast", "cancelDesc": "See platform deadline",
            "preBookParam": "SECRET_BOOKING", "vendorRatePlanId": "SECRET_VENDOR",
        }]}]}


class HotelDetailTests(unittest.TestCase):
    def candidate(self):
        return normalize_tuniu(search_payload(), "hotel", TRIP, 4).candidates[0]

    def normalize(self, payload=None, limit=4):
        return normalize_hotel_detail(payload or detail_payload(),
                                      hotel_detail_arguments(self.candidate(), TRIP), limit)

    def query(self, detail=None, **changes):
        with patch("business_travel.tuniu.call_search", new_callable=AsyncMock,
                   return_value=search_payload()), patch(
                       "business_travel.tuniu.call_hotel_detail", new_callable=AsyncMock) as call:
            call.side_effect = detail or (
                lambda url, token, args, timeout, auth: detail_payload(args["hotelId"]))
            result = TuniuProvider(settings(tuniu_access_token="fixture-secret", **changes)).search("hotel", TRIP)
        return result, call

    def test_explicit_occupancy_and_no_policy_inference(self):
        trip = TRIP.model_copy(update={"room_occupants": 2})
        self.assertNotIn("adultNum", search_arguments("hotel", trip, False))
        self.assertEqual(self.normalize()["supplier_default_fields"], ["roomNum", "adultNum", "childNum"])
        trip = HotelRequest(**{**TRIP.model_dump(), "hotel_rooms": 2, "hotel_adults": 3, "hotel_child_ages": [6, 9]})
        args = hotel_detail_arguments(self.candidate(), trip)
        self.assertEqual((args["roomNum"], args["adultNum"], args["childNum"], args["childAges"]),
                         (2, 3, 2, [6, 9]))
        self.assertNotIn("roomNum", search_arguments("hotel", trip, False))
        self.assertEqual(search_arguments("hotel", trip, False)["adultNum"], 3)

    def test_occupancy_updates_null_and_scope_projection(self):
        for payload in ({"hotel_rooms": True}, {"hotel_adults": "2"}, {"hotel_child_ages": [True]},
                        {"hotel_child_ages": [-1]}):
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                TripPatch(**payload)
        self.assertIsNone(TripPatch(hotel_adults=None).model_dump(exclude_unset=True)["hotel_adults"])
        trip = TransportRequest(origin="A", destination="B", departure_date="2099-10-12",
                                hotel_rooms=2, hotel_adults=3, hotel_child_ages=[6])
        self.assertIsNone(trip.hotel_rooms)
        self.assertIsNone(trip.hotel_child_ages)

    def test_detail_whitelist_and_price_uncertainty(self):
        data = self.normalize()
        self.assertEqual(data["rates"][0]["amount"], "358.50")
        self.assertFalse(data["date_verified"])
        self.assertFalse(data["inventory_verified"])
        self.assertEqual(data["rates"][0]["basis"], "unknown")
        self.assertTrue(data["rates"][0]["taxes_confirmed"])
        self.assertNotIn("SECRET", json.dumps(data))

    def test_identity_mismatch_and_explicit_failure_rejected(self):
        for payload in (detail_payload(2), detail_payload(True), {**detail_payload(), "success": False},
                        {**detail_payload(), "roomTypes": {}}):
            with self.subTest(payload=payload), self.assertRaises(ProviderFailure):
                self.normalize(payload)
        candidate = self.candidate()
        for value in (None, True, {}, 0, "invented"):
            candidate.details["hotelId"] = value
            with self.assertRaises(ProviderFailure):
                hotel_detail_arguments(candidate, TRIP)

    def test_inventory_unknown_is_not_sold_out(self):
        for value, expected in ((0, "sold_out"), (2, "available_snapshot"), (None, "unknown"),
                                (True, "unknown"), ("3", "unknown"), (-1, "unknown")):
            with self.subTest(value=value):
                self.assertEqual(self.normalize(detail_payload(count=value))["availability"], expected)
        self.assertEqual(self.normalize({"hotelId": 1, "roomTypes": []})["availability"], "unknown")
        malformed = detail_payload(count=0)
        malformed["roomTypes"].append(None)
        self.assertEqual(self.normalize(malformed)["availability"], "unknown")

    def test_bad_prices_stay_unknown(self):
        for price in ("NaN", "Infinity", "-1", "300起", None, {}, True):
            with self.subTest(price=price):
                self.assertIsNone(self.normalize(detail_payload(price=price))["rates"][0]["amount"])

    def test_limits_and_inventory_aggregation_before_truncation(self):
        payload = detail_payload(count=0)
        payload["roomTypes"] += detail_payload(count=5)["roomTypes"]
        data = self.normalize(payload, limit=1)
        self.assertTrue(data["rates_truncated"])
        self.assertEqual(len(data["rates"]), 1)
        self.assertEqual(data["rates"][0]["remaining_count"], 5)
        self.assertEqual(data["availability"], "available_snapshot")
        result, call = self.query(tuniu_hotel_detail_limit=2)
        self.assertEqual(call.await_count, 2)
        self.assertEqual(result.candidates[2].details["hotel_detail"]["status"], "not_queried")
        self.assertEqual(len(result.candidates), 4)
        self.assertTrue(all(not c.date_verified and not c.inventory_verified for c in result.candidates))
        self.assertEqual(result.candidates[0].quote.amount, 201)

    def test_disabled_details_make_no_call(self):
        result, call = self.query(tuniu_hotel_detail_limit=0)
        call.assert_not_awaited()
        self.assertEqual(result.status, "ok")

    def test_timeout_preserves_search_and_completed_details(self):
        async def slow(url, token, args, *rest):
            if args["hotelId"] == 1:
                return detail_payload(1)
            await asyncio.sleep(1)
        result, call = self.query(slow, tuniu_hotel_detail_timeout=0.01, tuniu_hotel_detail_budget=0.02)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.candidates[0].details["hotel_detail"]["status"], "ok")
        self.assertEqual(result.candidates[1].details["hotel_detail"]["error_code"], "tuniu_timeout")
        self.assertEqual(len(result.candidates), 4)
        self.assertLessEqual(call.await_count, 3)

    def test_auth_failure_stops_further_calls_and_does_not_leak(self):
        result, call = self.query(ProviderFailure("tuniu_authorization_required"))
        self.assertEqual(call.await_count, 1)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.candidates[1].details["hotel_detail"]["status"], "not_queried")
        self.assertNotIn("fixture-secret", result.model_dump_json())

    def test_unexpected_error_preserves_candidates_without_raw_exception(self):
        result, _ = self.query(RuntimeError("SECRET_FROM_VENDOR"))
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.candidates[0].details["hotel_detail"]["error_code"], "tuniu_query_failed")
        self.assertNotIn("SECRET", result.model_dump_json())

    def test_display_preserves_unknowns_and_discloses_defaults(self):
        candidate = self.candidate()
        candidate.details["hotel_detail"] = self.normalize()
        text = "\n".join(render_hotel_detail(candidate.model_dump(mode="json")))
        for phrase in ("供应商默认", "非用户已确认", "含税；每晚/全程口径待确认", "Standard", "Breakfast", "自行"):
            self.assertIn(phrase, text)
        for status, phrase in (("error", "查询失败"), ("not_queried", "尚未查询")):
            candidate.details["hotel_detail"] = {"status": status}
            self.assertIn(phrase, "\n".join(render_hotel_detail(candidate.model_dump())))

    def test_configurable_bounds(self):
        for key, value in (("TUNIU_HOTEL_DETAIL_LIMIT", "-1"), ("TUNIU_HOTEL_RATE_LIMIT", "0"),
                           ("TUNIU_HOTEL_DETAIL_TIMEOUT", "nan"), ("TUNIU_HOTEL_DETAIL_BUDGET", "inf")):
            with self.subTest(key=key), patch.dict(os.environ, {"TRAVEL_LOAD_DOTENV": "0", key: value}, clear=True):
                with self.assertRaises(ValueError):
                    Settings.from_env()
        with patch.dict(os.environ, {"TRAVEL_LOAD_DOTENV": "0", "TUNIU_HOTEL_DETAIL_TIMEOUT": "1.5"}, clear=True):
            self.assertEqual(Settings.from_env().tuniu_hotel_detail_timeout, 1.5)

    def test_truncated_terms_are_disclosed(self):
        payload = detail_payload()
        payload["roomTypes"][0]["ratePlans"][0]["cancelDesc"] = "x" * 501
        candidate = self.candidate()
        candidate.details["hotel_detail"] = self.normalize(payload)
        rate = candidate.details["hotel_detail"]["rates"][0]
        self.assertEqual(rate["truncated_fields"], ["cancellation"])
        self.assertEqual(len(rate["cancellation"]), 500)
        self.assertIn("部分文字已截断", "\n".join(render_hotel_detail(candidate.model_dump())))

    def test_snapshot_dedup_preserves_every_subject_and_conflicting_version(self):
        active = {"id": "active", "domain": "trip", "revision": 2,
                  "values": {"hotel_adults": 3}, "evidence": {"hotel_adults": {"quote": "three adults"}}}
        older = {**active, "id": "other", "revision": 1}
        current = {"trip": active, "subjects": {"active": active, "other": older}}
        snapshot = _snapshot(current)
        self.assertEqual(snapshot["trip"], active)
        self.assertEqual(snapshot["subjects"], {"other": older})
        self.assertEqual(len(current["subjects"]), 2)
        current["subjects"]["active"] = {**active, "revision": 1}
        self.assertEqual(_snapshot(current)["subjects"]["active"]["revision"], 1)

    def test_shared_descriptions_preserve_constraints_and_definitions(self):
        original = convert_to_openai_tool(TurnRoute)
        compact = protocol_tool(TurnRoute)
        descriptions = []

        def constraints(value):
            if isinstance(value, dict):
                if isinstance(value.get("description"), str):
                    descriptions.append(value["description"])
                return {k: constraints(v) for k, v in value.items() if k != "description"}
            if isinstance(value, list):
                return [constraints(v) for v in value]
            return value

        expected = constraints(original)
        source_descriptions = list(descriptions)
        self.assertEqual(constraints(compact), expected)
        wire = json.dumps(compact, ensure_ascii=False)
        for description in source_descriptions:
            self.assertIn(json.dumps(description, ensure_ascii=False)[1:-1], wire)
        self.assertLess(encoded_size(compact), encoded_size(original))
        self.assertEqual(convert_to_openai_tool(TurnRoute), original)


if __name__ == "__main__":
    unittest.main()

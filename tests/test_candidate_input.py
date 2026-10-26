"""Selection projections remove redundancy, not business facts or original data."""

import json
import unittest

from business_travel.candidate_input import candidate_selection_input
from business_travel.contracts import Candidate


class CandidateInputTests(unittest.TestCase):
    def candidate(self, kind="flight", **changes):
        return Candidate(**{
            "id": "fixture-id", "kind": kind, "label": "Fixture",
            "source": "tuniu", "observed_at": "2026-10-13T11:25:00+00:00",
            "request_date": "2026-10-15",
            "quote": {"amount": "450", "currency": "CNY", "basis": "ticket",
                      "status": "indicative", "taxes_confirmed": True},
            "date_verified": True, "inventory_verified": False, **changes,
        })

    def test_flight_preserves_selection_facts_and_original(self):
        candidate = self.candidate(
            label="MU2842", transport_mode="flight", seat_class="Economy",
            departure="2026-10-15 21:50", arrival="2026-10-15 23:45",
            details={"inbound": True, "airlineCompany": "Fixture airline",
                     "departureAirport": "Daxing", "arrivalAirport": "Lukou",
                     "cabinClass": "Economy", "remainingSeats": "9",
                     "base_price": "310", "tax": "140"})
        original = candidate.model_dump(mode="json")
        projected = candidate_selection_input(candidate, "compact")
        for key in ("source", "observed_at", "address", "url", "transport_mode"):
            self.assertNotIn(key, projected)
        self.assertNotIn("cabinClass", projected["details"])
        for key in ("id", "label", "quote", "request_date", "departure", "arrival",
                    "seat_class", "date_verified", "inventory_verified"):
            self.assertEqual(projected[key], original[key])
        self.assertEqual(projected["details"], {
            key: value for key, value in original["details"].items() if key != "cabinClass"})
        self.assertEqual(candidate.model_dump(mode="json"), original)
        self.assertLess(len(json.dumps(projected)), len(json.dumps(original)))

    def test_train_preserves_zero_stock_and_unconfirmed_date(self):
        candidate = self.candidate(
            kind="train", label="G547 / Second", seat_class="Second",
            transport_mode="high_speed_train", date_verified=False,
            details={"inbound": False, "seat_type": "Second", "remaining_seats": 0,
                     "departStationName": "Beijing South", "destStationName": "Nanjing South"})
        projected = candidate_selection_input(candidate, "compact")
        self.assertNotIn("seat_type", projected["details"])
        self.assertEqual(projected["details"]["remaining_seats"], 0)
        self.assertFalse(projected["details"]["inbound"])
        self.assertFalse(projected["date_verified"])
        self.assertEqual(projected["transport_mode"], "high_speed_train")

    def test_hotel_preserves_rates_uncertainty_and_requested_dates(self):
        candidate = self.candidate(
            kind="hotel", date_verified=False,
            quote={"amount": "1619", "currency": "CNY", "basis": "unknown",
                   "status": "indicative", "taxes_confirmed": True},
            details={"inbound": False, "hotelId": 123, "brandName": "",
                     "check_in": "2026-10-14", "check_out": "2026-10-15",
                     "hotel_detail": {
                         "status": "ok", "observed_at": "2026-10-13",
                         "requested": {"hotelId": 123, "checkIn": "2026-10-13",
                                       "checkOut": "2026-10-15"},
                         "date_verified": False, "inventory_verified": False,
                         "supplier_default_fields": ["roomNum", "adultNum", "childNum"],
                         "availability": "available_snapshot", "returned_rate_count": 28,
                         "rates_truncated": True, "incomplete_response": False,
                         "rates": [
                             {"amount": "1619.0", "bed_type": "Twin", "meal": "No breakfast",
                              "cancellation": "Cancellation incurs full 1619.0 charge.",
                              "remaining_count": 9, "basis": "unknown",
                              "truncated_fields": []},
                             {"amount": "2019.0", "meal": "Two breakfasts",
                              "truncated_fields": ["cancellation"]}]}})
        original = candidate.model_dump(mode="json")
        projected = candidate_selection_input(candidate, "compact")
        details = projected["details"]
        self.assertNotIn("hotelId", details)
        self.assertNotIn("brandName", details)
        self.assertNotIn("inbound", details)
        detail = details["hotel_detail"]
        self.assertEqual(detail["requested"], {"checkIn": "2026-10-13", "checkOut": "2026-10-15"})
        self.assertNotIn("observed_at", detail)
        self.assertNotIn("date_verified", detail)
        self.assertNotIn("inventory_verified", detail)
        self.assertEqual(detail["returned_rate_count"], 28)
        self.assertTrue(detail["rates_truncated"])
        self.assertFalse(detail["incomplete_response"])
        self.assertEqual(detail["supplier_default_fields"], ["roomNum", "adultNum", "childNum"])
        self.assertEqual(len(detail["rates"]), 2)
        self.assertNotIn("truncated_fields", detail["rates"][0])
        self.assertEqual(detail["rates"][1]["truncated_fields"], ["cancellation"])
        self.assertEqual(detail["rates"][0]["cancellation"],
                         original["details"]["hotel_detail"]["rates"][0]["cancellation"])
        self.assertEqual(candidate.model_dump(mode="json"), original)

    def test_unknown_and_conflicting_facts_are_not_discarded(self):
        candidate = self.candidate(
            seat_class="Economy", url="https://example.com/fixture",
            details={"cabinClass": "Business", "baggage": "23kg", "connections": [],
                     "hotel_detail": {"status": "not_queried", "reason": "detail_limit"}})
        projected = candidate_selection_input(candidate, "compact")
        self.assertEqual(projected["details"], candidate.details)
        self.assertEqual(projected["url"], candidate.url)

    def test_empty_rates_conflicting_verification_and_missing_quote_remain(self):
        candidate = self.candidate(kind="hotel", quote={},
                                   details={"hotel_detail": {
                                       "status": "ok", "availability": "unknown", "rates": [],
                                       "date_verified": False, "inventory_verified": True}})
        projected = candidate_selection_input(candidate, "compact")
        self.assertEqual(projected["quote"], candidate.model_dump(mode="json")["quote"])
        self.assertIsNone(projected["quote"]["amount"])
        self.assertEqual(projected["details"]["hotel_detail"]["rates"], [])
        self.assertFalse(projected["details"]["hotel_detail"]["date_verified"])
        self.assertTrue(projected["details"]["hotel_detail"]["inventory_verified"])

    def test_full_mode_is_exact_and_invalid_mode_is_rejected(self):
        candidate = self.candidate()
        self.assertEqual(candidate_selection_input(candidate, "full"), candidate.model_dump(mode="json"))
        with self.assertRaises(ValueError):
            candidate_selection_input(candidate, "invalid")


if __name__ == "__main__":
    unittest.main()

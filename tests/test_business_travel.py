"""Offline contract/provider tests. Fixtures are explicitly synthetic."""

import json
import subprocess
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from business_travel.contracts import Candidate, PolicyRules, Quote, TransportRequest, CombinedRequest, check_candidate
from business_travel.memory import TravelMemory
from business_travel.policy import PolicyStore
from business_travel.providers import FlyAIProvider, InformationProvider, normalize_flyai, safe_url
from business_travel.settings import Settings


def make_settings(root):
    return Settings(api_key="", model="", base_url="", node_path="", flyai_cli_path="",
                    storage_dir=root / "new", legacy_memory_dir=root / "old")


def make_trip(**changes):
    return TransportRequest(origin="Beijing", destination="Shanghai", departure_date="2099-09-08", **changes)


def make_combined(**changes):
    return CombinedRequest(origin="Beijing", destination="Shanghai", departure_date="2099-09-08", **changes)


def flight_payload(price="370.00", dep="2099-09-08 09:00:00"):
    return {"status": 0, "data": {"itemList": [{
        "ticketPrice": price, "jumpUrl": "https://example.com/booking",
        "journeys": [{"segments": [{"depDateTime": dep, "arrDateTime": "2099-09-08 11:00:00",
                                    "marketingTransportNo": "TEST001"}]}],
    }]}}


class ContractTests(unittest.TestCase):
    def test_dates(self):
        for values in ({"return_date": "2099-09-07"}, {"check_in": "2099-09-08"},
                       {"check_in": "2099-09-08", "check_out": "2099-09-08"}):
            with self.assertRaises(ValueError):
                (make_combined if "check_in" in values else make_trip)(**values)

    def test_missing_price(self):
        result = normalize_flyai(flight_payload(None), "flight", make_trip(), 10)
        self.assertEqual(result.candidates[0].quote.status, "missing")
        self.assertIsNone(result.candidates[0].quote.amount)

    def test_price_and_date(self):
        result = normalize_flyai(flight_payload(), "flight", make_trip(), 10)
        candidate = result.candidates[0]
        self.assertEqual(candidate.quote.amount, Decimal("370"))
        self.assertIsNone(candidate.quote.currency)
        self.assertFalse(candidate.inventory_verified)
        self.assertTrue(candidate.date_verified)
        self.assertTrue(candidate.quote.taxes_confirmed)

    def test_wrong_date_not_reused(self):
        self.assertEqual(normalize_flyai(flight_payload(dep="2099-09-07 09:00:00"),
                                        "flight", make_trip(), 10).status, "empty")

    def test_empty_and_error(self):
        self.assertEqual(normalize_flyai({"status": 0, "data": {"itemList": []}},
                                        "flight", make_trip(), 10).status, "empty")
        self.assertEqual(normalize_flyai({"status": 403}, "flight", make_trip(), 10).status, "error")

    def test_hotel_is_not_room_quote(self):
        result = normalize_flyai({"status": 0, "data": {"itemList": [
            {"name": "Synthetic Hotel", "price": "500", "detailUrl": "https://example.com"}]}},
            "hotel", make_combined(check_in="2099-09-08", check_out="2099-09-09"), 10)
        self.assertFalse(result.candidates[0].date_verified)
        self.assertEqual(result.candidates[0].quote.basis, "unknown")

    def test_policy_never_approves(self):
        candidate = normalize_flyai(flight_payload(), "flight", make_trip(), 10, currency="CNY").candidates[0]
        rules = PolicyRules(version="test", effective_from="2099-01-01", effective_to="2099-12-31",
                            transport_ticket_caps={"flight": "300"})
        self.assertEqual(check_candidate(candidate, make_trip(), rules)["status"], "over_cap")
        rules.transport_ticket_caps["flight"] = Decimal("500")
        self.assertEqual(check_candidate(candidate, make_trip(), rules)["status"], "unverified")
        self.assertEqual(check_candidate(candidate, make_trip(), None)["reason"], "policy_unconfigured")

    def test_invalid_urls(self):
        self.assertIsNone(safe_url("javascript:alert(1)"))
        self.assertIsNone(safe_url("https://user:secret@example.com"))

    def test_policy_covers_return_after_checkout(self):
        trip = make_combined(return_date="2099-09-12", check_in="2099-09-08", check_out="2099-09-09")
        candidate = normalize_flyai(flight_payload(), "flight", trip, 10, currency="CNY").candidates[0]
        rules = PolicyRules(version="test", effective_from="2099-09-01", effective_to="2099-09-10",
                            transport_ticket_caps={"flight": "500"})
        self.assertEqual(check_candidate(candidate, trip, rules)["reason"], "policy_outside_effective_period")


class StorageProviderTests(unittest.TestCase):
    def test_memory_isolation_and_idempotence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            one = TravelMemory(root, root / "legacy", "one")
            two = TravelMemory(root, root / "legacy", "two")
            one.save_trip({"id": "draft-1", "request": {}})
            one.save_trip({"id": "draft-1", "request": {}})
            self.assertEqual(len(one.read()["trip_history"]), 1)
            self.assertEqual(two.read()["trip_history"], [])
            with self.assertRaises(ValueError):
                TravelMemory(root, root, "../escape")

    def test_legacy_read_does_not_rewrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "old"
            legacy.mkdir()
            file = legacy / "one.json"
            text = '{"preferences":{"airlines":["test"]},"trip_history":[]}'
            file.write_text(text, encoding="utf-8")
            memory = TravelMemory(root / "new", legacy, "one")
            self.assertEqual(memory.read()["preferences"], {"airlines": ["test"]})
            self.assertEqual(file.read_text(encoding="utf-8"), text)

    def test_provider_timeout_and_process_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = make_settings(root)
            provider = FlyAIProvider(settings)
            self.assertEqual(provider.search("flight", make_trip()).status, "unavailable")
            object.__setattr__(settings, "node_path", __file__)
            object.__setattr__(settings, "flyai_cli_path", __file__)
            with patch("business_travel.providers.subprocess.run", side_effect=subprocess.TimeoutExpired("x", 1)):
                self.assertEqual(provider.search("flight", make_trip()).error_code, "provider_timeout")
            with patch("business_travel.providers.subprocess.run", return_value=subprocess.CompletedProcess(
                [], 1, json.dumps(flight_payload()), "")):
                self.assertEqual(provider.search("flight", make_trip()).error_code, "provider_process_failed")

    def test_policy_not_implicitly_loaded(self):
        self.assertEqual(PolicyStore(None, None).search("hotel")["status"], "unavailable")

    def test_map_missing_key(self):
        self.assertEqual(InformationProvider(make_settings(Path("."))).commute("a", "b", "c")["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()

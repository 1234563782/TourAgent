"""Provider normalization to reviewed transport policy, without database or network."""

import unittest
from datetime import date
from unittest.mock import Mock

from business_travel.contracts import Candidate, TransportRequest
from business_travel.policy_pg import PostgresPolicyStore
from business_travel.tuniu import normalize_tuniu


class CandidatePolicyBridgeTests(unittest.TestCase):
    def setUp(self):
        self.trip = TransportRequest(origin="天津", destination="苏州", departure_date="2099-12-03",
                                employee_grade="普通员工", mainland_business_trip=True,
                                special_approval=False, long_assignment_days=3)
        self.store = object.__new__(PostgresPolicyStore)
        self.store.check = Mock(return_value={"status": "within_checked_rules", "approved": False})

    def test_train_provider_seat_reaches_rule_engine(self):
        payload = {"successCode": True, "data": [{
            "trainNum": "G871", "departureTime": "2099-12-03 09:00",
            "arrivalTime": "2099-12-03 12:00",
            "price": {"edzPrice": 193}, "seatAvailable": {"edzNum": 2}}]}
        result = normalize_tuniu(payload, "train", self.trip, 10)
        self.assertEqual(len(result.candidates), 1)
        candidate = result.candidates[0]
        checked = self.store.check_candidate(candidate, self.trip)
        facts = self.store.check.call_args.args[0]
        self.assertEqual(facts.transport_mode, "high_speed_train")
        self.assertEqual(facts.seat_class, "二等座")
        self.assertEqual(facts.expense_date, date(2099, 12, 3))
        self.assertEqual(checked["candidate_id"], candidate.id)
        self.assertFalse(checked["approved"])

    def test_missing_normalized_class_does_not_parse_display_label(self):
        candidate = Candidate(id="c", kind="train", label="G871 二等座",
                              observed_at="2099-12-03", request_date="2099-12-03")
        result = self.store.check_candidate(candidate, self.trip)
        self.assertEqual(result["status"], "insufficient_information")
        self.store.check.assert_not_called()

    def test_flight_booking_code_is_not_assumed_economy(self):
        payload = {"successCode": True, "data": [{
            "flightNumber": "TEST871", "departureTime": "2099-12-03 09:00",
            "arrivalTime": "2099-12-03 12:00", "basePrice": 193, "totalTax": 40,
            "cabinClass": "Y"}]}
        candidate = normalize_tuniu(payload, "flight", self.trip, 10).candidates[0]
        self.assertEqual(candidate.transport_mode, "flight")
        self.assertIsNone(candidate.seat_class)
        self.assertEqual(self.store.check_candidate(candidate, self.trip)["status"], "insufficient_information")
        self.store.check.assert_not_called()

    def test_inbound_uses_candidate_date_and_arrival_city(self):
        candidate = Candidate(id="c", kind="train", label="synthetic", observed_at="2099-12-03",
                              request_date="2099-12-05", transport_mode="regular_train",
                              seat_class="硬卧", details={"inbound": True})
        self.store.check_candidate(candidate, self.trip)
        facts = self.store.check.call_args.args[0]
        self.assertEqual(facts.destination, "天津")
        self.assertEqual(facts.expense_date, date(2099, 12, 5))

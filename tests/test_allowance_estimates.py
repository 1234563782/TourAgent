"""Natural-day allowance windows: expansion, per-day evidence, and gating."""

import unittest
from decimal import Decimal
from pathlib import Path

from business_travel.policy_guard import FIELD_LABELS, render_fact_context, render_policy
from business_travel.policy_lodging import LodgingRow
from business_travel.policy_package import prepare_package
from business_travel.policy_pg import PostgresPolicyStore
from business_travel.policy_rules import ExpenseFacts
from business_travel.reimbursement import bind_estimate_sources, estimate_expense
from business_travel.university_policy import UniversityRules, check_university_expense

ROOT = Path(__file__).resolve().parents[1]


class AllowanceEstimateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = prepare_package(ROOT / "policies/hfut-2026/manifest.json", company_id="test_co")
        cls.rules = UniversityRules.model_validate(cls.bundle.rules)
        cls.rows = [LodgingRow.model_validate(row) for row in cls.bundle.lodging_rows]
        cls.doc = {"id": 7, "source": "hfut", "version": cls.bundle.version, "status": "published",
                   "source_sha256": cls.bundle.source_sha256,
                   "effective_from": cls.bundle.effective_from, "effective_to": cls.bundle.effective_to}
        cls.chunks = [dict(chunk_id=c.id, content=c.text, clause=c.clause,
                           section=c.section, table_data=c.table) for c in cls.bundle.chunks]
        cls.sources = [PostgresPolicyStore._source(row, cls.doc) for row in cls.chunks]

    def facts(self, **changes):
        base = {"kind": "meal", "expense_date": "2026-10-12", "destination": "北京",
                "destination_province": "北京", "allowance_region": "北京",
                "trip_context": "ordinary", "personnel_type": "staff",
                "mainland_business_trip": True}
        base.update(changes)
        return ExpenseFacts.model_validate(base)

    def calculate(self, **changes):
        return estimate_expense(
            self.facts(**changes),
            lambda unit: check_university_expense(unit, self.rules, self.rows),
            max_nights=366, currency=self.rules.currency,
            effective_from=self.bundle.effective_from, effective_to=self.bundle.effective_to)

    def bound(self, **changes):
        result = self.calculate(**changes)
        bind_estimate_sources(result, self.sources, self.doc)
        return result

    def test_three_calendar_days_expand_to_daily_rows(self):
        result = self.bound(expense_end="2026-10-14")
        estimate = result["estimate"]
        self.assertEqual([item["date"] for item in estimate["items"]],
                         ["2026-10-12", "2026-10-13", "2026-10-14"])
        self.assertEqual(Decimal(estimate["reference_amount"]), Decimal("300"))
        self.assertEqual(Decimal(estimate["cap_total"]), Decimal("300"))
        self.assertEqual(result["status"], "within_checked_rules")
        self.assertFalse(result["approved"])
        self.assertTrue(all(item["evidence_verified"] for item in estimate["items"]))

    def test_single_day_without_window_is_unchanged(self):
        result = self.bound()
        self.assertEqual(len(result["estimate"]["items"]), 1)
        self.assertEqual(Decimal(result["estimate"]["reference_amount"]), Decimal("100"))

    def test_conference_without_transit_dates_is_insufficient(self):
        result = self.bound(expense_end="2026-10-14", trip_context="conference",
                            organizer_provides_meals=True)
        self.assertEqual(result["status"], "insufficient_information")
        self.assertIsNone(result["estimate"]["reference_amount"])
        self.assertIn("in_transit_dates", result["missing_fields"])
        self.assertTrue(all(item["reference_amount"] is None for item in result["estimate"]["items"]))

    def test_conference_pays_only_explicit_transit_days(self):
        result = self.bound(expense_end="2026-10-14", trip_context="conference",
                            organizer_provides_meals=True,
                            in_transit_dates=["2026-10-12", "2026-10-14"])
        amounts = [item["reference_amount"] for item in result["estimate"]["items"]]
        self.assertEqual(amounts, ["100", "0", "100"])
        self.assertEqual(Decimal(result["estimate"]["reference_amount"]), Decimal("200"))
        self.assertEqual(result["status"], "within_checked_rules")

    def test_local_transport_without_method_is_not_defaulted_to_allowance(self):
        result = self.bound(kind="local_transport", expense_end="2026-10-14")
        self.assertEqual(result["status"], "insufficient_information")
        self.assertIn("local_transport_method", result["missing_fields"])
        self.assertIsNone(result["estimate"]["reference_amount"])

    def test_local_transport_allowance_expands_by_day(self):
        result = self.bound(kind="local_transport", expense_end="2026-10-14",
                            local_transport_method="allowance", paid_by_other_organization=False)
        self.assertEqual(Decimal(result["estimate"]["reference_amount"]), Decimal("240"))
        self.assertEqual(result["status"], "within_checked_rules")

    def test_local_transport_receipts_goes_to_manual_review(self):
        result = self.bound(kind="local_transport", expense_end="2026-10-14",
                            local_transport_method="receipts", paid_by_other_organization=False)
        self.assertEqual(result["status"], "manual_review")
        self.assertIsNone(result["estimate"]["reference_amount"])

    def test_window_outside_published_period_is_refused(self):
        result = self.bound(expense_date="2027-10-12", expense_end="2027-10-14")
        self.assertEqual(result["status"], "insufficient_information")
        self.assertEqual(result["reason"], "expense_outside_published_period")

    def test_reversed_window_is_rejected(self):
        with self.assertRaises(ValueError):
            self.facts(expense_end="2026-10-11")

    def test_mainland_scope_is_derived_from_a_known_destination(self):
        result = self.bound(expense_end="2026-10-12", mainland_business_trip=None)
        self.assertEqual(result["status"], "within_checked_rules")
        self.assertEqual(Decimal(result["estimate"]["reference_amount"]), Decimal("100"))

    def test_twelve_hour_rule_adds_one_increment_row(self):
        result = self.bound(expense_end="2026-10-13",
                            departure_at="2026-10-12T08:00:00", arrival_at="2026-10-13T09:00:00")
        items = result["estimate"]["items"]
        self.assertEqual(len(items), 3)
        addon = items[-1]
        self.assertTrue(addon["addon"])
        self.assertEqual(addon["addon_days"], 2)
        self.assertEqual(addon["reference_amount"], "200")
        self.assertEqual(Decimal(result["estimate"]["reference_amount"]), Decimal("400"))

    def test_exactly_twelve_hours_adds_nothing(self):
        result = self.bound(expense_end="2026-10-12",
                            departure_at="2026-10-12T08:00:00", arrival_at="2026-10-12T20:00:00")
        self.assertEqual(len(result["estimate"]["items"]), 1)
        self.assertFalse(any(item.get("addon") for item in result["estimate"]["items"]))

    def test_missing_ticket_times_add_nothing(self):
        result = self.bound(expense_end="2026-10-12")
        self.assertFalse(any(item.get("addon") for item in result["estimate"]["items"]))
        self.assertEqual(Decimal(result["estimate"]["reference_amount"]), Decimal("100"))

    def test_conference_addon_pays_transit_even_when_meals_are_provided(self):
        result = self.bound(expense_end="2026-10-13", trip_context="conference",
                            organizer_provides_meals=True, in_transit_dates=["2026-10-12"],
                            departure_at="2026-10-12T08:00:00", arrival_at="2026-10-13T09:00:00")
        self.assertEqual(Decimal(result["estimate"]["reference_amount"]), Decimal("300"))

    def test_view_shows_the_addon_row(self):
        result = self.bound(expense_end="2026-10-13",
                            departure_at="2026-10-12T08:00:00", arrival_at="2026-10-13T09:00:00")
        data = {"check_mode": "estimate", "rule_checks": [{
            "facts": self.facts(expense_end="2026-10-13").model_dump(mode="json"),
            "result": result, "fact_context": {}}]}
        text = render_policy(data)
        self.assertIn("在途加发", text)
        self.assertIn("每满12小时加发一天", text)

    def test_batch_summary_totals_only_when_every_line_is_cleared(self):
        from business_travel.reimbursement_view import render_batch_summary
        lines = [
            {"kind": "hotel", "scenario": "actual", "status": "within_checked_rules",
             "reference_amount": "500", "currency": "CNY", "evidence_complete": True},
            {"kind": "meal", "scenario": "actual", "status": "within_checked_rules",
             "reference_amount": "300", "currency": "CNY", "evidence_complete": True},
        ]
        text = render_batch_summary(lines)
        self.assertIn("已核参考合计：800 CNY", text)
        self.assertIn("均已核清且依据完整", text)

    def test_batch_summary_refuses_total_when_one_line_is_unclear(self):
        from business_travel.reimbursement_view import render_batch_summary
        lines = [
            {"kind": "hotel", "scenario": "actual", "status": "within_checked_rules",
             "reference_amount": "500", "currency": "CNY", "evidence_complete": True},
            {"kind": "meal", "scenario": "actual", "status": "insufficient_information",
             "reference_amount": None, "currency": "CNY", "evidence_complete": True},
        ]
        text = render_batch_summary(lines)
        self.assertIn("本次未给出合计", text)
        self.assertNotIn("已核参考合计", text)
        self.assertIn("500 CNY", text)
        self.assertIn("信息不足，未核清", text)

    def test_batch_summary_never_mixes_hypothetical_into_the_total(self):
        from business_travel.reimbursement_view import render_batch_summary
        lines = [
            {"kind": "hotel", "scenario": "actual", "status": "within_checked_rules",
             "reference_amount": "500", "currency": "CNY", "evidence_complete": True},
            {"kind": "transport", "scenario": "hypothetical", "status": "within_checked_rules",
             "reference_amount": "900", "currency": "CNY", "evidence_complete": True},
        ]
        text = render_batch_summary(lines)
        self.assertIn("已核参考合计：500 CNY", text)
        self.assertIn("假设测算", text)
        self.assertIn("不计入实际合计", text)

    def test_batch_summary_is_silent_for_a_single_line(self):
        from business_travel.reimbursement_view import render_batch_summary
        self.assertEqual(render_batch_summary([{"kind": "meal", "reference_amount": "100"}]), "")

    def test_view_lists_each_day_and_the_total(self):
        result = self.bound(expense_end="2026-10-14")
        data = {"check_mode": "estimate", "rule_checks": [{
            "facts": self.facts(expense_end="2026-10-14").model_dump(mode="json"),
            "result": result, "fact_context": {}}]}
        text = render_policy(data)
        self.assertIn("包干标准", text)
        self.assertIn("2026-10-13", text)
        self.assertIn("300", text)
        self.assertIn("自然（日历）天数", text)


if __name__ == "__main__":
    unittest.main()

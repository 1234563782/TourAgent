"""University package and conservative checks, without external services."""

import copy
import hashlib
import json
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from docx import Document

from business_travel.policy_documents import PolicyBundle, parse_docx
from business_travel.policy_guard import render_policy
from business_travel.policy_lodging import LodgingRow, list_lodging, lookup_lodging, parse_seasons
from business_travel.policy_package import prepare_package
from business_travel.policy_rules import ExpenseFacts
from business_travel.university_policy import (UniversityRules, check_university_expense,
                                                lodging_standard_for, planning_standards_for,
                                                transport_category)

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "policies/hfut-2026/manifest.json"


class UniversityPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = prepare_package(MANIFEST)
        cls.rows = [LodgingRow.model_validate(r) for r in cls.bundle.lodging_rows]
        cls.rules = UniversityRules.model_validate(cls.bundle.rules)

    def facts(self, **changes):
        return ExpenseFacts.model_validate({
            "kind": "hotel", "expense_date": "2026-09-16", "destination": "合肥",
            "destination_province": "安徽", "mainland_business_trip": True,
            "personnel_type": "staff", "lodging_standard": "其他人员",
            "amount": "350", "currency": "CNY", "basis": "person_day",
            "taxes_confirmed": True, "date_verified": True, **changes})

    def check(self, **changes):
        return check_university_expense(self.facts(**changes), self.rules, self.rows)

    def lookup(self, city, day="2026-09-16", province=None, grade="其他人员"):
        return lookup_lodging(self.rows, destination=city, province=province,
                              standard=grade, day=date.fromisoformat(day))

    def test_actual_package_all_sources_and_86_rows_bound(self):
        self.assertEqual(len(self.rows), 86)
        self.assertEqual(len(self.bundle.sources), 2)
        self.assertEqual(self.bundle.effective_to, date(2026, 12, 31))
        self.assertEqual({r.sheet for r in self.rows}, {"Sheet1"})
        self.assertEqual(self.bundle.digest(), prepare_package(MANIFEST).digest())
        for source in self.bundle.sources:
            self.assertEqual(source.sha256, hashlib.sha256((MANIFEST.parent / source.source).read_bytes()).hexdigest())

    def test_all_region_projection_keeps_every_source_row_and_selected_grade(self):
        result = list_lodging(self.rows, standard="部级")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["rows"]), 86)
        self.assertEqual(result["row_ids"][0], "lodging-r9")
        self.assertTrue(all(row["ordinary_cap"] for row in result["rows"]))
        self.assertEqual(result["rows"][0]["lodging_standard"], "部级")

    def test_every_rule_clause_has_body_evidence(self):
        clauses = {c.clause for c in self.bundle.chunks}
        self.assertTrue({c for group in self.rules.source_clauses.values() for c in group} <= clauses)
        self.assertIn("第三十七条", clauses)
        table = [c for c in self.bundle.chunks if c.kind == "table_row"]
        self.assertEqual(len(table), 3)
        self.assertTrue(all(c.clause == "第六条" for c in table))

    def test_manual_line_breaks_split_clauses_without_loss(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clauses.docx"
            doc = Document()
            doc.add_paragraph("第一条 范围。\n第二条 条件。\n第三条 限制。")
            doc.save(path)
            bundle = parse_docx(path, policy_key="test",
                                version="test", effective_from=date(2026, 1, 1))
        self.assertEqual([c.clause for c in bundle.chunks], ["第一条", "第二条", "第三条"])
        self.assertEqual("".join(c.text for c in bundle.chunks), "第一条 范围。第二条 条件。第三条 限制。")

    def test_mutated_source_or_lodging_data_rejected(self):
        payload = self.bundle.model_dump(mode="json")
        for action in ("row", "file", "duplicate", "chunk", "digest"):
            changed = copy.deepcopy(payload)
            if action == "row":
                changed["lodging_rows"][0]["caps"]["其他人员"] = "1"
            elif action == "file":
                changed["chunks"][0]["table"]["_provenance"]["sha256"] = "f" * 64
            elif action == "duplicate":
                changed["lodging_rows"].append(changed["lodging_rows"][0])
            elif action == "chunk":
                changed["lodging_rows"][0]["id"] = "missing"
            else:
                changed["source_sha256"] = "f" * 64
            with self.subTest(action=action), self.assertRaises(ValueError):
                PolicyBundle.model_validate(changed)

    def test_legacy_bundle_hash_is_unchanged(self):
        value = dict(schema_version=1, policy_key="old", version="1",
                     effective_from="2026-01-01", source="old.docx", source_sha256="a" * 64,
                     chunks=[dict(id="one", section="s", text="text")])
        bundle = PolicyBundle.model_validate(value)
        old = bundle.model_dump_json(exclude={"sources", "lodging_rows", "adoption_note"})
        self.assertEqual(bundle.digest(), hashlib.sha256(old.encode()).hexdigest())

    def test_beijing_nanjing_hangzhou_anhui(self):
        for city, province, cap in (("北京", None, "500"), ("南京市", "江苏省", "380"),
                                     ("杭州", "浙江", "400"), ("合肥", "安徽", "350")):
            with self.subTest(city=city):
                self.assertEqual(Decimal(self.lookup(city, province=province)["cap"]), Decimal(cap))

    def test_qingdao_season_boundaries(self):
        for day, cap in (("2026-06-30", "380"), ("2026-07-01", "450"),
                         ("2026-09-30", "450"), ("2026-10-01", "380")):
            with self.subTest(day=day):
                self.assertEqual(Decimal(self.lookup("青岛", day, "山东")["cap"]), Decimal(cap))

    def test_unknown_stay_date_never_defaults_to_today_for_season(self):
        result = lookup_lodging(self.rows, destination="青岛", province="山东",
                                standard="其他人员", day=None)
        self.assertEqual(result["status"], "insufficient_information")
        self.assertEqual(result["missing_fields"], ["expense_date"])
        self.assertNotIn("cap", result)
        self.assertIn("lodging-r44", result["row_ids"])

    def test_nonseasonal_standard_does_not_require_stay_date(self):
        result = lookup_lodging(self.rows, destination="南京", province="江苏",
                                standard="其他人员", day=None)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(Decimal(result["cap"]), Decimal("380"))
        self.assertIsNone(result["date"])

    def test_luoyang_ten_day_boundary(self):
        before = self.lookup("洛阳", "2026-05-10", "河南")
        after = self.lookup("洛阳", "2026-05-11", "河南")
        self.assertTrue(before["seasonal"])
        self.assertFalse(after["seasonal"])
        self.assertGreater(Decimal(before["cap"]), Decimal(after["cap"]))

    def test_cross_year_and_city_specific_seasons(self):
        self.assertTrue(self.lookup("张家口", "2026-01-10", "河北")["seasonal"])
        self.assertFalse(self.lookup("石家庄", "2026-01-10", "河北")["seasonal"])
        self.assertFalse(self.lookup("秦皇岛", "2026-09-10", "河北")["seasonal"])
        self.assertTrue(self.lookup("三亚", "2026-01-10", "海南")["seasonal"])
        self.assertFalse(self.lookup("三亚", "2026-05-01", "海南")["seasonal"])

    def test_merged_region_not_inherited_from_previous_province(self):
        subset = [r for r in self.rows if 20 <= int(r.id.split("r")[-1]) <= 23]
        self.assertEqual({r.region for r in subset}, {"内蒙古"})
        self.assertTrue(self.lookup("额济纳旗", "2026-10-01", "内蒙古自治区")["seasonal"])

    def test_unknown_or_ambiguous_location_is_not_other_area(self):
        for city, province in (("重庆", "重庆"), ("天津", "天津"), ("某未知区", "浙江"),
                               ("杭州西湖区", "浙江"), ("南京", "广东"), ("未知地点", None)):
            with self.subTest(city=city):
                self.assertNotEqual(self.lookup(city, province=province)["status"], "ok")

    def test_season_parser_rejects_unreviewed_syntax(self):
        for text in ("旺季", "4-13月", "春节", "7月", "4-5月初"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_seasons(text)

    def test_every_season_start_and_end_included(self):
        for row in self.rows:
            for window in row.seasons:
                for day in (date(2026, window.start_month, window.start_day),
                            date(2026, window.end_month, min(window.end_day, 28)
                                 if window.end_month == 2 else window.end_day)):
                    self.assertTrue(window.includes(day))

    def test_season_windows_cross_year_excludes_gap(self):
        window = parse_seasons("11-3月")[0]
        self.assertTrue(window.includes(date(2026, 12, 31)))
        self.assertTrue(window.includes(date(2026, 1, 1)))
        self.assertFalse(window.includes(date(2026, 4, 1)))

    def test_transport_identity_does_not_choose_lodging_grade(self):
        facts = self.facts(lodging_standard=None, academic_title="院士", transport_standard="一类")
        self.assertEqual(transport_category(facts, self.rules), "一类")
        self.assertIsNone(lodging_standard_for(facts, self.rules))
        result = self.check(lodging_standard=None, academic_title="院士", transport_standard="一类")
        self.assertEqual(result["missing_fields"], ["lodging_standard"])

    def test_transport_category_does_not_choose_lodging_grade(self):
        for category in ("一类", "二类", "三类"):
            with self.subTest(category=category):
                facts = self.facts(lodging_standard=None, transport_standard=category)
                self.assertIsNone(lodging_standard_for(facts, self.rules))
                self.assertEqual(planning_standards_for(facts, self.rules), (category, None))

    def test_student_planning_does_not_assume_third_class(self):
        facts = self.facts(personnel_type="student", student_activity_eligible=None,
                           transport_standard=None, lodging_standard=None)
        self.assertEqual(planning_standards_for(facts, self.rules), (None, None))
        self.assertIsNone(transport_category(facts, self.rules))
        result = self.check(personnel_type="student", student_activity_eligible=None,
                            transport_standard="三类", lodging_standard="其他人员")
        self.assertEqual(result["status"], "insufficient_information")
        self.assertEqual(result["missing_fields"], ["student_activity_eligible"])

    def test_transport_category_label_is_not_a_lodging_header(self):
        for category in ("一类", "二类", "三类"):
            with self.subTest(category=category):
                facts = self.facts(lodging_standard=category, transport_standard=category)
                self.assertIsNone(lodging_standard_for(facts, self.rules))
                result = self.check(lodging_standard=category, transport_standard=category)
                self.assertEqual(result["missing_fields"], ["lodging_standard"])

    def test_explicit_rank_suffix_matches_only_an_existing_header(self):
        baseline = self.check(lodging_standard="其他人员")
        for label in ("其他人员档", "其他人员档次", "其他人员标准", "其他人员"):
            with self.subTest(label=label):
                checked = self.check(lodging_standard=label)
                self.assertEqual(checked["status"], baseline["status"])
                self.assertEqual(checked["cap"], baseline["cap"])
        for label in ("普通人员", "教授", "不是其他人员档"):
            self.assertEqual(self.check(lodging_standard=label)["missing_fields"], ["lodging_standard"])

    def test_person_day_not_automatically_shared_room_division(self):
        result = self.check(amount="600", basis="room_night", occupants=2)
        self.assertEqual(result["reason"], "personal_lodging_amount_required")
        self.assertEqual(self.check(basis="room_night", occupants=1)["status"], "within_checked_rules")
        self.assertEqual(self.check(basis="person_day", occupants=2, amount="351")["status"], "over_standard")

    def test_missing_dates_taxes_currency_not_compliant(self):
        for field, value in (("date_verified", None), ("date_verified", False),
                             ("taxes_confirmed", False), ("currency", "USD"), ("basis", "ticket")):
            with self.subTest(field=field, value=value):
                self.assertEqual(self.check(**{field: value})["status"], "insufficient_information")

    def test_conference_and_fieldwork_exceptions_not_false_rejection(self):
        for change in ({"organizer_arranged_lodging": True}, {"trip_context": "fieldwork"},
                       {"trip_context": "noon_rest"}, {"special_approval": True}):
            self.assertEqual(self.check(amount="99999", **change)["status"], "manual_review")

    def test_university_does_not_require_legacy_company_approvals(self):
        result = self.check(kind="transport", transport_standard="二类",
                            transport_mode="flight", seat_class="经济舱",
                            special_approval=None, long_assignment_days=None,
                            flight_approval=None, suitable_direct_train_hours=None)
        self.assertEqual(result["status"], "within_checked_rules")
        self.assertFalse(result["approved"])

    def test_transport_identity_higher_category_and_conflicts(self):
        self.assertEqual(transport_category(self.facts(academic_title="院士", management_grade=5),
                                            self.rules), "一类")
        self.assertIsNone(transport_category(self.facts(academic_title="院士", transport_standard="三类"),
                                            self.rules))
        self.assertEqual(transport_category(self.facts(technical_grade=2), self.rules), "二类")

    def test_employee_grade_is_not_treated_as_academic_title(self):
        facts = self.facts(academic_title=None, employee_grade="院士",
                           transport_standard=None, lodging_standard=None)
        self.assertIsNone(transport_category(facts, self.rules))
        self.assertIsNone(lodging_standard_for(facts, self.rules))
        self.assertIsNone(transport_category(
            self.facts(academic_title=None, employee_grade="普通员工",
                       transport_standard=None, lodging_standard=None), self.rules))

    def test_transport_requests_only_absent_fields(self):
        for mode, seat, expected in (
            (None, "经济舱", ["transport_mode"]),
            ("flight", None, ["seat_class"]),
            (None, None, ["transport_mode", "seat_class"]),
        ):
            with self.subTest(mode=mode, seat=seat):
                result = self.check(kind="transport", transport_standard="二类",
                                    transport_mode=mode, seat_class=seat)
                self.assertEqual(result["status"], "insufficient_information")
                self.assertEqual(result["missing_fields"], expected)

    def test_student_requires_activity_and_never_first_class(self):
        for eligible in (None, False):
            self.assertIsNone(transport_category(self.facts(personnel_type="student",
                student_activity_eligible=eligible), self.rules))
        result = self.check(kind="transport", personnel_type="student", student_activity_eligible=True,
                            transport_mode="high_speed_train", seat_class="一等座")
        self.assertEqual(result["status"], "over_standard")
        self.assertFalse(result["approved"])

    def test_meals_natural_day_not_legacy_deductions(self):
        result = self.check(kind="meal", trip_context="ordinary", allowance_region="安徽",
                            overnight_trip=False, covered_meals=["breakfast"])
        self.assertEqual(result["amount"], "100")
        self.assertEqual(self.check(kind="meal", trip_context="ordinary", allowance_region="青海省")["amount"], "120")

    def test_meeting_meals_only_in_transit(self):
        common = dict(kind="meal", trip_context="conference", allowance_region="安徽",
                      organizer_provides_meals=True)
        self.assertEqual(self.check(**common)["missing_fields"], ["in_transit_day"])
        self.assertEqual(self.check(**common, in_transit_day=False)["amount"], "0")
        self.assertEqual(self.check(**common, in_transit_day=True)["amount"], "100")

    def test_unknown_meal_region_or_identity_not_defaulted(self):
        self.assertEqual(self.check(kind="meal", trip_context="ordinary",
                                   allowance_region="西宁")["missing_fields"], ["allowance_region"])
        self.assertEqual(self.check(kind="meal", trip_context="ordinary", personnel_type=None,
                                   allowance_region="安徽")["missing_fields"], ["personnel_type"])

    def test_invited_actual_allowances_not_staff_flat_rates(self):
        self.assertEqual(self.check(kind="meal", trip_context="ordinary",
            allowance_region="安徽", personnel_type="invited")["status"], "manual_review")

    def test_local_transport_choice_and_duplicate_coverage(self):
        common = dict(kind="local_transport", trip_context="ordinary",
                      local_transport_method="allowance", paid_by_other_organization=False)
        self.assertEqual(self.check(**common)["amount"], "80")
        self.assertEqual(self.check(**{**common, "paid_by_other_organization": True})["amount"], "0")
        self.assertEqual(self.check(**{**common, "local_transport_method": "receipts"})["status"], "manual_review")

    def test_local_transport_requests_only_unknown_fields_not_false(self):
        for method, paid, expected in (
            (None, False, ["local_transport_method"]),
            ("allowance", None, ["paid_by_other_organization"]),
            (None, None, ["local_transport_method", "paid_by_other_organization"]),
        ):
            with self.subTest(method=method, paid=paid):
                result = self.check(kind="local_transport", trip_context="ordinary",
                                    local_transport_method=method, paid_by_other_organization=paid)
                self.assertEqual(result["status"], "insufficient_information")
                self.assertEqual(result["missing_fields"], expected)

    def test_render_no_company_grade_question_and_units(self):
        result = self.check(lodging_standard=None)
        text = render_policy({"rule_checks": [{"facts": self.facts(lodging_standard=None).model_dump(),
                                               "result": result}]})
        self.assertIn("住宿人员档次", text)
        self.assertNotIn("、职级", text)
        text = render_policy({"rule_checks": [{"facts": self.facts().model_dump(), "result": self.check()}]})
        self.assertIn("人·天", text)

    def test_all_results_never_approve(self):
        for kind in ("hotel", "transport", "meal", "local_transport"):
            self.assertIs(self.check(kind=kind)["approved"], False)

    def test_unverified_facts_do_not_invent_company_grade_question(self):
        text = render_policy({"rule_checks": [{
            "facts": {"kind": "hotel", "lodging_standard": "其他人员"},
            "result": {"status": "insufficient_information", "reason": "unsupported_user_facts",
                       "missing_fields": ["lodging_standard"]},
        }]})
        self.assertIn("住宿人员档次", text)
        self.assertNotIn("职级", text)


if __name__ == "__main__":
    unittest.main()

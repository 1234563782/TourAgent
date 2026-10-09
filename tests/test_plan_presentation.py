"""Presentation changes must not manufacture prices, identity or policy clearance."""

import copy
import unittest

from business_travel.plan_checks import render_draft


def draft():
    return {
        "status": "draft",
        "request": {"query_scope": "combined", "origin": "甲市", "destination": "乙市",
                    "journey": "round_trip", "departure_date": "2099-10-12",
                    "return_date": "2099-10-14", "check_in": "2099-10-12",
                    "check_out": "2099-10-14"},
        "selected": [
            {"id": "internal-out", "kind": "train", "label": "G123 / 二等座",
             "departure": "2099-10-12 09:00", "arrival": "2099-10-12 11:00",
             "quote": {"amount": "120.50", "currency": "CNY", "basis": "ticket"}},
            {"id": "internal-back", "kind": "train", "label": "G456 / 二等座",
             "departure": "2099-10-14 10:00", "arrival": "2099-10-14 12:00",
             "details": {"inbound": True},
             "quote": {"amount": "150", "currency": "CNY", "basis": "ticket"}},
            {"id": "internal-hotel", "kind": "hotel", "label": "参考酒店", "address": "乙市示例路",
             "quote": {"amount": "300", "currency": "CNY", "basis": "unknown"},
             "details": {"hotel_detail": {
                 "status": "ok", "availability": "available_snapshot",
                 "requested": {"checkIn": "2099-10-12", "checkOut": "2099-10-14"},
                 "rates": [{"room_name": "标准房", "rate_name": "参考方案", "amount": "300",
                            "remaining_count": 99, "meal": "不含早餐", "cancellation": "取消收取全款"}]}}},
        ],
        "policy_checks": [
            {"candidate_id": "internal-out", "status": "insufficient_information",
             "reason": "required_facts_missing", "missing_fields": ["mainland_business_trip"]},
            {"candidate_id": "internal-back", "status": "insufficient_information",
             "reason": "required_facts_missing", "missing_fields": ["mainland_business_trip"]},
            {"candidate_id": "internal-hotel", "status": "insufficient_information",
             "reason": "each_night_requires_individual_verified_quote"},
        ],
    }


def main_text(text):
    lines, hidden = [], False
    for line in text.splitlines():
        if line.startswith(":::details "):
            hidden = True
        elif line == ":::":
            hidden = False
        elif not hidden:
            lines.append(line)
    return "\n".join(lines)


class PlanPresentationTests(unittest.TestCase):
    def test_summary_first_and_operational_detail_collapsed(self):
        data = draft()
        original = copy.deepcopy(data)
        text = render_draft(data)
        main = main_text(text)
        self.assertIn("甲市 ⇄ 乙市", main)
        self.assertLess(main.index("交通参考"), main.index("待补充"))
        self.assertIn("270.5 CNY", main)
        self.assertIn("每张票", main)
        self.assertIn("入住：2099-10-12", main)
        self.assertIn("2晚", main)
        self.assertIn("不能确认合规", main)
        self.assertIn("逐晚含税价格", main)
        self.assertEqual(main.count("是否大陆因公出差"), 1)
        self.assertIn("计价口径：待核实", main)
        self.assertIn("按供应商含税价格展示", main)
        self.assertIn("取消", main)
        self.assertNotIn("供应商默认", main)
        self.assertNotIn("返回库存", main)
        self.assertNotIn("未预订、未付款、未审批", text)
        self.assertIn(":::details 房型与退改详情", text)
        self.assertIn("取消收取全款", text)
        self.assertNotIn("internal-", text)
        self.assertEqual(data, original)

    def test_no_false_subtotal_with_mixed_unknown_or_invalid_prices(self):
        for changes in ({"currency": "USD"}, {"currency": ""}, {"amount": None}, {"basis": "unknown"},
                        {"amount": "NaN"}, {"amount": "-1"}, {"amount": True}):
            data = draft()
            data["selected"][1]["quote"].update(changes)
            with self.subTest(changes=changes):
                self.assertNotIn("交通票价合计", main_text(render_draft(data)))

    def test_over_standard_and_time_conflicts_remain_visible(self):
        data = draft()
        data["policy_checks"] = [
            {"candidate_id": "internal-hotel", "status": "over_standard",
             "reason": "hotel_nightly_cap_comparison", "cap": "250", "amount": "300",
             "excess": "50", "currency": "CNY"}]
        data["timeline_check"] = {"issues": ["return_departs_before_outbound_arrival"]}
        text = main_text(render_draft(data))
        for term in ("参考酒店", "超出所核对", "250 CNY", "50 CNY", "返程早于"):
            self.assertIn(term, text)

    def test_unknown_and_failed_policy_checks_not_presented_as_passed(self):
        for checks in ([], [{"status": "unverified", "reason": "policy_check_failed"}]):
            data = draft()
            data["policy_checks"] = checks
            text = main_text(render_draft(data))
            self.assertIn("不能确认合规", text)
            self.assertNotIn("已通过", text)

    def test_policy_details_do_not_hide_manual_review_or_known_limit(self):
        data = draft()
        data["policy_checks"] = [
            {"candidate_id": "internal-out", "status": "manual_review",
             "reason": "university_exception_conditions"},
            {"candidate_id": "internal-hotel", "status": "within_checked_rules",
             "reason": "university_lodging_cap", "cap": "400", "amount": "300", "currency": "CNY"}]
        text = main_text(render_draft(data))
        self.assertIn("人工核验", text)
        self.assertIn("400 CNY", text)
        self.assertIn("不代表完整合规", text)

    def test_table_cells_cannot_inject_rows(self):
        data = draft()
        data["selected"][0]["label"] = "G123 | 假列\n### 假标题"
        text = render_draft(data)
        self.assertNotIn("\n### 假标题", text)
        self.assertIn("G123", text)

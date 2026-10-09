import unittest

from business_travel.plan_checks import render_draft
from business_travel.policy_guard import render_policy
from business_travel.result_labels import POLICY_REASON_LABELS, policy_reason_label


class ResultLabelTests(unittest.TestCase):
    def test_hotel_quote_and_multinight_policy_are_readable_without_claiming_approval(self):
        internal_id = "66b38d426961cdaf4a92"
        text = render_draft({
            "status": "draft", "request": {"query_scope": "hotel"},
            "selected": [{"kind": "hotel", "label": "测试住宿", "id": internal_id,
                          "quote": {"amount": "300", "currency": "CNY", "basis": "unknown"}}],
            "policy_checks": [{"candidate_id": internal_id, "status": "insufficient_information",
                              "reason": "each_night_requires_individual_verified_quote"}]})
        for expected in ("酒店：测试住宿", "计价口径：待核实", "逐晚含税价格", "不能确认合规"):
            self.assertIn(expected, text)
        self.assertNotIn(internal_id, text)
        for code in ("unknown", "insufficient_information", "each_night_requires_individual_verified_quote"):
            self.assertNotIn(code, text)

    def test_unselected_candidates_and_commute_checks_do_not_expose_internal_ids(self):
        internal_id = "90e7ca628c3b1cf2a13e"
        text = render_draft({
            "request": {"query_scope": "combined"},
            "alternatives": [{"kind": "hotel", "label": "参考酒店", "id": internal_id}],
            "commute_checks": {internal_id: {"status": "ok", "distance_meters": 1200,
                                            "duration_seconds": 900}},
            "policy_checks": [{"candidate_id": internal_id, "status": "unverified",
                               "reason": "required_facts_missing"}],
        })
        self.assertIn("酒店：参考酒店", text)
        self.assertIn("通勤估算（酒店：参考酒店）", text)
        self.assertNotIn(internal_id, text)

    def test_missing_facts_and_amounts_remain_visible(self):
        text = render_draft({"policy_checks": [
            {"status": "insufficient_information", "reason": "required_facts_missing",
             "missing_fields": ["employee_grade", "taxes_confirmed"]},
            {"status": "over_standard", "reason": "hotel_nightly_cap_comparison",
             "cap": "400", "amount": "500", "excess": "100", "currency": "CNY"}]})
        for expected in ("职级", "是否含税", "超出所核对", "400 CNY", "500 CNY", "100 CNY"):
            self.assertIn(expected, text)

    def test_unknown_reason_is_not_exposed_or_treated_as_success(self):
        text = policy_reason_label("private-unexpected-error")
        self.assertNotIn("private", text)
        self.assertIn("不能据此认定合规", text)
        for reason in POLICY_REASON_LABELS:
            self.assertNotEqual(reason, policy_reason_label(reason))

    def test_policy_answer_uses_same_reason_vocabulary(self):
        text = render_policy({"rule_checks": [{
            "facts": {"kind": "hotel"},
            "result": {"status": "insufficient_information",
                       "reason": "nightly_tax_inclusive_quote_required"}}]})
        self.assertIn("每间每晚报价", text)
        self.assertIn("科目：酒店", text)
        self.assertNotIn("核验依据代码", text)
        self.assertNotIn("nightly_tax_inclusive_quote_required", text)

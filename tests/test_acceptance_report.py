"""验收报告必须保留原始回答，并区分执行完成与业务通过。"""

import unittest

from scripts.render_acceptance_report import render
from scripts.render_business_retest import render as render_retest


class AcceptanceReportTests(unittest.TestCase):
    def setUp(self):
        self.records = [{
            "id": "example", "expected": "Check facts", "turns": [{
                "question": "A question", "answer": "An incorrect answer",
                "status": "completed", "seconds": 1.2, "events": [],
                "memory": {"trip_history": []},
            }],
        }]

    def test_completed_does_not_mean_passed_and_answer_is_preserved(self):
        output = render(self.records, {"example": {
            "status": "未通过", "reason": "Incorrect facts",
        }}, [])
        self.assertIn("未通过 1 组", output)
        self.assertIn("An incorrect answer", output)
        self.assertIn("A question", output)

    def test_missing_review_is_rejected(self):
        with self.assertRaises(ValueError):
            render(self.records, {}, [])

    def test_extra_review_is_rejected(self):
        with self.assertRaises(ValueError):
            render(self.records, {
                "example": {"status": "通过", "reason": "Checked"},
                "extra": {"status": "通过", "reason": "Not executed"},
            }, [])


class ReportTripMemoryTests(unittest.TestCase):
    def test_reports_read_trip_history_without_preference_field(self):
        for structured in (False, True):
            for legacy in (False, True):
                with self.subTest(structured=structured, legacy=legacy):
                    memory = {"trip_history": [{"id": "saved-trip-example"}]}
                    if legacy:
                        memory["preferences"] = ["retired-preference-example"]
                    record = {"id": "example", "expected": "Read trips", "turns": [{
                        "question": "Read saved trips", "answer": "Original answer",
                        "status": "completed", "seconds": 1, "events": [], "memory": memory,
                    }]}
                    reviews = {"example": {"status": "通过", "reason": "Checked"}}
                    reports = [
                        render([record], reviews, []),
                        render_retest([record], reviews, "2099-01-01", [], structured=structured),
                    ]
                    for report in reports:
                        self.assertIn("saved-trip-example", report)
                        self.assertIn("Original answer", report)
                        self.assertNotIn("retired-preference-example", report)
                        self.assertNotIn("测试记忆中的偏好", report)
                        self.assertNotIn("实际测试偏好", report)
                        self.assertNotIn("优先修复偏好工具", report)
                        self.assertNotIn("偏好发起依赖模型调用工具", report)

    def test_reports_accept_empty_memory_and_legacy_preference_only_memory(self):
        for memory in ({}, {"preferences": []}, {"trip_history": []}):
            with self.subTest(memory=memory):
                record = {"id": "example", "expected": "No saved trips", "turns": [{
                    "question": "Read trips", "answer": "No trips", "status": "completed",
                    "seconds": 1, "events": [], "memory": memory,
                }]}
                reviews = {"example": {"status": "通过", "reason": "Checked"}}
                for report in (render([record], reviews, []),
                               render_retest([record], reviews, "2099-01-01", [])):
                    self.assertIn("已保存行程：[]", report)

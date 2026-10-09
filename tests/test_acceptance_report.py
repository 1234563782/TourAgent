"""验收报告必须保留原始回答，并区分执行完成与业务通过。"""

import unittest

from scripts.render_acceptance_report import render


class AcceptanceReportTests(unittest.TestCase):
    def setUp(self):
        self.records = [{
            "id": "example", "expected": "Check facts", "turns": [{
                "question": "A question", "answer": "An incorrect answer",
                "status": "completed", "seconds": 1.2, "events": [],
                "memory": {"preferences": [], "trip_history": []},
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

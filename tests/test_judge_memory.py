"""Synthetic memory answer Judge contract tests; no model/network calls."""

import unittest
from unittest.mock import Mock

from evals.judge_memory import grade_case, validate_report
from evals.langfuse_memory import fingerprint


class JudgeMemoryTests(unittest.TestCase):
    def test_semantic_grade_keeps_quote_grounded(self):
        model = Mock()
        model.invoke.return_value.content = '{"score":2,"reason":"解释清楚","answer_quote":"未保存"}'
        entry = {"score": {"status": "passed"},
                 "output": {"status": "completed", "answer": "该偏好未保存。",
                            "preferences": {"items": []}, "tools": []}}
        self.assertEqual(grade_case(model, {"question": "测试", "expected": "不保存"}, entry)["score"], 2)
        self.assertIn('"hard_assertion_status": "passed"', model.invoke.call_args.args[0])
        grade_case(model, {"question": "删除酒店", "expected": "仅保留座位",
                           "seed": [{"type": "酒店", "value": "安静"},
                                    {"type": "座位", "value": "靠过道"}]},
                   {"score": {"status": "passed"},
                    "output": {"status": "completed", "answer": "该偏好未保存。",
                               "preferences": {"items": [{"type": "座位", "value": "靠过道"}]},
                               "tools": []}})
        self.assertIn('"absent_initial_preference_types": ["酒店"]', model.invoke.call_args.args[0])
        model.invoke.return_value.content = '{"score":2,"reason":"错误引用","answer_quote":"已保存"}'
        with self.assertRaisesRegex(ValueError, "contiguous"):
            grade_case(model, {"question": "测试", "expected": "不保存"}, entry)

    def test_incomplete_execution_not_judged(self):
        model = Mock()
        grade = grade_case(model, {"question": "测试", "expected": "不保存"},
                           {"output": {"status": "blocked"}})
        self.assertEqual(grade["status"], "not_graded")
        model.invoke.assert_not_called()

    def test_report_needs_matching_fixture_and_remote_run(self):
        cases = [{"id": "a"}]
        report = {"source_sha256": fingerprint(cases),
                  "scope": "real_graph_isolated_memory_not_web_e2e",
                  "run_name": "run", "remote_run_items": 1,
                  "results": [{"case_id": "a"}]}
        validate_report(report, cases)
        report["remote_run_items"] = 0
        with self.assertRaisesRegex(ValueError, "verified"):
            validate_report(report, cases)


if __name__ == "__main__":
    unittest.main()

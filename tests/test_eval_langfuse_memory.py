import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import NAMESPACE_URL, uuid5

from evals.langfuse_memory import evaluate_output, fingerprint, verify_remote


class LangfuseMemoryEvaluationTests(unittest.TestCase):
    def test_blocked_has_no_numeric_quality_score(self):
        score = evaluate_output({"status": "blocked", "failure_code": "model_timeout"}, {"count": 1})
        self.assertIsNone(score["passed"])
        self.assertEqual(score["status"], "blocked")

    def test_missing_output_is_not_success(self):
        self.assertIsNone(evaluate_output({}, {"count": 0})["passed"])

    def test_no_write_contract(self):
        self.assertTrue(evaluate_output(
            {"status": "completed", "preferences": {"items": []}}, {"count": 0})["passed"])

    def test_wrong_memory_fails(self):
        self.assertFalse(evaluate_output(
            {"status": "completed", "preferences": {"items": []}},
            {"count": 1, "records": [{"value_contains": "aisle"}]})["passed"])

    def test_execution_failure_overrides_matching_memory(self):
        self.assertFalse(evaluate_output(
            {"status": "completed", "preferences": {"items": []},
             "execution_failures": ["unexpected_interrupt"]}, {"count": 0})["passed"])

    def test_fingerprint_ignores_object_order_not_content(self):
        self.assertEqual(fingerprint({"a": 1, "b": 2}), fingerprint({"b": 2, "a": 1}))
        self.assertNotEqual(fingerprint({"a": 1}), fingerprint({"a": 2}))

    def test_cloud_scores_must_match_not_just_exist(self):
        report = {"dataset": "synthetic", "run_name": "run", "results": [
            {"case_id": "one", "score": {"status": "failed", "passed": False}}]}
        client = Mock()
        client.get_dataset_run.return_value.dataset_run_items = [
            SimpleNamespace(dataset_item_id=str(uuid5(NAMESPACE_URL, "synthetic:one")), trace_id="t")]
        client.api.trace.get.return_value.scores = [
            SimpleNamespace(name="execution_status", value=0.0, string_value="failed"),
            SimpleNamespace(name="memory_contract", value=0)]
        self.assertTrue(verify_remote(client, report)["verified"])
        client.api.trace.get.return_value.scores[1].value = 1
        self.assertFalse(verify_remote(client, report)["verified"])

    def test_cloud_missing_items_fails(self):
        client = Mock()
        client.get_dataset_run.return_value.dataset_run_items = []
        report = {"dataset": "synthetic", "run_name": "run", "results": [
            {"case_id": "one", "score": {"status": "passed", "passed": True}}]}
        self.assertFalse(verify_remote(client, report)["verified"])

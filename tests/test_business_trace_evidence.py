"""云端验收不得把空结果、断链或正文泄漏误判为通过。"""

from types import SimpleNamespace
import unittest

from scripts.verify_business_traces import summarize_trace


class Row(SimpleNamespace):
    def model_dump(self, **kwargs):
        return vars(self)


class BusinessTraceEvidenceTests(unittest.TestCase):
    def rows(self):
        return [
            Row(id=str(index), name=name, trace_id="trace", end_time="ended",
                parent_observation_id=None if index == 0 else "0",
                input=None, output=None, metadata={})
            for index, name in enumerate(("chat", "slot_extraction", "chat_model", "slot_commit"))
        ]

    def test_complete_metadata_only_trace_passes(self):
        self.assertTrue(summarize_trace(self.rows(), "trace")["passed"])

    def test_declared_sdk_root_can_have_nonrecording_parent_but_children_cannot(self):
        rows = self.rows()
        rows[0].parent_observation_id = "sdk-nonrecording-parent"
        self.assertTrue(summarize_trace(rows, "trace", root_id="0")["passed"])
        self.assertFalse(summarize_trace(rows, "trace", root_id="wrong")["passed"])
        rows[-1].parent_observation_id = "missing-child-parent"
        self.assertFalse(summarize_trace(rows, "trace", root_id="0")["passed"])

    def test_empty_missing_unfinished_or_foreign_trace_fails(self):
        self.assertFalse(summarize_trace([], "trace")["passed"])
        self.assertFalse(summarize_trace(self.rows()[:-1], "trace")["passed"])
        for field, value in (("end_time", None), ("trace_id", "different")):
            rows = self.rows()
            setattr(rows[-1], field, value)
            self.assertFalse(summarize_trace(rows, "trace")["passed"])

    def test_orphan_cycle_and_duplicate_roots_fail(self):
        for parent in ("missing", "3", None):
            rows = self.rows()
            rows[-1].parent_observation_id = parent
            self.assertFalse(summarize_trace(rows, "trace")["passed"])
        rows = self.rows()
        rows.append(Row(**{**vars(rows[0]), "id": "another"}))
        self.assertFalse(summarize_trace(rows, "trace")["passed"])

    def test_payload_and_private_metadata_fail_without_echoing(self):
        for field in ("input", "output"):
            rows = self.rows()
            setattr(rows[-1], field, "private")
            result = summarize_trace(rows, "trace", ("private",))
            self.assertFalse(result["passed"])
            self.assertNotIn("private", str(result["observations"]))
        rows = self.rows()
        rows[-1].metadata = {"unknown": "private"}
        self.assertFalse(summarize_trace(rows, "trace", ("private",))["passed"])

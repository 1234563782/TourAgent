"""探针验证只记录能力信号，不记录模型返回正文。"""

import unittest
from unittest.mock import Mock

from langchain_core.messages import AIMessage

from scripts.probe_structured_output import probe


class StructuredProbeTests(unittest.TestCase):
    def test_provider_ignoring_impossible_schema_is_detected(self):
        model = Mock()
        model.bind.return_value.invoke.return_value = AIMessage(content="", tool_calls=[
            {"name": "ProbeResult", "args": {"category": "hypothetical", "amount": 37}, "id": "p"}])
        rows = probe(model, "strict_tool_impossible_schema")
        self.assertTrue(rows[0]["schema_valid"])
        self.assertFalse(rows[0]["requested_schema_satisfied"])
        self.assertNotIn("tool_calls", rows[0])

    def test_connection_failure_stops_without_recording_message(self):
        class OpenAIConnectionError(Exception):
            pass
        model = Mock()
        model.bind.return_value.invoke.side_effect = OpenAIConnectionError("secret")
        rows = probe(model)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["error_category"], "OpenAIConnectionError")
        self.assertNotIn("secret", str(rows))

    def test_auth_failure_stops(self):
        class AuthError(Exception):
            status_code = 401
        model = Mock()
        model.bind.return_value.invoke.side_effect = AuthError("secret")
        rows = probe(model)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["http_status"], 401)


if __name__ == "__main__":
    unittest.main()

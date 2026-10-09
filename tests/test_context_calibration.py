"""Per-invocation calibration without exporting prompts or provider metadata."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import math
import os
from tempfile import TemporaryDirectory
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from business_travel.context_budget import (
    BudgetedModel, ContextBudgetExceeded, assistant_context, input_limit, request_size,
    select_history, slot_history, usage_calibration,
)
from business_travel.observability import Telemetry, safe_metadata
from business_travel.settings import Settings
from tests import test_context_budget as budget_fixtures
from tests import test_observability as telemetry_fixtures


def answer(tokens=17):
    return AIMessage("SENSITIVE-ANSWER", usage_metadata={
        "input_tokens": tokens, "output_tokens": 2, "total_tokens": tokens + 2},
        response_metadata={"SENSITIVE-PROVIDER": "SENSITIVE-KEY"})


class FrameConfigurationTests(unittest.TestCase):
    def test_defaults_and_environment_overrides_dotenv(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("TRAVEL_CONTEXT_FRAME_TOKENS=777\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(Settings.from_env(path).context_frame_tokens, 777)
            with patch.dict(os.environ, {"TRAVEL_CONTEXT_FRAME_TOKENS": "512"}, clear=True):
                self.assertEqual(Settings.from_env(path).context_frame_tokens, 512)
        with patch.dict(os.environ, {"TRAVEL_LOAD_DOTENV": "0"}, clear=True):
            self.assertEqual(Settings.from_env().context_frame_tokens, 1024)

    def test_zero_allowed_but_negative_bool_fraction_and_no_room_rejected(self):
        budget_fixtures.settings(context_frame_tokens=0).validate_context()
        for value in (-1, True, 1.5, "1024", 53248, 999999):
            with self.subTest(value=value), self.assertRaises(ValueError):
                budget_fixtures.settings(context_frame_tokens=value).validate_context()
        for value in ("-1", "bad", "1.5"):
            with patch.dict(os.environ, {"TRAVEL_LOAD_DOTENV": "0",
                            "TRAVEL_CONTEXT_FRAME_TOKENS": value}, clear=True):
                with self.assertRaises(ValueError):
                    Settings.from_env()

    def test_both_assemblers_use_configured_reserve_without_changing_final_limit(self):
        messages = [HumanMessage("current")]
        for reserve in (0, 512, 2048):
            config = budget_fixtures.settings(context_frame_tokens=reserve)
            fixed = [SystemMessage("rules")]
            with patch("business_travel.context_budget.select_history", wraps=select_history) as select:
                assistant_context(messages, fixed, [], config)
                self.assertEqual(select.call_args.args[2],
                                 input_limit(config, "supervisor") - request_size(fixed, []) - reserve)
                payload = {"current_message": "current"}
                slot_history(payload, messages, "rules", [], config, 16)
                slot_fixed = [SystemMessage("rules"), HumanMessage(json.dumps(
                    {**payload, "conversation": []}, ensure_ascii=False, separators=(",", ":")))]
                self.assertEqual(select.call_args.args[2],
                                 input_limit(config, "slots") - request_size(slot_fixed, []) - reserve)
            self.assertEqual(input_limit(config, "slots"), 53248)

    def test_role_budget_too_small_for_frame_fails_without_truncation(self):
        config = budget_fixtures.settings(context_frame_tokens=2048,
                                         context_role_input_tokens={"supervisor": 1024})
        config.validate_context()
        with self.assertRaises(ContextBudgetExceeded):
            assistant_context([HumanMessage("current")], [SystemMessage("rule")], [], config)


class CalibrationTests(unittest.TestCase):
    def setUp(self):
        self.records = []
        self.client, self.provider = Mock(), Mock()
        self.client.start_observation.side_effect = lambda **kw: telemetry_fixtures.FakeSpan(self.records, **kw)
        factory = patch("business_travel.observability._make_client",
                        return_value=(self.client, self.provider))
        factory.start()
        self.addCleanup(factory.stop)
        self.settings = replace(telemetry_fixtures.enabled(), model="SENSITIVE-MODEL")
        self.service = Telemetry(self.settings)
        self.addCleanup(self.service.shutdown)

    def calls(self):
        return [r for r in self.records
                if r.name == "context_budget" and r.metadata.get("budget_stage") == "invoke"]

    def request(self, request_id="request"):
        return self.service.request({}, telemetry_fixtures.IDENTITY, "session", request_id)

    def test_real_callback_nested_under_matching_call_with_actual_usage(self):
        model = BudgetedModel(FakeMessagesListChatModel(responses=[answer()]), self.settings, "slots")
        messages = [HumanMessage("SENSITIVE-PROMPT")]
        with self.request() as (_, config):
            response = model.invoke(messages, config=config)
        call, = self.calls()
        generation, = [r for r in self.records if r.name == "chat_model"]
        self.assertIs(generation.parent, call)
        self.assertEqual(call.metadata["estimated_input_tokens"], request_size(messages))
        self.assertEqual(call.metadata["input_tokens"], generation.metadata["input_tokens"])
        self.assertEqual(call.metadata["input_estimate_delta"], request_size(messages) - 17)
        self.assertEqual(call.metadata["input_estimate_ratio"], request_size(messages) / 17)
        self.assertEqual(call.metadata["usage_status"], "available")
        self.assertEqual(call.metadata["model_ref"], self.service.reference("model", "SENSITIVE-MODEL"))
        self.assertEqual(response.content, "SENSITIVE-ANSWER")
        self.assertTrue(all(r.ended == 1 for r in self.records))
        self.assertNotIn("SENSITIVE", str([(r.options, r.metadata) for r in self.records]))

    def test_repair_attempts_have_unique_refs_and_updated_estimates_and_usage(self):
        raw = Mock()
        raw.bind_tools.return_value = raw
        raw.invoke.side_effect = [answer(10), answer(300)]
        model = BudgetedModel(raw, self.settings, "reviewer").bind_tools([{
            "type": "function", "function": {"name": "submit", "description": "schema" * 50,
                                            "parameters": {"type": "object", "properties": {}}}}])
        short, repaired = [HumanMessage("first")], [HumanMessage("first"), HumanMessage("repair" * 100)]
        with self.request():
            model.invoke(short)
            model.invoke(repaired)
        first, second = [r.metadata for r in self.calls()]
        self.assertNotEqual(first["call_ref"], second["call_ref"])
        self.assertEqual(first["model_ref"], second["model_ref"])
        self.assertEqual(first["input_tokens"], 10)
        self.assertEqual(second["input_tokens"], 300)
        self.assertEqual(first["estimated_input_tokens"], request_size(short, model.tools))
        self.assertGreater(second["estimated_input_tokens"], first["estimated_input_tokens"])
        self.assertEqual(raw.invoke.call_count, 2)

    def test_missing_invalid_zero_and_negative_bias(self):
        missing = [None, {}, {"output_tokens": 5}]
        invalid = ["bad", {"input_tokens": True}, {"input_tokens": -1},
                   {"input_tokens": 1.5}, {"input_tokens": 10**12 + 1}]
        for value in missing + invalid:
            metadata = usage_calibration(SimpleNamespace(usage_metadata=value), 100)
            self.assertEqual(metadata, {"usage_status": "missing" if value in missing else "invalid"})
        zero = usage_calibration(answer(0), 100)
        self.assertEqual(zero["input_tokens"], 0)
        self.assertNotIn("input_estimate_ratio", zero)
        negative = usage_calibration(answer(200), 100)
        self.assertEqual(negative["input_estimate_delta"], -100)
        self.assertEqual(negative["input_estimate_ratio"], 0.5)
        self.assertEqual(safe_metadata(negative), negative)

    def test_missing_usage_cannot_fall_back_to_untrusted_response_metadata(self):
        response = AIMessage("private", response_metadata={"token_usage": {"prompt_tokens": 999}})
        model = BudgetedModel(Mock(invoke=Mock(return_value=response)), self.settings, "policy")
        with self.request():
            model.invoke([HumanMessage("x")])
        metadata = self.calls()[0].metadata
        self.assertEqual(metadata["usage_status"], "missing")
        self.assertNotIn("input_tokens", metadata)
        self.assertNotIn("input_estimate_delta", metadata)

    def test_blocked_call_never_invokes_and_has_no_actual_usage(self):
        raw = Mock()
        config = replace(self.settings, context_role_input_tokens={"slots": 1024})
        with self.request(), self.assertRaises(ContextBudgetExceeded):
            BudgetedModel(raw, config, "slots").invoke([HumanMessage("x" * 2000)])
        raw.invoke.assert_not_called()
        metadata = self.calls()[0].metadata
        self.assertEqual(metadata["budget_decision"], "blocked")
        self.assertEqual(metadata["usage_status"], "not_called")
        self.assertNotIn("input_tokens", metadata)

    def test_model_failure_is_preserved_without_fabricated_usage_or_extra_retry(self):
        error = RuntimeError("SENSITIVE-FAILURE")
        raw = Mock(invoke=Mock(side_effect=error))
        with self.request(), self.assertRaises(RuntimeError) as caught:
            BudgetedModel(raw, self.settings, "planner").invoke([HumanMessage("x")])
        self.assertIs(caught.exception, error)
        self.assertEqual(raw.invoke.call_count, 1)
        metadata = self.calls()[0].metadata
        self.assertEqual(metadata["usage_status"], "unavailable")
        self.assertEqual(metadata["status"], "error")
        self.assertNotIn("input_tokens", metadata)
        self.assertNotIn("SENSITIVE", str(metadata))

    def test_concurrent_calls_do_not_mix_usage_or_parents(self):
        barrier = Barrier(2)
        def worker(number):
            raw = Mock()
            def invoke(*args, **kwargs):
                barrier.wait(timeout=10)
                return answer(number)
            raw.invoke.side_effect = invoke
            with self.request(str(number)):
                BudgetedModel(raw, self.settings, "supervisor").invoke([HumanMessage("x" * number)])
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(worker, (10, 20)))
        calls = self.calls()
        self.assertEqual({r.metadata["input_tokens"] for r in calls}, {10, 20})
        self.assertEqual(len({r.metadata["call_ref"] for r in calls}), 2)
        self.assertIsNot(calls[0].parent, calls[1].parent)
        for call in calls:
            self.assertEqual(call.metadata["estimated_input_tokens"],
                             request_size([HumanMessage("x" * call.metadata["input_tokens"])]))

    def test_export_failure_does_not_change_response_or_invoke_twice(self):
        broken = Mock()
        broken.start_observation.return_value = broken
        broken.update.side_effect = RuntimeError("SENSITIVE-EXPORT")
        self.client.start_observation.side_effect = None
        self.client.start_observation.return_value = broken
        expected = answer()
        raw = Mock(invoke=Mock(return_value=expected))
        with self.request():
            actual = BudgetedModel(raw, self.settings, "policy").invoke([HumanMessage("x")])
        self.assertIs(actual, expected)
        raw.invoke.assert_called_once()

    def test_numeric_allowlist_rejects_nonfinite_bool_and_unknown_metadata(self):
        for value in (math.inf, math.nan, -1, True, "1.2", 10**13, 10**1000):
            self.assertEqual(safe_metadata({"input_estimate_ratio": value}), {})
        for value in (True, 1.5, "1", 10**13):
            self.assertEqual(safe_metadata({"input_estimate_delta": value}), {})
        self.assertEqual(safe_metadata({"model_name": "secret", "call_ref": "secret",
                                      "usage_status": "private"}), {})


class CalibrationSDKTests(unittest.TestCase):
    def test_real_sdk_preserves_calibration_and_model_parent_without_payload(self):
        from langfuse import Langfuse
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
        exporter = InMemorySpanExporter()
        original = Langfuse
        def local_client(**kwargs):
            return original(**kwargs, span_exporter=exporter)
        settings = telemetry_fixtures.enabled()
        service = Telemetry(settings)
        with patch("langfuse.Langfuse", side_effect=local_client), \
                patch("httpx.Client.send", side_effect=AssertionError("network forbidden")), \
                patch("requests.Session.send", side_effect=AssertionError("network forbidden")):
            try:
                with service.request({}, telemetry_fixtures.IDENTITY, "private-s", "private-r") as (_, config):
                    BudgetedModel(FakeMessagesListChatModel(responses=[answer()]), settings, "slots").invoke(
                        [HumanMessage("SENSITIVE-PROMPT")], config=config)
                service.client.flush()
                spans = exporter.get_finished_spans()
                call, = [s for s in spans if s.name == "context_budget"]
                generation, = [s for s in spans if s.name == "chat_model"]
                self.assertEqual(generation.parent.span_id, call.context.span_id)
                exported = str([(s.name, dict(s.attributes), s.events) for s in spans])
                for key in ("input_tokens", "estimated_input_tokens", "input_estimate_delta",
                            "input_estimate_ratio", "call_ref", "usage_status"):
                    self.assertIn(key, str(dict(call.attributes)))
                self.assertNotIn("SENSITIVE", exported)
                self.assertTrue(all(s.end_time is not None for s in spans))
            finally:
                service.shutdown()


if __name__ == "__main__":
    unittest.main()

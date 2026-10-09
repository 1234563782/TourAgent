"""Synthetic telemetry only: no real identity, model, database or cloud requests."""

from concurrent.futures import ThreadPoolExecutor
import builtins
from dataclasses import replace
from datetime import date
import json
import os
from pathlib import Path
from threading import Barrier
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

import httpx
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from openai import APIStatusError

from business_travel.memory_pg import MemoryIdentity
from business_travel.observability import (
    Telemetry, MetadataCallback, Observation, configuration_problem, observation,
    record_event, safe_metadata, safe_usage_details,
)
from tests.test_auth import settings
from tests import test_web_chat as web_fixtures
from tests import test_travel_graph as graph_fixtures


IDENTITY = MemoryIdentity("synthetic-company", "synthetic-user")


def enabled():
    return replace(settings(), langfuse_enabled=True,
                   langfuse_base_url="https://telemetry.example",
                   langfuse_public_key="pk-synthetic-" + uuid4().hex,
                   langfuse_secret_key="sk-synthetic-private",
                   langfuse_hash_key="synthetic-hash-key-for-tests-only-123456789")


class FakeSpan:
    def __init__(self, records, name, parent=None, **kwargs):
        self.records, self.name, self.parent = records, name, parent
        self.metadata = dict(kwargs.get("metadata", {}))
        self.options = kwargs
        self.ended = 0
        records.append(self)

    def start_observation(self, **kwargs):
        return FakeSpan(self.records, parent=self, **kwargs)

    def update(self, **kwargs):
        self.metadata.update(kwargs.get("metadata", {}))
        self.options.update({key: value for key, value in kwargs.items() if key != "metadata"})

    def end(self):
        self.ended += 1


class TelemetryTests(unittest.TestCase):
    def test_slot_phase_is_allowlisted_without_arbitrary_text(self):
        for phase in ("TurnRoute", "TripExtraction", "ExpenseExtraction"):
            self.assertEqual(safe_metadata({"slot_phase": phase}), {"slot_phase": phase})
        self.assertEqual(safe_metadata({"slot_phase": "private-user-text", "phase": "private"}), {})
        with self.telemetry.request({}, IDENTITY, "s", "r"):
            with observation("slot_protocol", slot_phase="TurnRoute"):
                pass
        child = next(span for span in self.records if span.name == "slot_protocol")
        self.assertEqual(child.metadata["slot_phase"], "TurnRoute")

    def setUp(self):
        self.records = []
        self.client, self.provider = Mock(), Mock()
        self.client.start_observation.side_effect = lambda **kw: FakeSpan(self.records, **kw)
        self.factory = patch("business_travel.observability._make_client",
                             return_value=(self.client, self.provider)).start()
        self.addCleanup(patch.stopall)
        self.telemetry = Telemetry(enabled())
        self.addCleanup(self.telemetry.shutdown)

    def test_disabled_never_initializes_sdk_even_with_keys_in_environment(self):
        off = Telemetry(replace(enabled(), langfuse_enabled=False))
        config = {"configurable": {"thread_id": "private-thread"}}
        original_import = builtins.__import__
        def guarded_import(name, *args, **kwargs):
            if name == "langfuse" or name.startswith("langfuse."):
                raise AssertionError("disabled telemetry imported SDK")
            return original_import(name, *args, **kwargs)
        with patch.dict(os.environ, {"LANGFUSE_TRACING_ENABLED": "true"}), \
                patch("builtins.__import__", side_effect=guarded_import):
            with off.request(config, IDENTITY, "session", "request") as (span, copied):
                record_event("slot_commit", slots_committed=True)
                self.assertIs(config, copied)
                self.assertIsNone(span.raw)
        off.shutdown()
        self.factory.assert_not_called()

    def test_missing_or_invalid_configuration_is_fail_open_without_sdk(self):
        for config in (replace(enabled(), langfuse_secret_key=""),
                       replace(enabled(), langfuse_hash_key="short"),
                       replace(enabled(), langfuse_base_url="http://remote.example"),
                       replace(enabled(), langfuse_base_url="https://user:secret@host.example"),
                       replace(enabled(), langfuse_base_url="https://host.example?secret=x")):
            service = Telemetry(config)
            self.assertIsNotNone(configuration_problem(config))
            with self.assertLogs("business_travel.observability", "WARNING") as logs:
                with service.request({}, IDENTITY, "s", "r"):
                    pass
            self.assertNotIn("secret=x", str(logs.output))
        self.factory.assert_not_called()

    def test_client_failure_does_not_cancel_business_and_warns_once(self):
        self.factory.side_effect = RuntimeError("private-api-secret")
        executed = []
        with self.assertLogs("business_travel.observability", "WARNING") as logs:
            for _ in range(2):
                with self.telemetry.request({}, IDENTITY, "s", "r"):
                    executed.append(True)
        self.assertEqual(executed, [True, True])
        self.assertEqual(len(logs.output), 1)
        self.assertNotIn("private-api-secret", str(logs.output))

    def test_start_update_and_end_failures_do_not_mask_business(self):
        for method in ("start_observation", "update", "end"):
            with self.subTest(method=method):
                broken = Mock()
                broken.start_observation.return_value = broken
                getattr(broken, method).side_effect = RuntimeError("private-error")
                self.client.start_observation.side_effect = None
                self.client.start_observation.return_value = broken
                with self.telemetry.request({}, IDENTITY, "s", "r") as (root, config):
                    root.update(status="ok")
                    with observation("slot_extraction"):
                        record_event("slot_commit", slots_committed=True)

    def test_business_exception_is_preserved_not_swallowed_or_replayed(self):
        original = RuntimeError("business-secret")
        with self.assertRaises(RuntimeError) as caught:
            with self.telemetry.request({}, IDENTITY, "s", "r"):
                raise original
        self.assertIs(caught.exception, original)
        self.assertEqual(self.records[0].metadata["execution_status"], "failed")
        self.assertNotIn("business-secret", str(self.records[0].metadata))

    def test_identity_is_keyed_scoped_and_deterministic(self):
        refs = []
        for identity, session in ((IDENTITY, "s"), (IDENTITY, "s"),
                                  (IDENTITY, "s2"), (MemoryIdentity("other-company", "synthetic-user"), "s")):
            with self.telemetry.request({}, identity, session, "same-request"):
                refs.append(dict(self.records[-1].metadata))
        self.assertEqual(refs[0], refs[1])
        self.assertNotEqual(refs[0]["request_ref"], refs[2]["request_ref"])
        self.assertNotEqual(refs[0]["user_ref"], refs[3]["user_ref"])
        self.assertNotIn(IDENTITY.user_id, str(refs))
        self.assertNotEqual(self.telemetry.reference("user", "x"),
                            Telemetry(replace(enabled(), langfuse_hash_key="another" * 8)).reference("user", "x"))

    def test_nested_whitelist_rejects_raw_content_and_unknown_labels(self):
        payload = {"messages": [{"content": "private-policy"}], "status": "private-password",
                   "revision": True, "request_ref": "user@email.test", "count": -1,
                   "attempt": 2, "slots_committed": False, "http_status": 500,
                   "output_tokens": float("inf")}
        self.assertEqual(safe_metadata(payload), {"attempt": 2, "slots_committed": False, "http_status": 500})
        with self.telemetry.request({}, IDENTITY, "s", "r"):
            record_event("private-tool-name", **payload)
        self.assertEqual(self.records[-1].name, "tool")
        self.assertNotIn("private-", str(self.records[-1].metadata))

    def test_usage_is_written_to_langfuse_native_usage_details(self):
        usage = {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7}
        self.assertEqual(safe_usage_details(usage), {"input": 5, "output": 2, "total": 7})
        with self.telemetry.request({}, IDENTITY, "s", "r") as (_, config):
            callback = config["callbacks"][-1]
            callback.on_chat_model_start({"name": "model"}, [], run_id="llm")
            callback.on_llm_end(LLMResult(generations=[[ChatGeneration(message=AIMessage(
                "answer", usage_metadata=usage))]]), run_id="llm")
        generation = next(span for span in self.records if span.name == "chat_model")
        self.assertEqual(generation.options["usage_details"],
                         {"input": 5, "output": 2, "total": 7})
        self.assertEqual(generation.metadata["total_tokens"], 7)

    def test_invalid_usage_is_not_exported_as_native_usage(self):
        self.assertEqual(safe_usage_details({
            "input_tokens": True, "output_tokens": -1, "total_tokens": 1.5,
        }), {})

    def test_callback_ignores_prompts_tools_outputs_metadata_and_error_body(self):
        with self.telemetry.request({}, IDENTITY, "s", "r") as (_, config):
            callback = config["callbacks"][-1]
            callback.on_chat_model_start({"api_key": "private-key"}, [["private-prompt"]],
                                         run_id="llm", metadata={"private": "secret"})
            callback.on_llm_end(LLMResult(generations=[[ChatGeneration(message=AIMessage(
                "private-output", usage_metadata={"input_tokens": 5, "output_tokens": 2, "total_tokens": 7}))]],
                llm_output={"secret": "private-provider"}), run_id="llm")
            callback.on_tool_start({"name": "private-tool"}, "private-args", run_id="tool")
            response = httpx.Response(500, request=httpx.Request("POST", "https://private.example"))
            callback.on_tool_error(APIStatusError("private-body", response=response,
                                                  body={"secret": "private-key"}), run_id="tool")
        exported = str([(x.name, x.options, x.metadata) for x in self.records])
        self.assertNotIn("private-", exported)
        self.assertEqual(self.records[1].metadata["input_tokens"], 5)
        self.assertEqual(self.records[2].metadata["http_status"], 500)

    def test_concurrent_requests_do_not_share_active_parent(self):
        barrier = Barrier(2)
        def worker(number):
            with self.telemetry.request({}, IDENTITY, str(number), str(number)):
                barrier.wait(timeout=5)
                with observation("slot_extraction"):
                    record_event("slot_attempt", attempt=number)
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(worker, [1, 2]))
        children = [r for r in self.records if r.name == "slot_attempt"]
        self.assertEqual(len(children), 2)
        self.assertIsNot(children[0].parent.parent, children[1].parent.parent)
        count = len(self.records)
        record_event("slot_attempt", attempt=3)
        self.assertEqual(len(self.records), count)
        self.factory.assert_called_once()

    def test_shutdown_once_after_use_and_no_flush_per_request(self):
        for i in range(2):
            with self.telemetry.request({}, IDENTITY, "s", str(i)):
                pass
        self.client.flush.assert_not_called()
        self.telemetry.shutdown()
        self.telemetry.shutdown()
        self.client.shutdown.assert_called_once()
        self.provider.shutdown.assert_called_once()

    def test_graph_interrupt_is_not_recorded_as_tool_failure(self):
        from langgraph.errors import GraphInterrupt
        with self.telemetry.request({}, IDENTITY, "s", "r") as (_, config):
            callback = config["callbacks"][-1]
            callback.on_tool_start({"name": "save_trip"}, "private", run_id="t")
            callback.on_tool_error(GraphInterrupt(), run_id="t")
        self.assertEqual(self.records[-1].metadata["status"], "awaiting_confirmation")

    def test_shutdown_failure_still_closes_provider_and_stays_idempotent(self):
        with self.telemetry.request({}, IDENTITY, "s", "r"):
            pass
        self.client.shutdown.side_effect = RuntimeError("private-shutdown-error")
        with self.assertLogs("business_travel.observability", "WARNING") as logs:
            self.telemetry.shutdown()
            self.telemetry.shutdown()
        self.client.shutdown.assert_called_once()
        self.provider.shutdown.assert_called_once()
        self.assertNotIn("private-shutdown-error", str(logs.output))

    def test_unfinished_callback_spans_are_closed_as_interrupted(self):
        with self.telemetry.request({}, IDENTITY, "s", "r") as (_, config):
            config["callbacks"][-1].on_tool_start({"name": "query_weather"}, "private", run_id="t")
        self.assertEqual(self.records[-1].metadata["status"], "interrupted")
        self.assertTrue(all(record.ended == 1 for record in self.records))

    def test_policy_retrieval_records_outcome_without_source_text(self):
        from business_travel.policy_rag import explain_policy
        evidence = {"status": "ok", "context_incomplete": True, "sources": [{
            "document_id": 1, "version": "private-version", "source_sha256": "a" * 64,
            "chunk": "private-chunk", "source": "private-document",
            "text": "private-policy-content",
        }]}
        for retrieved, expected in ((evidence, "incomplete"),
                                    ({"status": "empty", "sources": []}, "insufficient"),
                                    (RuntimeError("private-database-url"), "unavailable")):
            with self.subTest(expected=expected):
                self.records.clear()
                store, factory = Mock(), Mock()
                if isinstance(retrieved, Exception):
                    store.search.side_effect = retrieved
                else:
                    store.search.return_value = retrieved
                with self.telemetry.request({}, IDENTITY, "s", uuid4().hex):
                    result = explain_policy(store, factory, enabled(), "private-question")
                self.assertEqual(result["rag"]["status"], expected)
                factory.assert_not_called()
                retrieve = next(r for r in self.records if r.name == "policy_retrieve")
                terminal = next(r for r in self.records if r.name == "policy_result")
                metadata = {"status": expected, "retrievals": 1}
                if expected == "unavailable":
                    metadata.update(policy_stage="policy_retrieve",
                                    policy_failure="policy_retrieval_failed")
                self.assertEqual(terminal.metadata, metadata)
                self.assertEqual(retrieve.parent.name, "policy_rag")
                self.assertNotIn("private-", str([(r.name, r.metadata) for r in self.records]))
                self.assertTrue(all(r.ended == 1 for r in self.records))


class SettingsTelemetryTests(unittest.TestCase):
    def test_env_explicit_opt_in_and_secrets_hidden(self):
        with patch.dict(os.environ, {"TRAVEL_LOAD_DOTENV": "0", "TRAVEL_LANGFUSE_ENABLED": "1",
                "LANGFUSE_BASE_URL": "https://telemetry.example",
                "LANGFUSE_PUBLIC_KEY": "private-pk", "LANGFUSE_SECRET_KEY": "private-sk",
                "TRAVEL_LANGFUSE_HASH_KEY": "private-hash" * 4}, clear=True):
            from business_travel.settings import Settings
            config = Settings.from_env()
        self.assertTrue(config.langfuse_enabled)
        self.assertNotIn("private-", repr(config))
        self.assertIsNone(configuration_problem(config))
        self.assertIsNone(configuration_problem(replace(config, langfuse_base_url="http://127.0.0.1:3000")))


class RealSDKTests(unittest.TestCase):
    def test_real_sdk_exports_only_synthetic_metadata_and_nested_model_without_network(self):
        from langfuse import Langfuse
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
        from langchain_core.language_models.fake_chat_models import FakeListChatModel
        from langgraph.graph import StateGraph, START, END, MessagesState

        exporter = InMemorySpanExporter()
        original = Langfuse
        def local_client(**kwargs):
            return original(**kwargs, span_exporter=exporter)

        model = FakeListChatModel(responses=["SENSITIVE-OUTPUT"])
        def node(state):
            with observation("slot_extraction"):
                answer = model.invoke(state["messages"])
                record_event("slot_commit", slots_committed=False, status="rejected")
                return {"messages": [answer]}
        builder = StateGraph(MessagesState)
        builder.add_node("prepare", node)
        builder.add_edge(START, "prepare")
        builder.add_edge("prepare", END)
        graph = builder.compile()
        service = Telemetry(enabled())
        with patch("langfuse.Langfuse", side_effect=local_client), \
                patch("httpx.Client.send", side_effect=AssertionError("network forbidden")), \
                patch("requests.Session.send", side_effect=AssertionError("network forbidden")):
            try:
                with service.request({}, IDENTITY, "private-session", "private-request") as (root, config):
                    result = graph.invoke({"messages": [HumanMessage("SENSITIVE-PROMPT")]}, config)
                    root.update(execution_status="completed", checkpoint_committed=True)
                service.client.flush()
                spans = exporter.get_finished_spans()
                self.assertEqual(result["messages"][-1].content, "SENSITIVE-OUTPUT")
                self.assertTrue({"chat", "slot_extraction", "chat_model", "slot_commit"}
                                <= {s.name for s in spans})
                by_name = {s.name: s for s in spans}
                self.assertEqual(by_name["chat_model"].parent.span_id, by_name["slot_extraction"].context.span_id)
                self.assertEqual(len({s.context.trace_id for s in spans}), 1)
                from scripts.verify_business_traces import summarize_trace
                from tests.test_business_trace_evidence import Row
                trace_id = f"{by_name['chat'].context.trace_id:032x}"
                root_id = f"{by_name['chat'].context.span_id:016x}"
                rows = [Row(
                    id=f"{span.context.span_id:016x}", name=span.name, trace_id=trace_id,
                    parent_observation_id=f"{span.parent.span_id:016x}" if span.parent else None,
                    end_time=span.end_time, input=None, output=None, metadata={},
                ) for span in spans]
                self.assertTrue(summarize_trace(rows, trace_id, root_id=root_id)["passed"])
                exported = str([(s.name, dict(s.attributes), dict(s.resource.attributes), s.events) for s in spans])
                for secret in ("SENSITIVE", "private-session", "private-request", IDENTITY.user_id,
                               IDENTITY.company_id, service.settings.langfuse_secret_key):
                    self.assertNotIn(secret, exported)
                self.assertTrue(all(s.end_time is not None for s in spans))
            finally:
                service.shutdown()


class WebTelemetryTests(unittest.TestCase):
    def setUp(self):
        web_fixtures.ChatTests.setUp(self)
        self.records = []
        self.sdk, self.provider = Mock(), Mock()
        self.sdk.start_observation.side_effect = lambda **kw: FakeSpan(self.records, **kw)
        factory = patch("business_travel.observability._make_client",
                        return_value=(self.sdk, self.provider))
        factory.start()
        self.addCleanup(factory.stop)
        self.service = Telemetry(enabled())
        self.addCleanup(self.service.shutdown)
        from business_travel.auth import AuthSettings
        from business_travel.web import create_app
        from fastapi.testclient import TestClient
        with patch("business_travel.web.Telemetry", return_value=self.service):
            self.client = TestClient(create_app(enabled(), AuthSettings(), self.auth),
                                     base_url="http://127.0.0.1:8000")
        self.addCleanup(self.client.close)

    def send(self, ident, stream=False):
        return self.client.post("/api/chat/stream" if stream else "/api/chat",
            json={"request_id": ident, "message": "private-message"},
            headers={"Origin": "http://127.0.0.1:8000"})

    def test_history_and_duplicate_request_do_not_create_execution_trace(self):
        from business_travel.chat_runs import request_digest
        from business_travel.web import ChatBody
        self.assertEqual(self.client.get("/api/chat").status_code, 200)
        self.assertEqual(self.records, [])
        ident = uuid4().hex
        self.assertEqual(self.send(ident).status_code, 200)
        self.runs.get.return_value = {"id": ident, "status": "completed", "stage": "assistant", "checkpoint_id": "v1",
            "request_hash": request_digest(ChatBody(request_id=ident, message="private-message"))}
        self.assertEqual(self.send(ident).status_code, 200)
        self.graph.stream.assert_called_once()
        self.assertEqual(len([r for r in self.records if r.name == "chat"]), 1)

    def test_sse_business_rejection_is_not_http_execution_failure(self):
        def stream(*args, **kwargs):
            record_event("slot_commit", status="error", slots_committed=False)
            yield "custom", {"stage": "understand"}
        self.graph.stream.side_effect = stream
        response = self.send(uuid4().hex, stream=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("event: done", response.text)
        root = next(r for r in self.records if r.name == "chat")
        event = next(r for r in self.records if r.name == "slot_commit")
        self.assertEqual(root.metadata["execution_status"], "completed")
        self.assertTrue(root.metadata["checkpoint_committed"])
        self.assertEqual(event.metadata["status"], "error")
        self.assertFalse(event.metadata["slots_committed"])
        self.assertIs(event.parent, root)
        self.assertNotIn("private-message", str([r.metadata for r in self.records]))

    def test_failed_stream_finishes_trace_and_does_not_commit_success(self):
        def stream(*args, **kwargs):
            yield "custom", {"stage": "understand"}
            raise RuntimeError("private-provider-error")
        self.graph.stream.side_effect = stream
        response = self.send(uuid4().hex, stream=True)
        self.assertIn("event: error", response.text)
        self.assertNotIn("event: done", response.text)
        self.assertEqual(self.records[0].metadata["execution_status"], "failed")
        self.assertNotIn("checkpoint_committed", self.records[0].metadata)
        self.assertEqual(self.records[0].ended, 1)
        self.graph.stream.assert_called_once()

    def test_startup_and_shutdown_close_client_after_request(self):
        with self.client:
            self.assertEqual(self.send(uuid4().hex).status_code, 200)
            self.sdk.shutdown.assert_not_called()
        self.sdk.shutdown.assert_called_once()

    def test_sdk_failure_does_not_change_web_answer(self):
        self.sdk.start_observation.side_effect = RuntimeError("private-sdk-error")
        response = self.send(uuid4().hex)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["run"]["status"], "completed")
        self.graph.stream.assert_called_once()


class BusinessTelemetryTests(unittest.TestCase):
    setUp = graph_fixtures.GraphTests.setUp
    tearDown = graph_fixtures.GraphTests.tearDown
    build = graph_fixtures.GraphTests.build

    def run_traced(self, graph, message):
        records, client = [], Mock()
        client.start_observation.side_effect = lambda **kw: FakeSpan(records, **kw)
        service = Telemetry(enabled())
        try:
            with patch("business_travel.observability._make_client", return_value=(client, Mock())):
                with service.request(self.config, IDENTITY, "session", uuid4().hex) as (_, config):
                    result = graph.invoke({"messages": [HumanMessage(message)]}, config)
            return result, records
        finally:
            service.shutdown()

    def test_planner_query_result_and_task_version_are_observed_without_candidates(self):
        graph = self.build([], provider=graph_fixtures.FixtureProvider(empty=True),
                           slots=[graph_fixtures.slot_response(self.trip, "plan_trip")])
        result, records = self.run_traced(graph, graph_fixtures.PLAN_TEXT)
        self.assertEqual(result["draft"]["status"], "incomplete")
        query = next(r for r in records if r.name == "query_flight")
        self.assertEqual(query.metadata["status"], "empty")
        self.assertEqual(query.metadata["count"], 0)
        self.assertEqual(next(r for r in records if r.name == "slot_commit").metadata["slots_committed"], True)
        task = next(r for r in records if r.name == "task_result")
        self.assertEqual(task.metadata["revision"], 1)
        self.assertNotIn("Beijing", str([r.metadata for r in records]))
        self.assertTrue(all(r.ended == 1 for r in records))

    def test_rejected_proposal_does_not_report_committed_slots(self):
        bad = AIMessage("private-invalid-proposal")
        graph = self.build([], slots=[bad] * self.settings.slot_max_attempts)
        result, records = self.run_traced(graph, "private-user-message")
        self.assertIsNotNone(result["slot_error"])
        commit = next(r for r in records if r.name == "slot_commit")
        self.assertFalse(commit.metadata["slots_committed"])
        self.assertEqual(len([r for r in records if r.name == "slot_validation"]),
                         self.settings.slot_max_attempts)
        self.assertNotIn("private-", str([r.metadata for r in records]))

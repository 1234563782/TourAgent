"""Synthetic content export; no cloud, model, supplier or database calls."""

from dataclasses import replace
import json
import os
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from business_travel.observability import Telemetry, configuration_problem, observation
from business_travel.settings import Settings
from business_travel.telemetry_content import ContentFilter, REDACTED
from tests.test_observability import enabled, IDENTITY, FakeSpan
from tests import test_observability as telemetry_fixtures


def settings():
    return replace(enabled(), langfuse_capture_content=True,
                   api_key="synthetic-provider-credential")


class ContentFilterTests(unittest.TestCase):
    def setUp(self):
        self.filter = ContentFilter(settings())

    def test_text_and_tool_schema_are_preserved(self):
        payload = {
            "messages": [SystemMessage("Use retrieved evidence."), HumanMessage("Check a train seat.")],
            "tools": [{"type": "function", "function": {"name": "lookup",
                       "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}],
            "input_tokens": 23,
        }
        result = self.filter.clean(payload)
        self.assertEqual(result["messages"][0]["content"], "Use retrieved evidence.")
        self.assertEqual(result["tools"], payload["tools"])
        self.assertEqual(result["input_tokens"], 23)

    def test_nested_json_and_config_secrets_are_redacted(self):
        payload = {"args": json.dumps({"user_id": "raw-user", "password": "made-up-password",
                   "text": settings().api_key, "nested": {"apiKey": "nested-credential"}}),
                   "content": 'Bearer fabricated-token password="secret with spaces" a@sample.com 13800138000'}
        text = json.dumps(self.filter.clean(payload))
        for private in ("raw-user", "made-up-password", settings().api_key,
                        "nested-credential", "fabricated-token", "secret with spaces",
                        "a@sample.com", "13800138000"):
            self.assertNotIn(private, text)
        self.assertIn(REDACTED, text)
        self.assertEqual(self.filter.clean(self.filter.clean(payload)), self.filter.clean(payload))

    def test_urls_cookie_and_non_text_content(self):
        result = self.filter.clean({
            "text": "https://alice:made-up-pass@example.com/path?token=some-token&city=Beijing\n"
                    "Cookie: session=opaque; other=hidden\npublic",
            "image_url": {"url": "https://example.com/private-image"},
            "file_data": "data:image/png;base64,AAAA",
        })
        text = json.dumps(result)
        for value in ("alice", "made-up-pass", "some-token", "opaque", "hidden", "private-image", "AAAA"):
            self.assertNotIn(value, text)
        self.assertIn("public", text)
        self.assertIn("city=Beijing", text)

    def test_message_omits_provider_metadata_and_artifact(self):
        message = ToolMessage(content="tool answer", tool_call_id="call-1",
                              artifact={"password": "artifact-secret"},
                              response_metadata={"credential": "provider-secret"})
        result = self.filter.clean(message)
        self.assertEqual(result["content"], "tool answer")
        self.assertEqual(result["tool_call_id"], "call-1")
        self.assertNotIn("artifact", result)
        self.assertNotIn("response_metadata", result)

    def test_unknown_objects_not_stringified_cycles_and_budget(self):
        class Forbidden:
            def __str__(self):
                raise AssertionError("must not stringify")
            __repr__ = __str__
        cycle = []
        cycle.append(cycle)
        result = self.filter.clean({"object": Forbidden(), "cycle": cycle})
        self.assertTrue(result["_truncated"])
        bounded = ContentFilter(replace(settings(), langfuse_content_max_chars=1024))
        for value in ("x" * 2048, ["x" * 50] * 10000, {"k" * 3000: "v"}, [[[]]] * 13000):
            cleaned = bounded.clean(value)
            self.assertTrue(cleaned["_truncated"])
            self.assertLessEqual(len(json.dumps(cleaned, ensure_ascii=False)), 1024)

    def test_config_defaults_and_explicit_opt_in(self):
        with patch.dict(os.environ, {"TRAVEL_LOAD_DOTENV": "0"}, clear=True):
            self.assertFalse(Settings.from_env().langfuse_capture_content)
            with patch.dict(os.environ, {"TRAVEL_LANGFUSE_CAPTURE_CONTENT": "1",
                                        "TRAVEL_LANGFUSE_CONTENT_MAX_CHARS": "2048"}):
                config = Settings.from_env()
                self.assertTrue(config.langfuse_capture_content)
                self.assertEqual(config.langfuse_content_max_chars, 2048)
        self.assertIsNotNone(configuration_problem(replace(settings(), langfuse_content_max_chars=0)))


class ContentCallbackTests(unittest.TestCase):
    def setUp(self):
        self.records = []
        client = Mock()
        client.start_observation.side_effect = lambda **kw: FakeSpan(self.records, **kw)
        factory = patch("business_travel.observability._make_client", return_value=(client, Mock()))
        factory.start()
        self.addCleanup(factory.stop)
        self.service = Telemetry(settings())
        self.addCleanup(self.service.shutdown)

    def test_model_tools_parameters_answers_and_usage(self):
        with self.service.request({}, IDENTITY, "s", "r") as (_, config):
            callback = config["callbacks"][-1]
            callback.on_chat_model_start({"secret": "serialized-secret"}, [
                [SystemMessage("system instructions"), HumanMessage("train question")]],
                run_id="model", invocation_params={"tools": [{"name": "query_train"}],
                                                  "api_key": "invocation-secret"})
            answer = AIMessage("answer", tool_calls=[{
                "id": "call-1", "name": "query_train", "args": {"city": "Beijing"}}],
                usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
            callback.on_llm_end(LLMResult(generations=[[ChatGeneration(message=answer)]],
                                         llm_output={"secret": "response-secret"}), run_id="model")
            callback.on_tool_start({"name": "query_train"}, '{"city":"Beijing","api_key":"tool-secret"}',
                                   run_id="tool")
            callback.on_tool_end(ToolMessage(content='{"price":300}', tool_call_id="call-1"), run_id="tool")
        model, tool = self.records[1:3]
        self.assertIn("system instructions", json.dumps(model.options))
        self.assertEqual(model.options["input"]["parameters"]["tools"], [{"name": "query_train"}])
        self.assertEqual(model.options["output"][0][0]["tool_calls"][0]["args"], {"city": "Beijing"})
        self.assertEqual(model.metadata["input_tokens"], 10)
        self.assertEqual(tool.options["output"]["content"], {"price": 300})
        for private in ("serialized-secret", "invocation-secret", "response-secret", "tool-secret"):
            self.assertNotIn(private, json.dumps([span.options for span in self.records]))

    def test_disabled_mode_never_evaluates_content(self):
        service = Telemetry(replace(settings(), langfuse_capture_content=False))
        try:
            with service.request({}, IDENTITY, "s", "r") as (root, _):
                root.content(input=lambda: self.fail("disabled mode evaluated payload"))
            self.assertNotIn("input", self.records[0].options)
        finally:
            service.shutdown()

    def test_content_failures_do_not_escape_into_business(self):
        with self.service.request({}, IDENTITY, "s", "r") as (root, _):
            root.content(input=lambda: 1 / 0)
            with patch.object(root.raw, "update", side_effect=RuntimeError("sink failed")):
                root.content(output="still complete")
            root.update(execution_status="completed")
        self.assertEqual(self.records[0].metadata["execution_status"], "completed")

    def test_concurrent_requests_keep_distinct_content(self):
        from concurrent.futures import ThreadPoolExecutor
        def run(index):
            with self.service.request({}, IDENTITY, str(index), str(index)) as (root, config):
                root.content(input={"turn": index})
                callback = config["callbacks"][-1]
                callback.on_tool_start({"name": "query_train"}, str(index), run_id=uuid4())
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(run, range(8)))
        children = [span for span in self.records if span.name == "query_train"]
        self.assertEqual(len(children), 8)
        for child in children:
            self.assertEqual(child.options["input"], str(child.parent.options["input"]["turn"]))

    def test_retrieval_sources_and_terminal_result(self):
        from business_travel.policy_rag import explain_policy
        store, factory = Mock(), Mock()
        store.search.return_value = {
            "status": "ok", "context_incomplete": True, "sources": [{
                "document_id": 1, "version": "synthetic-version", "source_sha256": "a" * 64,
                "chunk": "chunk-1", "source": "synthetic-policy", "text": "synthetic clause",
            }],
        }
        with self.service.request({}, IDENTITY, "s", "r"):
            result = explain_policy(store, factory, settings(), "synthetic question")
        retrieve = next(row for row in self.records if row.name == "policy_retrieve")
        terminal = next(row for row in self.records if row.name == "policy_result")
        self.assertEqual(retrieve.options["input"]["query"], "synthetic question")
        self.assertEqual(retrieve.options["output"]["sources"][0]["text"], "synthetic clause")
        self.assertEqual(terminal.options["output"], result)
        factory.assert_not_called()


class WebContentTests(telemetry_fixtures.WebTelemetryTests):
    def setUp(self):
        super().setUp()
        self.service.settings = replace(self.service.settings, langfuse_capture_content=True)
        self.service.content_filter = ContentFilter(self.service.settings)

    def test_new_answer_only_without_history_replay(self):
        from tests.test_web_chat import state
        history = [HumanMessage("old question"), AIMessage("old answer")]
        self.graph.get_state.side_effect = [
            state(history), state([*history, HumanMessage("private-message"), AIMessage("new answer")])]
        self.assertEqual(self.send(uuid4().hex, stream=True).status_code, 200)
        root = self.records[0]
        self.assertEqual(root.options["input"]["message"], "private-message")
        self.assertEqual(root.options["output"]["messages"], [{"role": "assistant", "text": "new answer"}])
        self.assertNotIn("old answer", json.dumps(root.options))

    def test_no_new_answer_does_not_reuse_previous_answer(self):
        from tests.test_web_chat import state
        self.graph.get_state.return_value = state([HumanMessage("old question"), AIMessage("old answer")])
        self.assertEqual(self.send(uuid4().hex).status_code, 200)
        self.assertEqual(self.records[0].options["output"]["messages"], [])

    def test_changed_history_is_omitted_instead_of_replayed(self):
        from tests.test_web_chat import state
        self.graph.get_state.side_effect = [
            state([HumanMessage("old question"), AIMessage("old answer")]),
            state([HumanMessage("changed history"), AIMessage("uncertain answer")])]
        self.assertEqual(self.send(uuid4().hex).status_code, 200)
        self.assertIn("_omitted", self.records[0].options["output"])


class BusinessContentTests(telemetry_fixtures.BusinessTelemetryTests):
    def run_traced(self, graph, message):
        records, client = [], Mock()
        client.start_observation.side_effect = lambda **kw: FakeSpan(records, **kw)
        service = Telemetry(settings())
        try:
            with patch("business_travel.observability._make_client", return_value=(client, Mock())):
                with service.request(self.config, IDENTITY, "session", uuid4().hex) as (_, config):
                    result = graph.invoke({"messages": [HumanMessage(message)]}, config)
            return result, records
        finally:
            service.shutdown()

    def test_planner_records_request_and_empty_result(self):
        fixtures = telemetry_fixtures.graph_fixtures
        graph = self.build([], provider=fixtures.FixtureProvider(empty=True),
                           slots=[fixtures.slot_response(self.trip, "plan_trip")])
        _, records = self.run_traced(graph, fixtures.PLAN_TEXT)
        query = next(row for row in records if row.name == "query_flight")
        self.assertEqual(query.options["input"]["trip"]["origin"], self.trip["origin"])
        self.assertEqual(query.options["output"]["status"], "empty")
        self.assertEqual(query.options["output"]["candidates"], [])

    def test_rejected_slots_include_actionable_feedback(self):
        graph = self.build([], slots=[AIMessage("invalid synthetic proposal")] * self.settings.slot_max_attempts)
        _, records = self.run_traced(graph, "synthetic question")
        validation = [row for row in records if row.name == "slot_validation"]
        self.assertTrue(validation)
        self.assertTrue(all(row.options.get("output") for row in validation))


class RealContentSDKTests(unittest.TestCase):
    def test_streamed_model_and_tools_export_redacted_content_without_network(self):
        from langfuse import Langfuse
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
        from langchain_core.language_models.fake_chat_models import FakeListChatModel
        from langgraph.graph import StateGraph, START, END, MessagesState
        exporter = InMemorySpanExporter()
        original = Langfuse
        service = Telemetry(settings())
        model = FakeListChatModel(responses=["synthetic answer"])
        def node(state):
            with observation("assistant"):
                chunks = list(model.stream(state["messages"]))
                answer = chunks[0]
                for chunk in chunks[1:]:
                    answer += chunk
                with observation("query_train", kind="tool") as span:
                    span.content(input={"city": "Beijing", "password": "fabricated-pass"},
                                 output={"price": 300})
                return {"messages": [answer]}
        builder = StateGraph(MessagesState)
        builder.add_node("answer", node)
        builder.add_edge(START, "answer")
        builder.add_edge("answer", END)
        graph = builder.compile()
        with patch("langfuse.Langfuse", side_effect=lambda **kw: original(**kw, span_exporter=exporter)), \
                patch("httpx.Client.send", side_effect=AssertionError("network forbidden")), \
                patch("requests.Session.send", side_effect=AssertionError("network forbidden")):
            try:
                with service.request({}, IDENTITY, "private-session", "private-request") as (root, config):
                    result = graph.invoke({"messages": [HumanMessage(
                        "synthetic question " + settings().api_key)]}, config)
                    root.content(output=result["messages"][-1])
                service.client.flush()
                spans = exporter.get_finished_spans()
                rows = {span.name: span for span in spans}
                self.assertTrue({"chat", "assistant", "chat_model", "query_train"} <= rows.keys())
                attributes = dict(rows["chat_model"].attributes)
                self.assertIn("synthetic question", attributes["langfuse.observation.input"])
                self.assertIn("synthetic answer", attributes["langfuse.observation.output"])
                self.assertIn("300", str(rows["query_train"].attributes))
                self.assertEqual(rows["chat_model"].parent.span_id, rows["assistant"].context.span_id)
                self.assertEqual(len({span.context.trace_id for span in spans}), 1)
                exported = str([(dict(span.attributes), span.events) for span in spans])
                for private in (settings().api_key, "fabricated-pass", "private-session", "private-request"):
                    self.assertNotIn(private, exported)
                self.assertFalse(service.warned)
            finally:
                service.shutdown()


if __name__ == "__main__":
    unittest.main()

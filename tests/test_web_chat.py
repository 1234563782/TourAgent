"""Chat HTTP boundaries with isolated graph and database doubles."""

from contextlib import nullcontext
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command

from business_travel.auth import AuthSettings
from business_travel.web import create_app, conversation_lock, chat_snapshot
from business_travel.memory_pg import MemoryIdentity
from tests.test_auth import settings


def state(messages=(), pending=(), revision="v1"):
    return SimpleNamespace(
        values={"messages": list(messages)}, interrupts=tuple(
            SimpleNamespace(value=value) for value in pending),
        config={"configurable": {"checkpoint_id": revision}})


class ChatTests(unittest.TestCase):
    def setUp(self):
        self.auth = Mock()
        self.auth.current_user.return_value = {
            "company_id": "company", "user_id": "verified", "username": "alice"}
        self.graph = Mock()
        self.graph.stream.return_value = iter(())
        self.graph.get_state.return_value = state()
        self.runs = Mock()
        self.runs.latest.return_value = None
        self.runs.get.return_value = None
        self.runs.finish.side_effect = lambda ident, status, checkpoint_id=None: {
            "id": ident, "status": status, "stage": "accepted", "checkpoint_id": checkpoint_id}
        self.registry = Mock()
        self.registry.require.return_value = {"id": "web", "title": "原有对话"}
        self.patches = [
            patch("business_travel.web.conversation_lock", return_value=nullcontext()),
            patch("business_travel.web.open_checkpointer", return_value=nullcontext(Mock())),
            patch("business_travel.web.build_assistant", return_value=self.graph),
            patch("business_travel.web.PolicyStore.from_settings", return_value=Mock()),
            patch("business_travel.web.ChatRunStore", return_value=self.runs),
            patch("business_travel.web.ConversationStore", return_value=self.registry),
            patch("business_travel.web.execution_resources",
                  return_value=nullcontext(Mock(exhausted=False))),
            patch("business_travel.web.memory_connection", return_value=nullcontext(Mock())),
        ]
        self.mocks = [item.start() for item in self.patches]
        for item in self.patches:
            self.addCleanup(item.stop)
        self.client = TestClient(create_app(settings(), AuthSettings(), self.auth),
                                 base_url="http://127.0.0.1:8000")
        self.addCleanup(self.client.close)

    def post(self, body):
        return self.client.post("/api/chat", json={"request_id": uuid4().hex, **body},
                                headers={"Origin": "http://127.0.0.1:8000"})

    def test_anonymous(self):
        self.auth.current_user.return_value = None
        self.assertEqual(self.client.get("/api/chat").status_code, 401)
        self.assertEqual(self.post({"message": "hello"}).status_code, 401)
        self.graph.stream.assert_not_called()

    def test_invalid_actions_and_identity_override(self):
        for body in ({}, {"message": " "}, {"message": "x", "resume": False},
                     {"resume": True}, {"resume": "false", "revision": "v1"},
                     {"message": "x", "user_id": "victim"},
                     {"message": "x", "revision": "v1"}, {"message": "x" * 3001}):
            with self.subTest(body_keys=list(body)):
                self.assertEqual(self.post(body).status_code, 422)
        self.assertEqual(self.client.get("/api/chat?user_id=victim").status_code, 400)
        self.graph.stream.assert_not_called()

    def test_text_and_identity(self):
        self.graph.get_state.side_effect = [
            state(), state([HumanMessage("hello"), AIMessage("response")], revision="v2")]
        response = self.post({"message": " hello "})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["messages"][-1]["text"], "response")
        action, config = self.graph.stream.call_args.args
        self.assertEqual(action["messages"][0].content, "hello")
        self.assertEqual(config["configurable"]["thread_id"], "company:verified:web")
        self.assertEqual(self.mocks[1].call_args.args[1].user_id, "verified")

    def test_full_length_chinese_message(self):
        self.assertEqual(self.post({"message": "中" * 3000}).status_code, 200)

    def test_pending_and_stale_confirmation_blocked(self):
        self.graph.get_state.return_value = state(pending=[{"action": "save_trip"}])
        self.assertEqual(self.post({"message": "another"}).status_code, 409)
        self.assertEqual(self.post({"resume": True, "revision": "old"}).status_code, 409)
        self.graph.stream.assert_not_called()

    def test_accept_and_cancel_use_boolean_command(self):
        for choice in (True, False):
            self.graph.get_state.side_effect = [
                state(pending=[{"action": "save_trip"}]), state(revision="v2")]
            self.assertEqual(self.post({"resume": choice, "revision": "v1"}).status_code, 200)
            command = self.graph.stream.call_args.args[0]
            self.assertIsInstance(command, Command)
            self.assertIs(command.resume, choice)

    def test_duplicate_confirmation_blocked(self):
        self.assertEqual(self.post({"resume": True, "revision": "v1"}).status_code, 409)
        self.graph.stream.assert_not_called()

    def test_history_does_not_invoke(self):
        self.graph.get_state.return_value = state([HumanMessage("hi"), AIMessage("hello")])
        self.assertEqual(len(self.client.get("/api/chat").json()["messages"]), 2)
        self.graph.stream.assert_not_called()

    def test_errors_redacted(self):
        self.graph.stream.side_effect = RuntimeError("secret-token-private-url")
        response = self.post({"message": "hi"})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("secret-token", response.text)

    def test_snapshot_hides_tool_data_and_intermediate_text(self):
        result = chat_snapshot(state([
            HumanMessage("hi"), AIMessage("intermediate"),
            AIMessage("", tool_calls=[{"name": "lookup", "args": {}, "id": "1"}]),
            ToolMessage("internal", tool_call_id="1"), AIMessage("final"),
            HumanMessage("next"), AIMessage("done"),
        ], pending=[{"action": "confirm_facts", "facts": {"value": 123}}]))
        self.assertEqual([x["text"] for x in result["messages"]], ["hi", "final", "next", "done"])
        self.assertEqual(result["interrupt"][0]["facts"], {"value": 123})

    def test_task_snapshot_exposes_only_public_fields(self):
        checkpoint = state()
        checkpoint.values["tasks"] = {"t": {
            "id": "t", "intent": "plan_trip", "subject_id": "s", "revision": 2,
            "status": "needs_clarification", "missing_fields": ["check_in"],
            "internal_tool_response": "private-payload", "values": {"private": "fact"}}}
        result = chat_snapshot(checkpoint)
        self.assertEqual(result["tasks"][0]["missing_fields"], ["check_in"])
        self.assertNotIn("private", str(result))

    def test_stream_executes_graph_in_verified_thread(self):
        self.graph.stream.return_value = iter([
            ("custom", {"stage": "query_hotel", "text": "untrusted replacement", "secret": "hidden"}),
            ("updates", {"assistant": {"messages": [AIMessage("internal text")]}}),
            ("updates", {"advance": {"task_answers": ["已核验结果"]}}),
        ])
        self.graph.get_state.side_effect = [
            state(), state([HumanMessage("hi"), AIMessage("已核验结果")], revision="v2")]
        response = self.client.post("/api/chat/stream", json={"message": "hi", "request_id": uuid4().hex},
                                   headers={"Origin": "http://127.0.0.1:8000"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("正在查询酒店", response.text)
        self.assertIn("已核验结果", response.text)
        self.assertIn("event: done", response.text)
        self.assertNotIn("untrusted replacement", response.text)
        self.assertNotIn("internal text", response.text)
        self.assertNotIn("hidden", response.text)
        self.graph.invoke.assert_not_called()
        self.assertEqual(self.graph.stream.call_args.args[1]["configurable"]["thread_id"],
                         "company:verified:web")

    def test_stream_rejects_stale_confirmation_before_execution(self):
        self.graph.get_state.return_value = state(pending=[{"action": "save_trip"}])
        response = self.client.post("/api/chat/stream",
            json={"resume": True, "revision": "stale", "request_id": uuid4().hex},
            headers={"Origin": "http://127.0.0.1:8000"})
        self.assertEqual(response.status_code, 409)
        self.graph.stream.assert_not_called()


class LockTests(unittest.TestCase):
    def test_unlock_even_after_error_and_contention_rejected(self):
        connection = Mock()
        with patch("business_travel.web.memory_connection", return_value=nullcontext(connection)):
            connection.execute.return_value.fetchone.return_value = {"acquired": True}
            with self.assertRaises(RuntimeError):
                with conversation_lock(settings(), MemoryIdentity("company", "alice")):
                    raise RuntimeError("test")
            self.assertIn("pg_advisory_unlock", connection.execute.call_args.args[0])
            connection.execute.return_value.fetchone.return_value = {"acquired": False}
            from fastapi import HTTPException
            with self.assertRaises(HTTPException) as error:
                with conversation_lock(settings(), MemoryIdentity("company", "alice")):
                    self.fail("Contended execution must not run")
            self.assertEqual(error.exception.status_code, 409)

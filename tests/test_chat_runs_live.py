"""Real isolated PostgreSQL ledger, HTTP routes and checkpoint replay."""

import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import Mock, patch
from uuid import uuid4

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt
from psycopg import sql

from business_travel.auth import AuthSettings
from business_travel.chat_runs import ChatRunStore, setup_chat_runs, request_digest
from business_travel.conversations import ConversationStore
from business_travel.graph import AgentState
from business_travel.memory_pg import MemoryIdentity, memory_connection, setup_memory
from business_travel.settings import Settings
from business_travel.web import ChatBody, create_app, conversation_lock


@unittest.skipUnless(os.environ.get("TRAVEL_TEST_POSTGRES") == "1", "PostgreSQL integration is opt-in")
class ChatRunLiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = "travel_run_test_" + uuid4().hex
        cls.settings = replace(Settings.from_env(), memory_schema=cls.schema)
        setup_memory(cls.settings)

    @classmethod
    def tearDownClass(cls):
        if cls.settings.memory_schema != cls.schema or not cls.schema.startswith("travel_run_test_"):
            raise AssertionError("Not an isolated test schema")
        with memory_connection(cls.settings) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(cls.schema)))

    def setUp(self):
        self.identity = MemoryIdentity(uuid4().hex)
        self.registry = ConversationStore(self.settings, self.identity)
        self.conversation = self.registry.create()["id"]
        self.runs = ChatRunStore(self.settings, self.identity, self.conversation)
        self.auth = Mock()
        self.auth.current_user.return_value = {
            "user_id": self.identity.user_id, "username": "fixture"}
        self.calls = 0
        self.writes = 0
        self.fail = False
        self.pause = False
        self.confirm = False
        self.started = threading.Event()
        self.release = threading.Event()
        self.addCleanup(self.release.set)

        def build(*args, checkpointer, **kwargs):
            def respond(state):
                self.calls += 1
                self.started.set()
                if self.pause and not self.release.wait(8):
                    raise AssertionError("Fixture did not release")
                if self.fail:
                    raise RuntimeError("private-provider-token")
                if self.confirm:
                    accepted = interrupt({"action": "fixture"})
                    if accepted:
                        self.writes += 1
                    return {"messages": [AIMessage("accepted" if accepted else "cancelled")]}
                text = next(m.content for m in reversed(state["messages"]) if isinstance(m, HumanMessage))
                return {"messages": [AIMessage("收到：" + text)]}
            graph = StateGraph(AgentState)
            graph.add_node("respond", respond)
            graph.add_edge(START, "respond")
            graph.add_edge("respond", END)
            return graph.compile(checkpointer=checkpointer)

        for item in (patch("business_travel.web.build_assistant", side_effect=build),
                     patch("business_travel.web.PolicyStore.from_settings", return_value=Mock())):
            item.start()
            self.addCleanup(item.stop)
        self.origin = {"Origin": "http://127.0.0.1:8000"}
        self.client = TestClient(create_app(self.settings, AuthSettings(), self.auth),
                                 base_url=self.origin["Origin"])
        self.addCleanup(self.client.close)
        self.route = f"/api/conversations/{self.conversation}"

    def send(self, payload, stream=False):
        return self.client.post(self.route + "/chat" + ("/stream" if stream else ""),
                                json=payload, headers=self.origin)

    def payload(self, text="test"):
        return {"request_id": uuid4().hex, "message": text}

    def test_repeat_is_pinned_after_later_turn_and_after_store_rebuild(self):
        first = self.payload("first")
        original = self.send(first)
        self.assertEqual(original.status_code, 200, original.text)
        self.assertEqual(self.send(self.payload("second")).status_code, 200)
        replay = self.send(first)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json(), original.json())
        self.assertEqual(self.calls, 2)
        rebuilt = ChatRunStore(self.settings, self.identity, self.conversation)
        self.assertEqual(rebuilt.get(first["request_id"])["checkpoint_id"], original.json()["revision"])
        setup_chat_runs(self.settings)
        self.assertEqual(rebuilt.get(first["request_id"])["status"], "completed")
        stream = self.send(first, stream=True)
        self.assertIn("event: done", stream.text)
        self.assertNotIn("event: delta", stream.text)
        self.assertEqual(self.calls, 2)

    def test_same_key_changed_content_is_rejected(self):
        payload = self.payload()
        self.assertEqual(self.send(payload).status_code, 200)
        changed = self.send({**payload, "message": "different"})
        self.assertEqual(changed.status_code, 409)
        self.assertEqual(self.calls, 1)

    def test_running_status_and_concurrent_repeat_do_not_execute_twice(self):
        self.pause = True
        payload = self.payload()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.send, payload)
            try:
                self.assertTrue(self.started.wait(5))
                status = self.client.get(self.route + "/runs/" + payload["request_id"])
                self.assertEqual(status.status_code, 200)
                self.assertEqual(status.json()["run"]["status"], "running")
                self.assertEqual(self.send(payload).status_code, 409)
                self.assertEqual(self.send(self.payload("other")).status_code, 409)
                self.assertEqual(self.calls, 1)
            finally:
                self.release.set()
            self.assertEqual(future.result(timeout=5).status_code, 200)
        self.assertEqual(self.client.get(self.route + "/runs").json()["run"]["status"], "completed")

    def test_interrupted_owner_blocks_new_and_repeat_without_replay(self):
        payload = self.payload()
        with conversation_lock(self.settings, self.identity, self.conversation):
            self.runs.start(payload["request_id"], request_digest(ChatBody(**payload)), None)
            status = self.client.get(self.route + "/runs").json()["run"]
            self.assertEqual(status["status"], "running")
        # Closing the owner connection models the lock release after process loss.
        status = self.client.get(self.route + "/runs").json()["run"]
        self.assertEqual(status["status"], "interrupted")
        self.assertTrue(status["needs_review"])
        self.assertEqual(self.send(payload).status_code, 409)
        self.assertEqual(self.send(self.payload("new")).status_code, 409)
        self.assertEqual(self.calls, 0)

    def test_failed_run_preserves_only_prior_committed_history(self):
        first = self.send(self.payload("successful")).json()
        self.fail = True
        payload = self.payload("will fail")
        self.assertEqual(self.send(payload).status_code, 503)
        row = self.runs.get(payload["request_id"])
        self.assertEqual(row["status"], "failed")
        self.assertNotIn("private-provider-token", str(row))
        history = self.client.get(self.route + "/chat").json()
        self.assertEqual(history["messages"], first["messages"])
        self.assertTrue(history["run"]["needs_review"])
        self.assertEqual(self.send(payload).status_code, 409)
        self.assertEqual(self.calls, 2)

    def test_confirmation_request_replay_never_writes_again(self):
        self.confirm = True
        first_payload = self.payload()
        first = self.send(first_payload).json()
        self.assertEqual(first["run"]["status"], "awaiting_confirmation")
        self.assertEqual(self.writes, 0)
        confirm = {"request_id": uuid4().hex, "resume": True, "revision": first["revision"]}
        result = self.send(confirm)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.writes, 1)
        self.assertEqual(self.send(confirm).json(), result.json())
        self.assertEqual(self.writes, 1)
        self.assertEqual(self.send(first_payload).json(), first)
        self.assertEqual(self.writes, 1)
        stale = {**confirm, "request_id": uuid4().hex}
        self.assertEqual(self.send(stale).status_code, 409)

    def test_foreign_user_conversation_and_company_cannot_read_or_reuse_run(self):
        payload = self.payload()
        self.assertEqual(self.send(payload).status_code, 200)
        other_conversation = self.registry.create()["id"]
        self.assertEqual(self.client.get(
            f"/api/conversations/{other_conversation}/runs/{payload['request_id']}").status_code, 404)
        self.auth.current_user.return_value = {
            "user_id": uuid4().hex, "username": "other"}
        for path in ("/runs", "/runs/" + payload["request_id"], "/chat"):
            self.assertEqual(self.client.get(self.route + path).status_code, 404)
        self.assertEqual(self.send(payload).status_code, 404)
        other = ChatRunStore(self.settings, MemoryIdentity(uuid4().hex),
                             self.conversation)
        self.assertIsNone(other.get(payload["request_id"]))
        self.assertEqual(self.calls, 1)

    def test_request_id_is_scoped_by_conversation(self):
        payload = self.payload()
        self.assertEqual(self.send(payload).status_code, 200)
        other = self.registry.create()["id"]
        result = self.client.post(f"/api/conversations/{other}/chat", json=payload, headers=self.origin)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["conversation_id"], other)
        self.assertEqual(self.calls, 2)

    def test_legacy_write_also_uses_ledger(self):
        payload = self.payload()
        first = self.client.post("/api/chat", json=payload, headers=self.origin)
        second = self.client.post("/api/chat/stream", json=payload, headers=self.origin)
        self.assertEqual(first.status_code, 200)
        self.assertIn("event: done", second.text)
        self.assertEqual(self.calls, 1)

    def test_checkpoint_commit_without_run_commit_is_not_replayed(self):
        payload = self.payload("committed checkpoint")
        with patch.object(ChatRunStore, "finish", side_effect=RuntimeError("ledger unavailable")):
            self.assertEqual(self.send(payload).status_code, 503)
        self.assertEqual(self.runs.get(payload["request_id"])["status"], "running")
        status = self.client.get(self.route + "/runs").json()["run"]
        self.assertEqual(status["status"], "interrupted")
        self.assertEqual(self.send(payload).status_code, 409)
        self.assertEqual(self.calls, 1)
        # Completion was not confirmed across both stores; no partial history is promoted.
        self.assertEqual(self.client.get(self.route + "/chat").json()["messages"], [])

    def test_run_endpoints_require_auth_and_reject_query_overrides(self):
        payload = self.payload()
        self.assertEqual(self.send(payload).status_code, 200)
        for suffix in ("/runs", "/runs/" + payload["request_id"]):
            self.assertEqual(self.client.get(self.route + suffix + "?user_id=foreign").status_code, 400)
        self.assertEqual(self.client.get(self.route + "/runs/invalid").status_code, 404)
        self.auth.current_user.return_value = None
        for suffix in ("/runs", "/runs/" + payload["request_id"]):
            self.assertEqual(self.client.get(self.route + suffix).status_code, 401)

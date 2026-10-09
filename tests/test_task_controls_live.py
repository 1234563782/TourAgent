"""Real PostgreSQL and HTTP with the production graph and synthetic models/providers."""

import json
import os
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch
from uuid import uuid4

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage
from psycopg import sql

from business_travel.auth import AuthSettings
from business_travel.conversations import ConversationStore
from business_travel.graph import build_assistant
from business_travel.memory_pg import (
    MemoryIdentity, PostgresTravelMemory, memory_connection, open_checkpointer, setup_memory,
)
from business_travel.policy import PolicyStore
from business_travel.settings import Settings
from business_travel.web import create_app
from tests.test_task_controls import control_response, dependency_batch
from tests.test_travel_graph import FixtureProvider, ScriptedModel, review_response, select_result, slot_response


@unittest.skipUnless(os.environ.get("TRAVEL_TEST_POSTGRES") == "1", "PostgreSQL integration is opt-in")
class TaskControlLiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = "travel_task_control_test_" + uuid4().hex
        cls.settings = replace(Settings.from_env(), memory_schema=cls.schema)
        setup_memory(cls.settings)

    @classmethod
    def tearDownClass(cls):
        if cls.settings.memory_schema != cls.schema or not cls.schema.startswith("travel_task_control_test_"):
            raise AssertionError("Not an isolated task control schema")
        with memory_connection(cls.settings) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(cls.schema)))

    def setUp(self):
        self.identity = MemoryIdentity(self.settings.memory_company_id, uuid4().hex)
        self.memory = PostgresTravelMemory(self.settings, self.identity)
        self.registry = ConversationStore(self.settings, self.identity)
        self.conversation = self.registry.create()["id"]
        self.config = {"configurable": {"thread_id": self.identity.thread_id(self.conversation)}}
        self.provider = FixtureProvider()

    def models(self, slots):
        models = {"slots": ScriptedModel(slots), "reviewer": ScriptedModel([review_response()] * 12),
                  "supervisor": ScriptedModel([AIMessage(content="您好")] * 5)}
        return lambda role: ScriptedModel([select_result]) if role == "planner" else models[role]

    def graph(self, saver, factory):
        return build_assistant(factory, self.settings, self.memory, PolicyStore(None, None),
                               checkpointer=saver, provider=self.provider)

    def test_cancel_survives_reconstruction_and_identity_isolation(self):
        with open_checkpointer(self.settings, self.identity) as saver:
            graph = self.graph(saver, self.models([
                slot_response({"destination": "B"}, "plan_trip", scope="hotel")]))
            first = graph.invoke({"messages": [HumanMessage(content="B酒店")]}, self.config)
            ident = next(iter(first["tasks"]))
        with open_checkpointer(self.settings, self.identity) as saver:
            graph = self.graph(saver, self.models([control_response("cancel")]))
            result = graph.invoke({"messages": [HumanMessage(content="取消刚才查询")]}, self.config)
            self.assertEqual(result["tasks"][ident]["status"], "cancelled")
        with open_checkpointer(self.settings, self.identity) as saver:
            graph = self.graph(saver, self.models([]))
            self.assertEqual(graph.get_state(self.config).values["tasks"][ident]["status"], "cancelled")
            other_conversation = {"configurable": {
                "thread_id": self.identity.thread_id(self.registry.create()["id"])}}
            self.assertFalse(graph.get_state(other_conversation).values)
        other = MemoryIdentity(self.identity.company_id, uuid4().hex)
        with open_checkpointer(self.settings, other) as saver:
            other_graph = build_assistant(self.models([]), self.settings,
                PostgresTravelMemory(self.settings, other), PolicyStore(None, None),
                checkpointer=saver, provider=self.provider)
            with self.assertRaises(PermissionError):
                other_graph.get_state(self.config)
        self.assertEqual(self.provider.requests, [])
        self.assertEqual(self.memory.read()["trip_history"], [])

    def test_blocked_read_only_dependency_resumes_after_database_reopen(self):
        with open_checkpointer(self.settings, self.identity) as saver:
            graph = self.graph(saver, self.models([dependency_batch]))
            first = graph.invoke({"messages": [HumanMessage(
                content="A到B机票，查询成功后解释差旅制度")]}, self.config)
            parent, dependent = first["tasks"]
        with open_checkpointer(self.settings, self.identity) as saver:
            graph = self.graph(saver, self.models([
                slot_response({"departure_date": "2099-10-03"}, "plan_trip", update=True)]))
            updated = graph.invoke({"messages": [HumanMessage(content="2099-10-03出发")]}, self.config)
            self.assertEqual(updated["tasks"][parent]["status"], "completed")
            self.assertEqual(updated["tasks"][dependent]["status"], "blocked")
        with open_checkpointer(self.settings, self.identity) as saver:
            graph = self.graph(saver, self.models([control_response("resume")]))
            result = graph.invoke({"messages": [HumanMessage(content="继续政策说明")]}, self.config)
            self.assertEqual(result["tasks"][dependent]["attempt"], 1)
            self.assertNotEqual(result["tasks"][dependent]["status"], "blocked")
            self.assertEqual(result["tasks"][dependent]["dependencies"], [parent])
        self.assertEqual(len(self.provider.requests), 1)

    def test_sse_cancel_has_delta_terminal_snapshot_and_idempotent_replay(self):
        factory = self.models([
            slot_response({"destination": "B"}, "plan_trip", scope="hotel"), control_response("cancel")])
        auth = Mock()
        auth.current_user.return_value = {"company_id": self.identity.company_id,
                                         "user_id": self.identity.user_id, "username": "fixture"}
        origin = "http://127.0.0.1:8000"
        route = f"/api/conversations/{self.conversation}/chat"
        def build(*args, **kwargs):
            return self.graph(kwargs["checkpointer"], factory)
        with patch("business_travel.web.build_assistant", side_effect=build), patch(
                "business_travel.web.PolicyStore.from_settings", return_value=PolicyStore(None, None)), TestClient(
                    create_app(self.settings, AuthSettings(), auth), base_url=origin) as client:
            first = client.post(route, headers={"Origin": origin},
                                json={"request_id": uuid4().hex, "message": "B酒店"})
            self.assertEqual(first.status_code, 200)
            ident = first.json()["tasks"][0]["id"]
            body = {"request_id": uuid4().hex, "message": "取消查询任务"}
            response = client.post(route + "/stream", headers={"Origin": origin}, json=body)
            self.assertEqual(response.status_code, 200)
            events = {}
            for block in response.text.strip().split("\n\n"):
                name, data = block.split("\n", 1)
                events.setdefault(name.removeprefix("event: "), []).append(
                    json.loads(data.removeprefix("data: ")))
            self.assertIn("已取消", events["delta"][0]["text"])
            done = events["done"][0]
            self.assertEqual(done["tasks"][0]["id"], ident)
            self.assertEqual(done["tasks"][0]["status"], "cancelled")
            self.assertEqual(done["run"]["status"], "completed")
            replay = client.post(route, headers={"Origin": origin}, json=body)
            self.assertEqual(replay.status_code, 200)
            self.assertEqual(replay.json(), done)
            history = client.get(route)
            self.assertEqual(history.status_code, 200)
            self.assertEqual(history.json()["tasks"][0]["status"], "cancelled")
            self.assertEqual(len(history.json()["messages"]), 4)
        self.assertEqual(self.provider.requests, [])

"""Isolated PostgreSQL conversation and checkpoint tests, no external model calls."""

import os
import unittest
from dataclasses import replace
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt, Command
from psycopg import sql

from business_travel.conversations import ConversationStore, setup_conversations
from business_travel.graph import AgentState
from business_travel.memory_pg import (MemoryIdentity, memory_connection, setup_memory,
                                       open_checkpointer, PostgresTravelMemory)
from business_travel.settings import Settings
from business_travel.web import conversation_lock
from fastapi import HTTPException


def fixture_graph(checkpointer):
    def respond(state):
        text = next(m.content for m in reversed(state["messages"]) if isinstance(m, HumanMessage))
        if text == "confirm-test":
            accepted = interrupt({"action": "fixture", "text": text})
            return {"messages": [AIMessage("accepted" if accepted else "cancelled")]}
        return {"messages": [AIMessage("收到：" + text)], "slots": {"test": text},
                "draft": {"test": text}}
    builder = StateGraph(AgentState)
    builder.add_node("respond", respond)
    builder.add_edge(START, "respond")
    builder.add_edge("respond", END)
    return builder.compile(checkpointer=checkpointer)


@unittest.skipUnless(os.environ.get("TRAVEL_TEST_POSTGRES") == "1", "PostgreSQL integration is opt-in")
class ConversationLiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = "travel_conversation_test_" + uuid4().hex
        cls.settings = replace(Settings.from_env(), memory_schema=cls.schema)
        setup_memory(cls.settings)

    @classmethod
    def tearDownClass(cls):
        if cls.settings.memory_schema != cls.schema or not cls.schema.startswith("travel_conversation_test_"):
            raise AssertionError("Not an isolated test schema")
        with memory_connection(cls.settings) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(cls.schema)))

    def setUp(self):
        self.identity = MemoryIdentity(uuid4().hex)
        self.store = ConversationStore(self.settings, self.identity)

    def test_create_list_restore_title_pagination_and_legacy(self):
        self.assertEqual(self.store.list()["items"][0]["id"], "web")
        first, second = self.store.create(), self.store.create()
        self.assertNotEqual(first["id"], second["id"])
        self.store.touch(first["id"], "酒店\n上海")
        self.store.touch(first["id"], "second question")
        self.store.touch(first["id"])
        rebuilt = ConversationStore(self.settings, self.identity)
        self.assertEqual(rebuilt.require(first["id"])["title"], "酒店 上海")
        self.assertEqual(rebuilt.list(limit=1)["next_offset"], 1)
        self.assertEqual(len(rebuilt.list(offset=1)["items"]), 2)
        setup_conversations(self.settings)
        self.assertEqual(len(rebuilt.list()["items"]), 3)

    def test_foreign_ids_and_invalid_ids_are_denied(self):
        ident = self.store.create()["id"]
        other = ConversationStore(self.settings, MemoryIdentity(uuid4().hex))
        with self.assertRaises(LookupError):
            other.require(ident)
        other.touch(ident, "changed")
        self.assertEqual(self.store.require(ident)["title"], "新对话")
        for bad in ("default", "x:y:web", "../web", uuid4().hex):
            with self.assertRaises(LookupError):
                self.store.require(bad)
        self.assertEqual([r["id"] for r in other.list()["items"]], ["web"])

    def test_real_checkpoints_and_pending_confirmation_are_separate(self):
        shared = {"id": "shared-draft", "note": "kept"}
        PostgresTravelMemory(self.settings, self.identity).save_trip(shared)
        first, second = self.store.create()["id"], self.store.create()["id"]
        a = {"configurable": {"thread_id": self.identity.thread_id(first)}}
        b = {"configurable": {"thread_id": self.identity.thread_id(second)}}
        legacy = {"configurable": {"thread_id": self.identity.thread_id("web")}}
        with open_checkpointer(self.settings, self.identity) as saver:
            graph = fixture_graph(saver)
            graph.invoke({"messages": [HumanMessage("old-web")]}, legacy)
            graph.invoke({"messages": [HumanMessage("A")]}, a)
            graph.invoke({"messages": [HumanMessage("confirm-test")]}, a)
            self.assertTrue(graph.get_state(a).interrupts)
            self.assertFalse(graph.get_state(b).values)
            graph.invoke({"messages": [HumanMessage("B")]}, b)
        self.store.ensure_legacy()
        with open_checkpointer(self.settings, self.identity) as saver:
            graph = fixture_graph(saver)
            self.assertEqual(graph.get_state(a).values["slots"], {"test": "A"})
            self.assertEqual(graph.get_state(b).values["draft"], {"test": "B"})
            self.assertFalse(graph.get_state(b).interrupts)
            self.assertEqual(graph.get_state(legacy).values["slots"], {"test": "old-web"})
            graph.invoke(Command(resume=False), a)
            self.assertEqual(graph.get_state(a).values["messages"][-1].content, "cancelled")
            self.assertEqual(graph.get_state(b).values["messages"][-1].content, "收到：B")
        # Conversation creation does not fork saved trips into another user.
        saved = PostgresTravelMemory(self.settings, self.identity).read()
        self.assertEqual(saved["trip_history"], [shared])
        self.assertNotIn("preferences", saved)
        other = MemoryIdentity(uuid4().hex)
        self.assertEqual(PostgresTravelMemory(self.settings, other).read()["trip_history"], [])

    def test_locks_are_per_conversation_and_release_after_contention(self):
        first, second = self.store.create()["id"], self.store.create()["id"]
        with conversation_lock(self.settings, self.identity, first):
            with conversation_lock(self.settings, self.identity, second):
                with self.assertRaises(HTTPException) as error:
                    with conversation_lock(self.settings, self.identity, first):
                        self.fail("Same conversation must remain locked")
                self.assertEqual(error.exception.status_code, 409)
        with conversation_lock(self.settings, self.identity, first):
            pass

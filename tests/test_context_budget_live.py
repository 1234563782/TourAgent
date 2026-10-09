"""Isolated PostgreSQL checkpoints with real graphs and synthetic model responses."""

import json
import os
import unittest
from dataclasses import replace
from unittest.mock import Mock
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from psycopg import sql

from business_travel.conversations import ConversationStore
from business_travel.graph import build_assistant
from business_travel.memory_pg import (
    MemoryIdentity, PostgresTravelMemory, memory_connection, open_checkpointer, setup_memory,
)
from business_travel.policy import PolicyStore
from business_travel.settings import Settings
from tests.test_travel_graph import ScriptedModel, call, review_response, slot_response


@unittest.skipUnless(os.environ.get("TRAVEL_TEST_POSTGRES") == "1", "PostgreSQL integration is opt-in")
class ContextPersistenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = "travel_context_test_" + uuid4().hex
        cls.settings = replace(Settings.from_env(), memory_schema=cls.schema,
                               context_recent_tokens=512, langfuse_enabled=False)
        setup_memory(cls.settings)

    @classmethod
    def tearDownClass(cls):
        if cls.settings.memory_schema != cls.schema or not cls.schema.startswith("travel_context_test_"):
            raise AssertionError("Not an isolated context test schema")
        with memory_connection(cls.settings) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(cls.schema)))

    def setUp(self):
        self.identity = MemoryIdentity(self.settings.memory_company_id, uuid4().hex)
        self.memory = PostgresTravelMemory(self.settings, self.identity)
        self.registry = ConversationStore(self.settings, self.identity)
        self.config = self.new_config()

    def new_config(self):
        return {"configurable": {"thread_id": self.identity.thread_id(self.registry.create()["id"])}}

    def graph(self, saver, replies):
        models = {"slots": ScriptedModel([slot_response()] * 4),
                  "reviewer": ScriptedModel([review_response()] * 4),
                  "supervisor": ScriptedModel(replies)}
        return build_assistant(lambda role: models[role], self.settings, self.memory,
                               PolicyStore(None, None), checkpointer=saver, provider=Mock())

    def test_summary_and_exact_original_survive_reopen_without_replaying(self):
        original = [HumanMessage("第一条原话：没有同意保存，也不知道是否已批准。", id="source")]
        for index in range(50):
            original.extend([AIMessage("旧回答"), HumanMessage(f"历史{index}：" + "完整背景。" * 80)])
        original.extend([AIMessage("旧回答"), HumanMessage("你好")])
        with open_checkpointer(self.settings, self.identity) as saver:
            graph = self.graph(saver, [AIMessage("您好")])
            first = graph.invoke({"messages": original}, self.config)
            self.assertIsNotNone(first["context_summary"])
            self.assertEqual(first["messages"][:len(original)], original)
        with open_checkpointer(self.settings, self.identity) as saver:
            graph = self.graph(saver, [
                call("read_conversation_history", {"message_index": 0}, "history"),
                AIMessage("只读取原话，不执行历史操作")])
            restored = graph.get_state(self.config).values
            self.assertEqual(restored["messages"], first["messages"])
            self.assertEqual(restored["context_summary"], first["context_summary"])
            result = graph.invoke({"messages": [HumanMessage("查看最早的原话")]}, self.config)
            response = next(message for message in reversed(result["messages"])
                            if isinstance(message, ToolMessage) and message.name == "read_conversation_history")
            self.assertEqual(json.loads(json.loads(response.content)["text"])["content"], original[0].content)
            self.assertEqual(result["messages"][:len(first["messages"])], first["messages"])
            self.assertEqual(self.memory.read()["trip_history"], [])

    def test_history_read_cannot_cross_conversation_or_identity(self):
        second = self.new_config()
        with open_checkpointer(self.settings, self.identity) as saver:
            graph = self.graph(saver, [AIMessage("记录"),
                call("read_conversation_history", {"message_index": 0}, "history"), AIMessage("只读")])
            graph.invoke({"messages": [HumanMessage("会话A专属内容")]}, self.config)
            result = graph.invoke({"messages": [HumanMessage("会话B读取本会话第一条")]}, second)
            response = next(message for message in result["messages"] if isinstance(message, ToolMessage))
            self.assertNotIn("会话A专属内容", response.content)
            self.assertIn("会话B读取本会话第一条", response.content)
        other = MemoryIdentity(self.identity.company_id, uuid4().hex)
        with open_checkpointer(self.settings, other) as saver:
            self.memory = PostgresTravelMemory(self.settings, other)
            graph = self.graph(saver, [])
            with self.assertRaises(PermissionError):
                graph.get_state(self.config)

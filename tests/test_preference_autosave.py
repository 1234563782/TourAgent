"""Long-term preference capture is no longer part of the assistant."""

import unittest
from pathlib import Path


class PreferenceRemovalTests(unittest.TestCase):
    def test_assistant_source_has_no_preference_tools(self):
        source = Path(__file__).resolve().parents[1].joinpath(
            "business_travel", "graph.py").read_text(encoding="utf-8")
        self.assertNotIn("def save_preferences", source)
        self.assertNotIn("def edit_preferences", source)
        self.assertNotIn("capture_preferences", source)
        root = Path(__file__).resolve().parents[1] / "business_travel"
        for name in ("memory.py", "memory_pg.py", "memory_migration.py", "contracts.py"):
            module = (root / name).read_text(encoding="utf-8")
            self.assertNotIn("def save_preferences", module)
            self.assertNotIn("def mutate_preferences", module)
            self.assertNotIn("class TravelPreference", module)
            self.assertNotIn("CREATE TABLE IF NOT EXISTS user_preferences", module)
        self.assertFalse((root / "preferences.py").exists())


    def test_storage_contract_has_no_preference_api(self):
        from business_travel.memory import MemoryStore, TravelMemory
        from business_travel.memory_pg import PostgresTravelMemory
        for store in (MemoryStore, TravelMemory, PostgresTravelMemory):
            for name in ("save_preferences", "mutate_preferences", "preference_snapshot"):
                self.assertFalse(hasattr(store, name), (store.__name__, name))

    def test_preference_request_cannot_write_and_saved_trips_remain_readable(self):
        from unittest.mock import patch
        from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
        from tests.test_travel_graph import GraphTests, call
        import json

        fixture = GraphTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.memory.save_trip({"id": "kept-draft"})
        with patch("business_travel.memory_pg.setup_memory") as setup:
            graph = fixture.build([
                call("query_memory", {}, "read-trips"),
                AIMessage(content="不支持保存长期偏好，已有行程草案仍可读取。"),
            ])
            result = graph.invoke({"messages": [HumanMessage("请记住我喜欢靠窗，并读取已保存的行程")]},
                                  fixture.config)
        setup.assert_not_called()
        reads = [json.loads(item.content) for item in result["messages"]
                 if isinstance(item, ToolMessage) and item.name == "query_memory"]
        self.assertEqual(reads, [{"trip_history": [{"id": "kept-draft"}]}])
        self.assertEqual(fixture.memory.read(), {"trip_history": [{"id": "kept-draft"}]})
        self.assertNotIn("preference_pending", result)
        self.assertIsNone(result.get("slot_error"))

    def test_explicit_setup_drops_only_retired_table(self):
        from unittest.mock import MagicMock, patch
        from business_travel.memory_pg import setup_memory
        connection = MagicMock()
        connection.__enter__.return_value = connection
        settings = MagicMock(memory_schema="isolated_test_schema")
        with patch("business_travel.memory_pg.memory_connection", return_value=connection), \
             patch("business_travel.memory_pg.PostgresSaver"), \
             patch("business_travel.conversations.setup_conversations"), \
             patch("business_travel.chat_runs.setup_chat_runs"), \
             patch("business_travel.resources.setup_resources"):
            setup_memory(settings)
        statements = [call.args[0] for call in connection.execute.call_args_list
                      if isinstance(call.args[0], str)]
        self.assertEqual([sql for sql in statements if "DROP" in sql],
                         ["DROP TABLE IF EXISTS user_preferences"])
        self.assertFalse(any("CREATE TABLE" in sql and "user_preferences" in sql for sql in statements))
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS trips" in sql for sql in statements))

    def test_legacy_preference_blobs_are_ignored_without_changing_trip_sources(self):
        import json
        import sqlite3
        from contextlib import closing
        import tempfile
        from business_travel.memory import TravelMemory
        from business_travel.memory_migration import read_snapshot, inventory
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "legacy"
            legacy.mkdir()
            (legacy / "one.json").write_text(json.dumps({
                "preferences": "old unsupported data", "trip_history": [{"id": "legacy-trip"}],
            }), encoding="utf-8")
            memory = TravelMemory(root, legacy, "one")
            memory.save_trip({"id": "sqlite-trip"})
            with closing(sqlite3.connect(memory.path)) as connection, connection:
                connection.execute("CREATE TABLE users (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
                connection.execute("INSERT INTO users VALUES (?, ?)", ("one", "malformed JSON"))
            before = memory.path.read_bytes()
            snapshot = read_snapshot(root, legacy, "one")
            self.assertEqual(snapshot.trips, [{"id": "legacy-trip"}, {"id": "sqlite-trip"}])
            self.assertEqual(snapshot.summary(), {"trips": 2, "checkpoints": 0, "pending_writes": 0})
            self.assertNotIn("preferences", memory.read())
            self.assertNotIn("preferences", inventory(root, legacy))
            self.assertEqual(memory.path.read_bytes(), before)


    def test_old_checkpoint_keeps_messages_and_trips_without_retired_channel(self):
        from langchain_core.messages import AIMessage, HumanMessage
        from langgraph.checkpoint.memory import InMemorySaver
        from langgraph.graph import StateGraph, START, END
        from business_travel.graph import AgentState
        from tests.test_travel_graph import GraphTests

        class OldState(AgentState, total=False):
            preference_pending: dict | None

        saver = InMemorySaver()
        old = StateGraph(OldState)
        old.add_node("finish", lambda state: {})
        old.add_edge(START, "finish")
        old.add_edge("finish", END)
        fixture = GraphTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        slots = {"subjects": {"trip-1": {"id": "trip-1", "revision": 1}}}
        old.compile(checkpointer=saver).invoke({
            "messages": [HumanMessage("旧会话"), AIMessage("旧答复")],
            "slots": slots, "tasks": {}, "preference_pending": {"mutation": "retired"},
        }, fixture.config)
        graph = fixture.build([], checkpointer=saver)
        reopened = graph.get_state(fixture.config)
        self.assertEqual([item.content for item in reopened.values["messages"]], ["旧会话", "旧答复"])
        self.assertEqual(reopened.values["slots"], slots)
        self.assertNotIn("preference_pending", reopened.values)
        self.assertFalse(reopened.next)


if __name__ == "__main__":
    unittest.main()

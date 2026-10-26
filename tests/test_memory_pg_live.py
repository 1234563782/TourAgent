"""Opt-in PostgreSQL integration tests: generated schema and synthetic records only."""

import hashlib
import os
import tempfile
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from psycopg import sql

from business_travel.graph import build_assistant
from business_travel.memory import TravelMemory
from business_travel.memory_migration import import_snapshot, read_snapshot
from business_travel.memory_pg import (
    MemoryIdentity, PostgresTravelMemory, ScopedPostgresSaver, memory_connection, open_checkpointer, setup_memory,
)
from business_travel.policy import PolicyStore
from business_travel.settings import Settings
from tests.test_travel_graph import ScriptedModel, call, slot_response, review_response
from business_travel.fact_context import fact_context
from business_travel.execution_events import public_graph_events


@unittest.skipUnless(os.environ.get("TRAVEL_TEST_POSTGRES") == "1", "PostgreSQL integration is opt-in")
class PostgresIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        original = Settings.from_env()
        if original.missing_memory_config():
            raise RuntimeError("PostgreSQL memory configuration is missing")
        cls.schema = "travel_memory_test_" + uuid.uuid4().hex
        cls.settings = replace(original, memory_schema=cls.schema)
        setup_memory(cls.settings)

    @classmethod
    def tearDownClass(cls):
        # Only this run's randomly generated test schema may be removed.
        if cls.settings.memory_schema != cls.schema or not cls.schema.startswith("travel_memory_test_"):
            raise AssertionError("Refusing to remove a non-test schema")
        with memory_connection(cls.settings) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(cls.schema)))

    def setUp(self):
        self.identity = MemoryIdentity(self.settings.memory_company_id, "test_" + uuid.uuid4().hex)
        self.memory = PostgresTravelMemory(self.settings, self.identity)
        self.config = {"configurable": {"thread_id": self.identity.thread_id("same")}}

    def graph(self, saver, responses, memory=None, slot_responses=None, reviews=None):
        model = ScriptedModel(responses)
        slots = ScriptedModel(slot_responses if slot_responses is not None else [slot_response()] * 20)
        reviewer = ScriptedModel(reviews if reviews is not None else [review_response()] * 20)
        models = {"slots": slots, "reviewer": reviewer, "supervisor": model}
        return build_assistant(lambda role: models[role], self.settings, memory or self.memory,
                               PolicyStore(None, None), checkpointer=saver)

    def test_pending_tasks_survive_postgres_reopen_and_are_thread_scoped(self):
        with open_checkpointer(self.settings, self.identity) as saver:
            graph = self.graph(saver, [], slot_responses=[
                slot_response({"destination": "杭州"}, "plan_trip", scope="hotel")])
            result = graph.invoke({"messages": [HumanMessage(content="查杭州酒店")]}, self.config)
            tasks = result["tasks"]
            self.assertEqual(next(iter(tasks.values()))["status"], "needs_clarification")
        with open_checkpointer(self.settings, self.identity) as saver:
            graph = self.graph(saver, [AIMessage(content="您好")])
            self.assertEqual(graph.get_state(self.config).values["tasks"], tasks)
            other = {"configurable": {"thread_id": self.identity.thread_id("other")}}
            self.assertFalse(graph.get_state(other).values)
            continued = graph.invoke({"messages": [HumanMessage(content="你好")]}, self.config)
            ident = next(iter(tasks))
            self.assertEqual(continued["tasks"][ident], tasks[ident])

    def test_failed_turn_recovery_survives_postgres_reopen(self):
        import json
        def outage(_):
            raise ConnectionError("synthetic")
        with open_checkpointer(self.settings, self.identity) as saver:
            graph = self.graph(saver, [], slot_responses=[
                slot_response({"destination": "天津"}, "plan_trip", scope="hotel"), outage])
            original = graph.invoke({"messages": [HumanMessage("查天津酒店")]}, self.config)
            failed = graph.invoke({"messages": [HumanMessage("目的地改为苏州")]}, self.config)
            self.assertEqual(failed["slots"], original["slots"])
            self.assertEqual(failed["pending_turns"][0]["user_message"], 1)
        def recover(messages):
            payload = json.loads(messages[-1].content)
            subject = payload["current_slots"]["trip"]
            return call("SlotSubmission", {"tasks": [{
                "operation": "update_facts", "target_id": subject["id"],
                "base_revision": subject["revision"], "trip": {"destination": "苏州"},
                "evidence": {"destination": {"user_message": 1, "quote": "苏州"}}}],
                "recovery": {"action": "retry", "user_messages": [1],
                             "evidence": {"user_message": 2, "quote": "重试目的地更正"}}}, "recover")
        with open_checkpointer(self.settings, self.identity) as saver:
            graph = self.graph(saver, [], slot_responses=[recover])
            self.assertEqual(graph.get_state(self.config).values["pending_turns"], failed["pending_turns"])
            result = graph.invoke({"messages": [HumanMessage("重试目的地更正")]}, self.config)
            self.assertIsNone(result["slot_error"])
            self.assertEqual(result["slots"]["trip"]["values"]["destination"], "苏州")
            self.assertEqual(result["slots"]["trip"]["evidence"]["destination"]["user_message"], 1)
            self.assertEqual(result["pending_turns"], [])
            self.assertEqual(len(result["tasks"]), 1)
            self.assertFalse(graph.get_state({"configurable": {
                "thread_id": self.identity.thread_id("other")}}).values)

    def test_stream_trip_confirmation_survives_reopen_and_cancel_does_not_write(self):
        subject, draft = self.trip_fixture()
        with open_checkpointer(self.settings, self.identity) as saver:
            graph = self.graph(saver, [call("save_trip", {}, "save")])
            events = list(public_graph_events(graph.stream(
                {"messages": [HumanMessage(content="Save trip")], "draft": draft,
                 "slots": {"trip": subject, "subjects": {subject["id"]: subject}}}, self.config,
                stream_mode=["custom", "updates"])))
            self.assertTrue(graph.get_state(self.config).interrupts)
            self.assertFalse(any(kind == "delta" for kind, _ in events))
            self.assertNotIn("preferences", self.memory.read())
        with open_checkpointer(self.settings, self.identity) as saver:
            graph = self.graph(saver, [AIMessage(content="Cancelled")])
            events = list(public_graph_events(graph.stream(Command(resume=False), self.config,
                                                          stream_mode=["custom", "updates"])))
            current = graph.get_state(self.config)
            self.assertFalse(current.interrupts)
            self.assertIn("已取消", current.values["messages"][-1].content)
            self.assertEqual("".join(data["text"] for kind, data in events if kind == "delta"),
                             current.values["messages"][-1].content)
            other = {"configurable": {"thread_id": self.identity.thread_id("different")}}
            self.assertFalse(graph.get_state(other).values)
        self.assertNotIn("preferences", self.memory.read())
        self.assertEqual(self.memory.read()["trip_history"], [])

    def test_saved_trips_survive_reconstruction_and_are_identity_scoped(self):
        draft = {"id": "private-trip"}
        self.memory.save_trip(draft)
        self.assertEqual(PostgresTravelMemory(self.settings, self.identity).read(),
                         {"trip_history": [draft]})
        other = MemoryIdentity(self.identity.company_id, "different")
        self.assertEqual(PostgresTravelMemory(self.settings, other).read(), {"trip_history": []})
        other_settings = replace(self.settings, memory_company_id="other_company")
        other_memory = PostgresTravelMemory(
            other_settings, MemoryIdentity("other_company", self.identity.user_id))
        self.assertEqual(other_memory.read(), {"trip_history": []})
        other_memory.save_trip({"id": "other-company-trip"})
        self.assertEqual(self.memory.read(), {"trip_history": [draft]})

    def test_trips_are_idempotent_ordered_bounded_and_conflicts_preserve_original(self):
        for idx in range(12):
            self.memory.save_trip({"id": f"trip-{idx}", "value": idx})
        self.memory.save_trip({"id": "trip-11", "value": 11})
        with self.assertRaises(ValueError):
            self.memory.save_trip({"id": "trip-11", "value": 999})
        history = self.memory.read()["trip_history"]
        self.assertEqual(len(history), 10)
        self.assertEqual(history[0], {"id": "trip-11", "value": 11})
        self.assertEqual(history[-1]["id"], "trip-2")

    def trip_fixture(self):
        subject = {"id": "synthetic-subject", "revision": 1, "values": {}, "provenance": {}}
        draft = {"id": "synthetic-draft", "request": {}, "status": "draft",
                 "subject_id": subject["id"], "revision": subject["revision"],
                 "fact_context": fact_context(subject)}
        return subject, draft

    def test_trip_save_confirmation_and_restart(self):
        subject, draft = self.trip_fixture()
        with open_checkpointer(self.settings, self.identity) as saver:
            graph = self.graph(saver, [call("save_trip", {}, "save")])
            result = graph.invoke({"messages": [HumanMessage(content="Save")], "draft": draft,
                                   "slots": {"trip": subject, "subjects": {subject["id"]: subject}}}, self.config)
            self.assertTrue(result["__interrupt__"])
        self.assertEqual(self.memory.read()["trip_history"], [])
        with open_checkpointer(self.settings, self.identity) as saver:
            graph = self.graph(saver, [AIMessage(content="Saved, not booked")])
            graph.invoke(Command(resume=True), self.config)
        self.assertEqual(self.memory.read()["trip_history"], [draft])

    def test_same_session_other_user_company_and_clear_are_separate(self):
        with open_checkpointer(self.settings, self.identity) as saver:
            graph = self.graph(saver, [AIMessage(content="Private answer")])
            graph.invoke({"messages": [HumanMessage(content="Private question")]}, self.config)
            clear = {"configurable": {"thread_id": self.identity.thread_id("new_session")}}
            self.assertEqual(graph.get_state(clear).values, {})
        for company, user in ((self.identity.company_id, "other"), ("other_company", self.identity.user_id)):
            identity = MemoryIdentity(company, user)
            settings = replace(self.settings, memory_company_id=company)
            with open_checkpointer(settings, identity) as saver:
                with self.assertRaises(PermissionError):
                    saver.get_tuple(self.config)
                self.assertIsNone(saver.get_tuple({"configurable": {"thread_id": identity.thread_id("same")}}))

    def source(self, root, *, interrupt=False):
        memory = TravelMemory(root, root / "legacy", self.identity.user_id)
        memory.save_trip({"id": "old-trip"})
        if interrupt:
            config = {"configurable": {"thread_id": f"{self.identity.user_id}:same"}}
            with SqliteSaver.from_conn_string(str(root / "checkpoints.sqlite")) as saver:
                subject, draft = self.trip_fixture()
                graph = self.graph(saver, [call("save_trip", {}, "save")], memory=memory)
                self.assertTrue(graph.invoke({"messages": [HumanMessage(content="Save trip")], "draft": draft,
                    "slots": {"trip": subject, "subjects": {subject["id"]: subject}}}, config)["__interrupt__"])
        return read_snapshot(root, root / "legacy", self.identity.user_id)

    def test_migration_is_repeatable_without_overwriting_new_trips(self):
        with tempfile.TemporaryDirectory() as temp:
            snapshot = self.source(Path(temp))
            self.assertNotIn("preferences", snapshot.summary())
            self.assertEqual(import_snapshot(self.settings, snapshot)["status"], "imported")
            self.memory.save_trip({"id": "later-trip", "value": 2})
            self.assertEqual(import_snapshot(self.settings, snapshot)["status"], "already_imported")
        history = self.memory.read()["trip_history"]
        self.assertIn({"id": "old-trip"}, history)
        self.assertIn({"id": "later-trip", "value": 2}, history)
        self.assertNotIn("preferences", self.memory.read())

    def test_trip_conflict_rolls_back_and_preserves_target(self):
        self.memory.save_trip({"id": "old-trip", "value": "existing"})
        with tempfile.TemporaryDirectory() as temp:
            snapshot = self.source(Path(temp))
            with self.assertRaises(ValueError):
                import_snapshot(self.settings, snapshot)
        self.assertNotIn("preferences", self.memory.read())
        self.assertEqual(self.memory.read()["trip_history"], [{"id": "old-trip", "value": "existing"}])

    def test_checkpoint_conflict_rolls_back_entire_import(self):
        with open_checkpointer(self.settings, self.identity) as saver:
            self.graph(saver, [AIMessage(content="Existing")]).invoke(
                {"messages": [HumanMessage(content="Existing")]}, self.config)
        with tempfile.TemporaryDirectory() as temp:
            snapshot = self.source(Path(temp), interrupt=True)
            with self.assertRaises(ValueError):
                import_snapshot(self.settings, snapshot)
        self.assertEqual(self.memory.read(), {"trip_history": []})

    def test_verification_failure_rolls_back_all_rows_and_import_marker(self):
        with tempfile.TemporaryDirectory() as temp:
            snapshot = self.source(Path(temp), interrupt=True)
            with patch.object(ScopedPostgresSaver, "get_tuple", return_value=None), self.assertRaises(ValueError):
                import_snapshot(self.settings, snapshot)
            self.assertEqual(self.memory.read(), {"trip_history": []})
            with open_checkpointer(self.settings, self.identity) as saver:
                self.assertIsNone(saver.get_tuple(self.config))
            self.assertEqual(import_snapshot(self.settings, snapshot)["status"], "imported")

    def test_setup_is_repeatable_and_preserves_trips(self):
        self.memory.save_trip({"id": "kept", "value": 1})
        setup_memory(self.settings)
        self.assertEqual(self.memory.read(), {"trip_history": [{"id": "kept", "value": 1}]})
        with memory_connection(self.settings) as connection:
            self.assertIsNone(connection.execute(
                "SELECT to_regclass('user_preferences') AS name").fetchone()["name"])

    def test_migrated_interrupt_can_be_approved_after_restart(self):
        self.migrate_interrupt(True)

    def test_migrated_interrupt_can_be_rejected_after_restart(self):
        self.migrate_interrupt(False)

    def migrate_interrupt(self, approved):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            snapshot = self.source(root, interrupt=True)
            path = root / "checkpoints.sqlite"
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            result = import_snapshot(self.settings, snapshot)
            self.assertGreater(result["checkpoints"], 0)
            self.assertGreater(result["pending_writes"], 0)
            self.assertEqual(before, hashlib.sha256(path.read_bytes()).hexdigest())
            with open_checkpointer(self.settings, self.identity) as saver:
                graph = self.graph(saver, [AIMessage(content="Done")])
                self.assertTrue(graph.get_state(self.config).next)
                graph.invoke(Command(resume=approved), self.config)
                self.assertFalse(graph.get_state(self.config).next)
            self.assertEqual(import_snapshot(self.settings, snapshot)["status"], "already_imported")
        self.assertNotIn("preferences", self.memory.read())
        expected = [self.trip_fixture()[1], {"id": "old-trip"}] if approved else [{"id": "old-trip"}]
        self.assertEqual(self.memory.read()["trip_history"], expected)


if __name__ == "__main__":
    unittest.main()

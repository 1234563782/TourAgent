"""Preference contracts and real graph execution using synthetic model outputs."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from business_travel.auth import AuthSettings
from business_travel.contracts import TravelPreference
from business_travel.graph import build_assistant
from business_travel.memory import TravelMemory
from business_travel.memory_pg import MemoryIdentity, PostgresTravelMemory
from business_travel.policy import PolicyStore
from business_travel.preferences import (
    PreferenceConflict, PreferenceContext, PreferenceMutation, merge_changes, mutate,
    preference_values, review_preference, snapshot,
)
from business_travel.settings import Settings
from business_travel.web import create_app
import test_travel_graph as fixtures


def addition(kind="seat", value="window"):
    return {"operation": "add", "preference": {"type": kind, "value": value}}


class PreferenceStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.memory = TravelMemory(self.root, self.root / "legacy", "alice")

    def change(self, changes, revision=None):
        return self.memory.mutate_preferences(PreferenceMutation(
            revision=revision or self.memory.preference_snapshot()["revision"], changes=changes),
            {"kind": "user_interface"})

    def test_add_update_delete_preserve_unrelated_items_and_provenance(self):
        first = self.change([addition(), addition("hotel", "quiet")])
        seat, hotel = first["items"]
        second = self.change([{"operation": "update", "id": seat["id"],
                               "preference": {"type": "seat", "value": "aisle"}}])
        self.assertEqual(second["items"][1], hotel)
        self.assertEqual(second["items"][0]["scope"], "long_term")
        self.assertIsNotNone(second["items"][0]["updated_at"])
        self.assertEqual(second["items"][0]["source"], {"kind": "user_interface"})
        self.change([{"operation": "delete", "id": seat["id"]}])
        self.assertEqual(self.memory.read()["preferences"], [{"type": "hotel", "value": "quiet"}])
        self.assertEqual(TravelMemory(self.root, self.root / "legacy", "alice").preference_snapshot()["items"], [hotel])

    def test_legacy_values_and_object_import_remain_readable(self):
        raw = {"seat": "window"}
        current = snapshot(raw)
        updated = mutate(raw, PreferenceMutation(revision=current["revision"], changes=[addition("hotel", "quiet")]),
                         {"kind": "user_interface"})
        self.assertEqual(preference_values(updated), [
            {"type": "seat", "value": "window"}, {"type": "hotel", "value": "quiet"}])
        self.assertEqual(updated["items"][0]["source"], {"kind": "legacy_unknown"})

    def test_stale_revision_and_delete_replay_cannot_overwrite_new_data(self):
        old = self.memory.preference_snapshot()["revision"]
        first = self.change([addition()])
        with self.assertRaises(PreferenceConflict):
            self.change([addition("hotel", "quiet")], old)
        deletion = [{"operation": "delete", "id": first["items"][0]["id"]}]
        self.change(deletion, first["revision"])
        with self.assertRaises(PreferenceConflict):
            self.change(deletion, first["revision"])
        self.assertEqual(self.memory.read()["preferences"], [])

    def test_invalid_batch_is_atomic(self):
        for changes in ([addition(), addition()], [addition(), {"operation": "delete", "id": "missing"}],
                        [{"operation": "update", "id": "missing", "preference": {"type": "x", "value": "y"}}]):
            with self.assertRaises(ValueError):
                self.change(changes)
            self.assertEqual(self.memory.read()["preferences"], [])

    def test_blank_unbounded_and_forged_metadata_rejected(self):
        for value in ("", "  ", [], [" "], "x" * 1001):
            with self.assertRaises(ValueError):
                TravelPreference(type="seat", value=value)
        with self.assertRaises(ValueError):
            PreferenceMutation(revision="0" * 64, changes=[{**addition(), "source": {"consent": "explicit"}}])
        with self.assertRaises(ValueError):
            PreferenceMutation(revision="0" * 64, changes=[])

    def test_compatibility_merge_does_not_replace_other_preferences(self):
        self.memory.save_preferences([{"type": "seat", "value": "window"}, {"type": "hotel", "value": "quiet"}])
        current = self.memory.preference_snapshot()
        changes = merge_changes(current, [TravelPreference(type="seat", value="aisle")])
        self.change(changes)
        self.assertEqual(self.memory.read()["preferences"][1], {"type": "hotel", "value": "quiet"})

    def test_another_user_cannot_delete_by_known_id(self):
        first = self.change([addition()])
        other = TravelMemory(self.root, self.root / "legacy", "bob")
        with self.assertRaises(PreferenceConflict):
            other.mutate_preferences(PreferenceMutation(
                revision=other.preference_snapshot()["revision"],
                changes=[{"operation": "delete", "id": first["items"][0]["id"]}]), {"kind": "user_interface"})
        self.assertEqual(self.memory.preference_snapshot(), first)

    def test_postgres_row_lock_scope_and_bound_values(self):
        settings = Settings("", "", "", "", "", self.root, self.root,
                            memory_database_url="synthetic", memory_company_id="company")
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.execute.return_value.fetchone.return_value = {"data": []}
        store = PostgresTravelMemory(settings, MemoryIdentity("company", "alice"))
        with patch("business_travel.memory_pg.memory_connection", return_value=connection):
            store.mutate_preferences(PreferenceMutation(revision=snapshot([])["revision"],
                changes=[addition(value="';DROP TABLE trips;--")]), {"kind": "user_interface"})
        calls = connection.execute.call_args_list
        self.assertIn("FOR UPDATE", calls[1].args[0])
        self.assertEqual(calls[1].args[1], ("company", "alice"))
        self.assertEqual(calls[2].args[1][1:], ("company", "alice"))
        self.assertNotIn("DROP TABLE", calls[2].args[0])
        connection.transaction.assert_called_once()


class PreferenceGraphTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.memory = TravelMemory(root, root / "legacy", "alice")
        self.settings = Settings("", "", "", "", "", root, root / "legacy")
        self.config = {"configurable": {"thread_id": "alice:one"}}

    def graph(self, args, review=None, tool="save_preferences", saver=None):
        models = {
            "supervisor": fixtures.ScriptedModel([fixtures.call(tool, args, "write"), AIMessage(content="done")]),
            "slots": fixtures.ScriptedModel([fixtures.slot_response()]),
            "reviewer": fixtures.ScriptedModel([fixtures.review_response(), review] if review else [
                fixtures.review_response()]),
        }
        return build_assistant(lambda role: models[role], self.settings, self.memory, PolicyStore(None, None),
                               checkpointer=saver or InMemorySaver())

    def args(self, **overrides):
        return {"preferences": [{"type": "seat", "value": "window"}],
                "context": {"subject": "self", "scope": "long_term", "assertion": "actual",
                            "quote": "Remember my seat", **overrides}}

    def status(self, result):
        return json.loads(next(message.content for message in reversed(result["messages"])
                               if isinstance(message, ToolMessage)))["status"]

    def test_explicit_request_review_writes_without_second_confirmation(self):
        graph = self.graph(self.args(), fixtures.call("PreferenceReview",
            {"decision": "accept", "quote": "Remember my seat"}, "review-pref"))
        result = graph.invoke({"messages": [HumanMessage(content="Remember my seat", id="source-1")]}, self.config)
        self.assertNotIn("__interrupt__", result)
        self.assertEqual(self.status(result), "saved")
        source = self.memory.preference_snapshot()["items"][0]["source"]
        self.assertEqual(source["message_id"], "source-1")
        self.assertEqual(source["capture"], "automatic")

    def test_temporary_hypothetical_other_person_and_quote_never_persist(self):
        for context in [{"scope": "current_trip"}, {"assertion": "hypothetical"},
                        {"subject": "other"}, {"assertion": "quoted"}, {"quote": "invented"}]:
            with self.subTest(context=context):
                result = self.graph(self.args(**context)).invoke({
                    "messages": [HumanMessage(content="Remember my seat")]}, self.config)
                self.assertEqual(self.status(result), "rejected")
                self.assertNotIn("__interrupt__", result)
                self.assertEqual(self.memory.read()["preferences"], [])

    def test_review_disagrees_with_proposal(self):
        graph = self.graph(self.args(), fixtures.call("PreferenceReview",
            {"decision": "reject", "quote": "Remember my seat"}, "review-pref"))
        result = graph.invoke({"messages": [HumanMessage(content="Remember my seat")]}, self.config)
        self.assertEqual(self.status(result), "rejected")
        self.assertEqual(self.memory.read()["preferences"], [])

    def test_review_invalid_or_unavailable_never_writes_or_requests_permission(self):
        for review in [AIMessage(content="accept"), fixtures.call("PreferenceReview",
                      {"decision": "accept", "quote": "invented"}, "review")]:
            graph = self.graph(self.args(), review)
            first = graph.invoke({"messages": [HumanMessage(content="Remember my seat")]}, self.config)
            self.assertNotIn("__interrupt__", first)
            self.assertEqual(self.status(first), "review_unavailable")
            self.assertEqual(self.memory.read()["preferences"], [])

    def test_unknown_context_requires_clarification_not_permission(self):
        for field in ("subject", "scope", "assertion"):
            graph = self.graph(self.args(**{field: "unknown"}))
            first = graph.invoke({"messages": [HumanMessage(content="Remember my seat")]}, self.config)
            self.assertNotIn("__interrupt__", first)
            self.assertEqual(self.status(first), "needs_clarification")
            self.assertEqual(self.memory.read()["preferences"], [])

    def test_checkpoint_restart_keeps_original_revision_before_write(self):
        saver = InMemorySaver()
        args = self.args()
        models = {
            "supervisor": fixtures.ScriptedModel([fixtures.call("save_preferences", args, "write")]),
            "slots": fixtures.ScriptedModel([fixtures.slot_response()]),
            "reviewer": fixtures.ScriptedModel([fixtures.review_response(), fixtures.call(
                "PreferenceReview", {"decision": "accept", "quote": "Remember my seat"}, "review")]),
        }
        first = build_assistant(lambda role: models[role], self.settings, self.memory,
                                PolicyStore(None, None), checkpointer=saver)
        # Pause before applying the reviewed proposal, not for user permission.
        first.invoke({"messages": [HumanMessage(content="Remember my seat")]}, self.config,
                     interrupt_before=["tools"])
        self.assertEqual(first.get_state(self.config).next, ("tools",))
        self.assertEqual(self.memory.read()["preferences"], [])
        self.memory.save_preferences([{"type": "seat", "value": "aisle"}])
        models = {"supervisor": fixtures.ScriptedModel([AIMessage(content="done")])}
        resumed = build_assistant(lambda role: models.get(role, Mock()), self.settings, self.memory,
                                  PolicyStore(None, None), checkpointer=saver)
        result = resumed.invoke(None, self.config)
        self.assertEqual(self.status(result), "conflict")
        self.assertEqual(self.memory.read()["preferences"], [{"type": "seat", "value": "aisle"}])

    def test_reviewed_merge_preserves_others_and_can_delete_by_id(self):
        self.memory.save_preferences([{"type": "hotel", "value": "quiet"}])
        graph = self.graph(self.args(), fixtures.call("PreferenceReview",
            {"decision": "accept", "quote": "Remember my seat"}, "review"))
        graph.invoke({"messages": [HumanMessage(content="Remember my seat")]}, self.config)
        current = self.memory.preference_snapshot()
        self.assertEqual(len(current["items"]), 2)
        args = {"mutation": {"revision": current["revision"], "changes": [
            {"operation": "delete", "id": current["items"][1]["id"]}]},
            "context": self.args(quote="Delete seat")["context"]}
        graph = self.graph(args, fixtures.call("PreferenceReview",
            {"decision": "accept", "quote": "Delete seat"}, "review"), tool="edit_preferences")
        result = graph.invoke({"messages": [HumanMessage(content="Delete seat")]}, self.config)
        self.assertEqual(self.status(result), "saved")
        self.assertEqual(self.memory.read()["preferences"], [{"type": "hotel", "value": "quiet"}])


class PreferenceAPITests(unittest.TestCase):
    def setUp(self):
        self.store = Mock()
        self.store.current_user.return_value = {"company_id": "company", "user_id": "alice"}
        settings = Settings("", "", "", "", "", Path("."), Path("."), memory_company_id="company")
        self.client = TestClient(create_app(settings, AuthSettings(), self.store), base_url="http://127.0.0.1:8000")
        self.origin = {"Origin": "http://127.0.0.1:8000"}
        self.body = {"revision": "0" * 64, "changes": [addition()]}

    def test_read_and_patch_identity_always_comes_from_session(self):
        with patch("business_travel.web.PostgresTravelMemory") as memory:
            memory.return_value.preference_snapshot.return_value = {"items": [], "revision": "0" * 64}
            memory.return_value.mutate_preferences.return_value = {"items": [], "revision": "1" * 64}
            self.assertEqual(self.client.get("/api/preferences").status_code, 200)
            self.assertEqual(self.client.patch("/api/preferences", headers=self.origin, json=self.body).status_code, 200)
            self.assertEqual(memory.call_args.args[1], MemoryIdentity("company", "alice"))
            mutation, source = memory.return_value.mutate_preferences.call_args.args
            self.assertEqual(source, {"kind": "user_interface", "consent": "explicit_request"})
            self.assertIsInstance(mutation, PreferenceMutation)

    def test_unauthenticated_cross_origin_extra_identity_and_query_rejected(self):
        with patch("business_travel.web.PostgresTravelMemory") as memory:
            self.assertEqual(self.client.patch("/api/preferences", json=self.body).status_code, 403)
            self.assertEqual(self.client.patch("/api/preferences", headers=self.origin,
                json={**self.body, "user_id": "victim"}).status_code, 422)
            self.assertEqual(self.client.patch("/api/preferences?user_id=victim",
                headers=self.origin, json=self.body).status_code, 400)
            self.assertEqual(self.client.get("/api/preferences?user_id=victim").status_code, 400)
            memory.return_value.mutate_preferences.assert_not_called()
            self.store.current_user.return_value = None
            self.assertEqual(self.client.get("/api/preferences").status_code, 401)
            self.assertEqual(self.client.patch("/api/preferences", headers=self.origin, json=self.body).status_code, 401)

    def test_conflict_and_invalid_operations_do_not_expose_internal_errors(self):
        with patch("business_travel.web.PostgresTravelMemory") as memory:
            for error, status in [(PreferenceConflict("private"), 409), (ValueError("private"), 422)]:
                memory.return_value.mutate_preferences.side_effect = error
                response = self.client.patch("/api/preferences", headers=self.origin, json=self.body)
                self.assertEqual(response.status_code, status)
                self.assertNotIn("private", response.text)

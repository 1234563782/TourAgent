"""Automatic preference persistence with synthetic reviews and real graph execution."""

import json
import tempfile
import unittest
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.checkpoint.memory import InMemorySaver

from business_travel.graph import build_assistant
from business_travel.memory import TravelMemory
from business_travel.policy import PolicyStore
from business_travel.settings import Settings
import test_travel_graph as fixtures
from scripts.verify_preference_dialogues import check_memory


class PreferenceAutosaveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.memory = TravelMemory(self.root, self.root / "legacy", "alice")
        self.settings = Settings("", "", "", "", "", self.root, self.root / "legacy")
        self.config = {"configurable": {"thread_id": "alice:autosave"}}

    def run_proposal(self, text, *, context=None, decision="accept", review_quote=None,
                     review=None, tool="save_preferences", payload=None):
        args = payload or {"preferences": [{"type": "transport", "value": "train"}]}
        args = {**args, "context": context or {
            "subject": "self", "scope": "long_term", "assertion": "actual", "quote": text,
        }}
        models = {
            "supervisor": fixtures.ScriptedModel([
                fixtures.call(tool, args, "save"), AIMessage(content="done")]),
            "slots": fixtures.ScriptedModel([fixtures.slot_response()]),
            "reviewer": fixtures.ScriptedModel([fixtures.review_response(), review or fixtures.call(
                "PreferenceReview", {"decision": decision, "quote": review_quote or text}, "review")]),
        }
        graph = build_assistant(lambda role: models[role], self.settings, self.memory,
                                PolicyStore(None, None), checkpointer=InMemorySaver())
        result = graph.invoke({"messages": [HumanMessage(content=text, id="current-message")]}, self.config)
        self.assertNotIn("__interrupt__", result)
        status = json.loads(next(message.content for message in reversed(result["messages"])
                                 if isinstance(message, ToolMessage)))["status"]
        return status, result

    def test_plain_statement_is_saved_without_authorization(self):
        status, _ = self.run_proposal("我喜欢坐火车")
        self.assertEqual(status, "saved")
        item, = self.memory.preference_snapshot()["items"]
        self.assertEqual(item["value"], "train")
        self.assertEqual(item["source"], {
            "kind": "chat", "message_id": "current-message", "quote": "我喜欢坐火车",
            "capture": "automatic",
        })
        rebuilt = TravelMemory(self.root, self.root / "legacy", "alice")
        self.assertEqual(rebuilt.read()["preferences"], [{"type": "transport", "value": "train"}])
        other = TravelMemory(self.root, self.root / "legacy", "bob")
        self.assertEqual(other.read()["preferences"], [])

    def test_memory_tools_require_object_context(self):
        schemas = {}

        class SchemaModel(fixtures.ScriptedModel):
            def bind_tools(self, tools, **kwargs):
                for tool in tools:
                    schema = convert_to_openai_tool(tool)["function"]
                    schemas[schema["name"]] = schema["parameters"]
                return super().bind_tools(tools, **kwargs)

        model = SchemaModel([])
        build_assistant(lambda role: model, self.settings, self.memory, PolicyStore(None, None))
        for name in ("save_preferences", "edit_preferences"):
            self.assertIn("context", schemas[name]["required"])
            self.assertEqual(schemas[name]["properties"]["context"]["type"], "object")

    def test_json_encoded_context_is_rejected_with_relevant_safe_error(self):
        status, result = self.run_proposal("I prefer trains", context=json.dumps({
            "subject": "self", "scope": "long_term", "assertion": "actual", "quote": "I prefer trains",
        }))
        self.assertEqual(status, "error")
        output = next(message.content for message in reversed(result["messages"])
                      if isinstance(message, ToolMessage))
        self.assertIn('"field": "context"', output)
        self.assertNotIn("check_travel_policy", output)
        self.assertNotIn("I prefer trains", output)
        self.assertEqual(self.memory.read()["preferences"], [])

    def test_live_acceptance_checks_memory_instead_of_only_completion(self):
        case = {"memory_expectation": {"count": 1, "records": [
            {"type": "seat", "value_contains": "window"}]}}
        self.assertTrue(check_memory(case, {"items": []}))
        self.assertTrue(check_memory(case, {"items": [{"type": "seat", "value": "aisle"}]}))
        self.assertFalse(check_memory(case, {"items": [{"type": "seat", "value": "window"}]}))
        self.assertTrue(check_memory({}, {"items": []}))

    def test_reviewer_still_checks_candidates_without_explicit_save_request(self):
        for decision in ("reject", "clarify"):
            with self.subTest(decision=decision):
                status, _ = self.run_proposal("我喜欢坐火车", decision=decision)
                self.assertEqual(status, "rejected" if decision == "reject" else "needs_clarification")
                self.assertEqual(self.memory.read()["preferences"], [])

    def test_invalid_review_does_not_write_or_ask_for_authorization(self):
        for review in (AIMessage(content="accept"), fixtures.call(
                "PreferenceReview", {"decision": "accept", "quote": "not in message"}, "invalid")):
            with self.subTest(review=review):
                status, _ = self.run_proposal("我喜欢坐火车", review=review)
                self.assertEqual(status, "review_unavailable")
                self.assertEqual(self.memory.read()["preferences"], [])

    def test_temporary_other_person_and_hypothetical_are_rejected(self):
        for changes in ({"scope": "current_trip"}, {"subject": "other"},
                        {"assertion": "hypothetical"}, {"assertion": "quoted"},
                        {"quote": "fabricated evidence"}):
            with self.subTest(changes=changes):
                context = {"subject": "self", "scope": "long_term", "assertion": "actual",
                           "quote": "test statement", **changes}
                status, _ = self.run_proposal("test statement", context=context)
                self.assertEqual(status, "rejected")
                self.assertEqual(self.memory.read()["preferences"], [])

    def test_version_conflict_does_not_overwrite_concurrent_change(self):
        def change_during_review(messages):
            self.memory.save_preferences([{"type": "transport", "value": "flight"}])
            return fixtures.call("PreferenceReview", {"decision": "accept", "quote": "I prefer trains"}, "review")

        status, _ = self.run_proposal("I prefer trains", review=change_during_review)
        self.assertEqual(status, "conflict")
        self.assertEqual(self.memory.read()["preferences"], [{"type": "transport", "value": "flight"}])

    def test_delete_is_reviewed_against_current_request(self):
        self.memory.save_preferences([{"type": "transport", "value": "flight"}])
        current = self.memory.preference_snapshot()
        payload = {"mutation": {"revision": current["revision"], "changes": [
            {"operation": "delete", "id": current["items"][0]["id"]}]}}
        status, _ = self.run_proposal("Hello", tool="edit_preferences", payload=payload, decision="reject")
        self.assertEqual(status, "rejected")
        self.assertEqual(len(self.memory.read()["preferences"]), 1)
        status, _ = self.run_proposal("Forget my transport preference", tool="edit_preferences", payload=payload)
        self.assertEqual(status, "saved")
        self.assertEqual(self.memory.read()["preferences"], [])

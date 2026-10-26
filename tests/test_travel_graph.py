"""Real LangGraph execution with scripted models and explicitly synthetic providers."""

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from business_travel.contracts import SearchResult
from business_travel.graph import agent_graph, build_assistant
from langchain_core.tools import tool
from langchain_core.utils.function_calling import convert_to_openai_tool
from business_travel.memory import TravelMemory
from business_travel.policy import PolicyStore
from business_travel.providers import normalize_flyai
from business_travel.settings import Settings
from scripts.slot_fixture import AtomicProposalFixture


def call(name, args, ident):
    if name == "SlotSubmission" and "tasks" not in args:
        args = {"tasks": [args]}
    if name == "SlotSubmission":
        # Migrate scripted fixtures to the public operation protocol, not model output.
        args = {**args, "tasks": [operation_fixture(task) for task in args.get("tasks", [])]}
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": ident, "type": "tool_call"}])


def operation_fixture(task):
    if "operation" in task and "action" not in task:
        return task
    value = dict(task)
    action = value.pop("action", "new")
    if action == "update":
        value["operation"] = "change_intent" if value.get("intent_change_evidence") else "update_facts"
        if value["operation"] == "update_facts":
            value.pop("intent", None)
    else:
        value["operation"] = "new_task"
    return value


def review_response(verdict="accept", issues=None, question=""):
    """Scripted protocol verdict, never a test of real language understanding."""
    return call("SemanticReview", {"verdict": verdict, "issues": issues or [],
                                  "question": question}, "review")


class ScriptedModel:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.tool_choice = None
        self.atomic_fixture = AtomicProposalFixture()
        self.messages = []

    def bind_tools(self, tools, **kwargs):
        self.tool_choice = kwargs.get("tool_choice")
        return self

    def invoke(self, messages):
        self.messages.append(messages)
        if self.tool_choice in {"TripExtraction", "ExpenseExtraction"} and self.atomic_fixture.proposal is not None:
            return self.atomic_fixture.reply(self.tool_choice, messages=messages)
        response = next(self.responses)
        response = response(messages) if callable(response) else response
        return self.atomic_fixture.reply(self.tool_choice, response, messages)


class FixtureProvider:
    def __init__(self, empty=False):
        self.requests = []
        self.empty = empty

    def search(self, kind, trip, inbound=False):
        self.requests.append(trip)
        if self.empty:
            return SearchResult(status="empty")
        payload = {"status": 0, "data": {"itemList": [{
            "ticketPrice": None, "jumpUrl": "https://example.com",
            "journeys": [{"segments": [{
                "depDateTime": f"{trip.departure_date} 09:00:00",
                "arrDateTime": f"{trip.departure_date} 11:00:00",
                "marketingTransportNo": "SYNTHETIC",
            }]}],
        }]}}
        return normalize_flyai(payload, kind, trip, 10)


def select_result(messages):
    result = json.loads(next(message.content for message in reversed(messages) if isinstance(message, HumanMessage)))
    return call("PlanSelection", {"selected_ids": [item["id"] for item in result["candidates"]]}, "submit")


PLAN_TEXT = "Beijing 到 Shanghai，2099-09-08 出发，flight"


def slot_response(values=None, intent="other", quotes=None, update=False, scope="transport",
                  policy_query=None):
    """Synthetic model response; citations still pass the real production validator."""
    def respond(messages):
        context = json.loads(next(m.content for m in reversed(messages) if isinstance(m, HumanMessage)))
        args = {"intent": intent}
        if intent == "policy_explain":
            args["policy_query"] = policy_query or {"category": "other", "mode": "general"}
        if values is not None:
            domain = "trip" if intent == "plan_trip" else "expense"
            # This fixture submits a predetermined classification, not an intent classifier.
            submitted = {"query_scope": scope, **values} if domain == "trip" and not update else values
            args[domain] = submitted
            args["evidence"] = {
                key: {"user_message": context["current_user_index"],
                      "quote": (quotes or {}).get(
                          key, context["current_message"] if key == "query_scope" else str(value))}
                for key, value in submitted.items() if key != "kind"
            }
            if update:
                subject = context["current_slots"][domain]
                args.update(action="update", target_id=subject["id"], base_revision=subject["revision"])
        return call("SlotSubmission", args, "slots")
    return respond


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.settings = Settings(api_key="", model="", base_url="", node_path="", flyai_cli_path="",
                                 storage_dir=root, legacy_memory_dir=root / "legacy")
        self.memory = TravelMemory(root, root / "legacy", "test")
        self.policy = PolicyStore(None, None)
        self.trip = {"origin": "Beijing", "destination": "Shanghai", "departure_date": "2099-09-08",
                     "transport": "flight"}
        self.config = {"configurable": {"thread_id": "test:session"}, "recursion_limit": 30}

    def tearDown(self):
        self.directory.cleanup()

    def build(self, supervisor, provider=None, checkpointer=None, slots=None):
        models = {"supervisor": ScriptedModel(supervisor),
                  "reviewer": ScriptedModel([review_response()] * 10),
                  "slots": ScriptedModel(slots if slots is not None else [slot_response()] * 10)}
        def factory(role):
            if role == "planner":
                return ScriptedModel([select_result])
            return models[role]
        return build_assistant(factory, self.settings, self.memory, self.policy,
                               checkpointer=checkpointer or InMemorySaver(), provider=provider or FixtureProvider())

    def test_supervisor_prompt_has_stable_prefix_and_current_date(self):
        with patch("business_travel.graph.agent_graph") as assemble:
            self.build([])
        self.assertNotIn("Today is", assemble.call_args.args[2])
        with patch("business_travel.graph.agent_graph") as assemble:
            build_assistant(lambda role: ScriptedModel([]),
                            replace(self.settings, prompt_date_mode="prefix"),
                            self.memory, self.policy, provider=FixtureProvider())
        self.assertIn(f"Today is {date.today().isoformat()}.", assemble.call_args.args[2])

        model = ScriptedModel([AIMessage(content="First"), AIMessage(content="Second")])
        graph = agent_graph(model, [], "Fixed rules")
        with patch("business_travel.graph.date") as clock:
            clock.today.side_effect = [date(2026, 10, 12), date(2026, 10, 13)]
            graph.invoke({"messages": [HumanMessage(content="First request")]})
            graph.invoke({"messages": [HumanMessage(content="Second request")]})

        for messages, expected_date in zip(model.messages, ("2026-10-12", "2026-10-13")):
            self.assertIsInstance(messages[0], SystemMessage)
            self.assertEqual(messages[0].content, "Fixed rules")
            self.assertEqual(messages[1].content, f"Today is {expected_date}.")
            self.assertIsInstance(messages[-1], HumanMessage)

        legacy = ScriptedModel([AIMessage(content="Legacy")])
        agent_graph(legacy, [], "Legacy rules with date", settings=replace(
            self.settings, prompt_date_mode="prefix")).invoke({"messages": [HumanMessage(content="Test")]})
        self.assertEqual([message.content for message in legacy.messages[0]
                          if isinstance(message, SystemMessage)], ["Legacy rules with date"])

    def test_planning_and_explicit_confirmation(self):
        graph = self.build([call("save_trip", {}, "save"), AIMessage(content="Saved, not booked.")],
                           slots=[slot_response(self.trip, "plan_trip"), slot_response()])
        graph.invoke({"messages": [HumanMessage(content=PLAN_TEXT)]}, self.config)
        result = graph.invoke({"messages": [HumanMessage(content="Save")]}, self.config)
        self.assertTrue(result["__interrupt__"])
        self.assertEqual(self.memory.read()["trip_history"], [])
        result = graph.invoke(Command(resume=True), self.config)
        self.assertNotIn("__interrupt__", result)
        self.assertEqual(len(self.memory.read()["trip_history"]), 1)
        self.assertIsNone(result["draft"]["selected"][0]["quote"]["amount"])

    def test_default_tuniu_provider_reaches_planner_without_leaking_token(self):
        config = replace(self.settings, tuniu_access_token="synthetic-private-token")
        models = {
            "reviewer": ScriptedModel([review_response()]),
            "supervisor": ScriptedModel([]),
            "slots": ScriptedModel([slot_response(self.trip, "plan_trip")]),
            "planner": ScriptedModel([select_result]),
        }
        graph = build_assistant(lambda role: models[role], config, self.memory, self.policy,
                                checkpointer=InMemorySaver())
        payload = {"successCode": True, "data": [{
            "flightNumber": "SYN001", "departureTime": "2099-09-08 09:00",
            "arrivalTime": "2099-09-08 11:00", "basePrice": "100", "totalTax": "50",
        }]}
        with patch("business_travel.tuniu.call_search", new_callable=AsyncMock, return_value=payload) as query:
            result = graph.invoke({"messages": [HumanMessage(content=PLAN_TEXT)]}, self.config)
        self.assertEqual(query.await_count, 1)
        self.assertEqual(result["draft"]["selected"][0]["source"], "tuniu")
        self.assertNotIn("synthetic-private-token", str(result))

    def test_reject_confirmation(self):
        graph = self.build([call("save_trip", {}, "save"), AIMessage(content="Cancelled.")],
                           slots=[slot_response(self.trip, "plan_trip"), slot_response()])
        graph.invoke({"messages": [HumanMessage(content=PLAN_TEXT)]}, self.config)
        graph.invoke({"messages": [HumanMessage(content="Save")]}, self.config)
        graph.invoke(Command(resume=False), self.config)
        self.assertEqual(self.memory.read()["trip_history"], [])

    def test_date_change_replaces_candidates(self):
        changed = {**self.trip, "departure_date": "2099-09-10"}
        provider = FixtureProvider()
        graph = self.build([], provider, slots=[slot_response(self.trip, "plan_trip"),
                           slot_response({"departure_date": changed["departure_date"]}, "plan_trip", update=True)])
        first = graph.invoke({"messages": [HumanMessage(content=PLAN_TEXT)]}, self.config)
        second = graph.invoke({"messages": [HumanMessage(content="改为2099-09-10")]}, self.config)
        self.assertNotEqual(first["draft"]["id"], second["draft"]["id"])
        self.assertEqual(second["draft"]["selected"][0]["request_date"], "2099-09-10")
        self.assertEqual(len(provider.requests), 2)

    def test_no_results_remains_incomplete(self):
        graph = self.build([], FixtureProvider(empty=True), slots=[slot_response(self.trip, "plan_trip")])
        result = graph.invoke({"messages": [HumanMessage(content=PLAN_TEXT)]}, self.config)
        self.assertEqual(result["draft"]["status"], "incomplete")
        self.assertEqual(result["draft"]["selected"], [])

    def test_clarification_does_not_call_provider(self):
        provider = FixtureProvider()
        graph = self.build([], provider, slots=[slot_response({}, "plan_trip")])
        result = graph.invoke({"messages": [HumanMessage(content="Business trip")]}, self.config)
        self.assertEqual(provider.requests, [])
        self.assertIn("出发日期", result["messages"][-1].content)

    def test_sqlite_resume_after_rebuild(self):
        filename = str(Path(self.directory.name) / "checkpoints.sqlite")
        with SqliteSaver.from_conn_string(filename) as saver:
            first = self.build([call("save_trip", {}, "save")], checkpointer=saver,
                               slots=[slot_response(self.trip, "plan_trip"), slot_response()])
            first.invoke({"messages": [HumanMessage(content=PLAN_TEXT)]}, self.config)
            result = first.invoke({"messages": [HumanMessage(content="Save")]}, self.config)
            self.assertTrue(result["__interrupt__"])
        with SqliteSaver.from_conn_string(filename) as saver:
            rebuilt = self.build([AIMessage(content="Saved after restart")], checkpointer=saver)
            result = rebuilt.invoke(Command(resume=True), self.config)
            self.assertNotIn("__interrupt__", result)
        self.assertEqual(len(self.memory.read()["trip_history"]), 1)
        self.assertEqual(result["slots"]["trip"]["values"]["origin"], "Beijing")

    def test_invalid_replan_clears_old_draft(self):
        invalid = {**self.trip, "departure_date": "2099-02-30"}
        bad = call("SlotSubmission", {"intent": "plan_trip", "trip": invalid}, "bad")
        graph = self.build([], slots=[slot_response(self.trip, "plan_trip"), bad, bad, bad])
        graph.invoke({"messages": [HumanMessage(content=PLAN_TEXT)]}, self.config)
        result = graph.invoke({"messages": [HumanMessage(content="改为2099-02-30")]}, self.config)
        self.assertIsNone(result["draft"])
        self.assertEqual(result["slots"]["trip"]["values"]["departure_date"], "2099-09-08")

    def test_multiple_tool_calls_are_not_executed(self):
        multiple = AIMessage(content="", tool_calls=[
            {"name": "plan_trip", "args": {"request": self.trip}, "id": "one", "type": "tool_call"},
            {"name": "save_trip", "args": {}, "id": "two", "type": "tool_call"},
        ])
        provider = FixtureProvider()
        graph = self.build([multiple, AIMessage(content="Will retry one at a time")], provider)
        graph.invoke({"messages": [HumanMessage(content="Plan")]}, self.config)
        self.assertEqual(provider.requests, [])
        self.assertEqual(self.memory.read()["trip_history"], [])

    def test_policy_specialist_calls_rule_tool_and_preserves_date(self):
        policy = Mock()
        policy.search.return_value = {"status": "ok", "sources": [{"source": "test.docx", "text": "test"}]}
        policy.check.return_value = {"status": "within_checked_rules", "cap": "420", "approved": False,
                                     "sources": [{"source": "test.docx", "clause": "4.2"}]}
        facts = {"kind": "hotel", "expense_date": "2099-09-08", "destination": "上海"}
        models = {
            "reviewer": ScriptedModel([review_response()]),
            "supervisor": ScriptedModel([]),
            "slots": ScriptedModel([slot_response(facts, "policy_check")]),
        }
        graph = build_assistant(lambda role: models[role], self.settings, self.memory, policy,
                                provider=FixtureProvider())
        result = graph.invoke({"messages": [HumanMessage(content="2099-09-08上海住宿标准")]})
        policy.check.assert_called_once()
        self.assertIsNone(policy.check.call_args.args[0].employee_grade)
        self.assertEqual(policy.check.call_args.args[0].expense_date.isoformat(), "2099-09-08")
        response = next(m for m in result["messages"] if isinstance(m, ToolMessage))
        self.assertIn("4.2", response.content)

    def test_policy_check_cannot_silently_switch_expense_date(self):
        policy = Mock()
        policy.search.return_value = {"status": "ok", "sources": []}
        models = {
            "reviewer": ScriptedModel([review_response("revise", ["expense_date contradicts user date"])] * 3),
            "supervisor": ScriptedModel([]),
            "slots": ScriptedModel([call("SlotSubmission", {"intent": "policy_check", "expense": {
                "kind": "meal", "expense_date": "2099-09-09", "destination": "上海"}, "evidence": {
                "expense_date": {"user_message": 0, "quote": "2099-09-08"},
                "destination": {"user_message": 0, "quote": "上海"}}}, "bad")] * 3),
        }
        graph = build_assistant(lambda role: models[role], self.settings, self.memory, policy,
                                provider=FixtureProvider())
        graph.invoke({"messages": [HumanMessage(content="2099-09-08上海")]})
        policy.check.assert_not_called()

    def test_long_term_preference_tools_are_not_exposed(self):
        schemas = {}

        class InspectModel(ScriptedModel):
            def bind_tools(self, tools, **kwargs):
                schemas.update({convert_to_openai_tool(item)["function"]["name"]: convert_to_openai_tool(item)
                                for item in tools})
                return self

        model = InspectModel([AIMessage(content="不会保存长期偏好")])
        models = {"slots": ScriptedModel([slot_response()]), "supervisor": model,
                  "reviewer": ScriptedModel([review_response()])}
        graph = build_assistant(lambda role: models[role], self.settings, self.memory, self.policy,
                                checkpointer=InMemorySaver(), provider=FixtureProvider())
        self.assertNotIn("save_preferences", schemas)
        self.assertNotIn("edit_preferences", schemas)
        result = graph.invoke({"messages": [HumanMessage(content="记住我喜欢安静酒店")]}, self.config)
        self.assertNotIn("__interrupt__", result)
        self.assertNotIn("preferences", self.memory.read())

    def test_policy_receives_original_user_context(self):
        policy = Mock()
        policy.search.return_value = {"status": "ok", "sources": []}
        policy.check.return_value = {"status": "within_checked_rules", "approved": False,
                                     "sources": []}
        original = "2099-09-08上海出差两天，无专项审批，单人入住。"

        def inspect_context(messages):
            data = json.loads(messages[-1].content)
            self.assertEqual(data["current_message"], original)
            return slot_response({"kind": "hotel", "expense_date": "2099-09-08",
                                  "destination": "上海", "special_approval": False},
                                 "policy_check", {"special_approval": "无专项审批"})(messages)

        models = {
            "reviewer": ScriptedModel([review_response()]),
            "supervisor": ScriptedModel([]),
            "slots": ScriptedModel([inspect_context]),
        }
        graph = build_assistant(lambda role: models[role], self.settings, self.memory, policy,
                                provider=FixtureProvider())
        result = graph.invoke({"messages": [HumanMessage(content=original)]})
        response = next(m for m in result["messages"] if isinstance(m, ToolMessage))
        self.assertIn("rule_checks", json.loads(response.content))

    def test_tool_error_category_without_sensitive_message(self):
        @tool
        def failing_tool() -> dict:
            """Synthetic failure."""
            raise TimeoutError("secret-token-in-url")

        model = ScriptedModel([call("failing_tool", {}, "fail"), AIMessage(content="Unavailable")])
        graph = agent_graph(model, [failing_tool], "Test")
        result = graph.invoke({"messages": [HumanMessage(content="Test")]})
        response = next(m for m in result["messages"] if isinstance(m, ToolMessage))
        self.assertIn("TimeoutError", response.content)
        self.assertNotIn("secret-token", response.content)


if __name__ == "__main__":
    unittest.main()

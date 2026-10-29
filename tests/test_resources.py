"""Budget propagation and configurable boundaries without provider traffic."""

from contextlib import nullcontext
from dataclasses import replace
import os
import time
import unittest
from unittest.mock import Mock, patch

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import StateGraph, START, END

from business_travel.context_budget import BudgetedModel
from business_travel.graph import AgentState
from business_travel.resources import (RequestBudget, CallBudgetExceeded, RunDeadlineExceeded,
                                       _current_budget, AdmissionRejected)
from business_travel.settings import Settings
from tests.test_auth import settings
from tests import test_web_chat as web_fixtures


class ResourceTests(unittest.TestCase):
    def test_env_configuration_and_invalid_limits(self):
        # The ceiling must cover a full-length graph, so a smaller override only
        # parses when max steps are lowered with it.
        with patch.dict(os.environ, {"TRAVEL_LOAD_DOTENV": "0",
                                    "TRAVEL_MAX_STEPS": "20",
                                    "TRAVEL_RESOURCE_REQUEST_CALLS": "28",
                                    "TRAVEL_RESOURCE_QUEUE_TIMEOUT": "0.25",
                                    "TRAVEL_DB_POOL_TIMEOUT": "0.5"}, clear=True):
            self.assertEqual(Settings.from_env().resource_request_calls, 28)
            self.assertEqual(Settings.from_env().resource_queue_timeout, 0.25)
            self.assertEqual(Settings.from_env().db_pool_timeout, 0.5)
        for values in [
            {"resource_queue_size": -1}, {"resource_user_concurrency": 0},
            {"resource_user_concurrency": 9},
            {"resource_queue_timeout": float("nan")}, {"resource_poll_seconds": 11},
            {"resource_user_daily_calls": 1}, {"db_session_pool_size": 2},
            {"db_pool_waiting": 0}, {"db_pool_timeout": float("inf")},
            # Ceiling below max steps plus repair and review headroom.
            {"resource_request_calls": 20},
            # Concurrency plus queue beyond the synchronous worker budget.
            {"resource_global_concurrency": 12, "resource_queue_size": 8},
        ]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                replace(settings(), **values).validate_resources()

    def test_call_budget_extends_in_tranches_and_stops_at_ceiling(self):
        extended = []
        budget = RequestBudget(20, Mock(), tranche=8, extend=extended.append)
        for _ in range(20):
            budget.consume()
        self.assertEqual(extended, [8, 4])
        self.assertEqual((budget.reserved, budget.used), (20, 20))
        with self.assertRaises(CallBudgetExceeded):
            budget.consume()
        self.assertTrue(budget.exhausted)

    def test_refused_tranche_extension_never_reaches_provider(self):
        provider = Mock()
        provider.invoke.return_value = AIMessage("ok")
        model = BudgetedModel(provider, settings(), "supervisor")
        def refuse(count):
            raise CallBudgetExceeded("daily_call_quota_exceeded")
        budget = RequestBudget(20, Mock(), tranche=1, extend=refuse)
        token = _current_budget.set(budget)
        try:
            model.invoke([HumanMessage("one")])
            with self.assertRaises(CallBudgetExceeded):
                model.invoke([HumanMessage("two")])
        finally:
            _current_budget.reset(token)
        self.assertEqual(provider.invoke.call_count, 1)

    def test_request_deadline_blocks_provider_call(self):
        provider = Mock()
        model = BudgetedModel(provider, settings(), "supervisor")
        budget = RequestBudget(5, Mock(), deadline=time.monotonic() - 1)
        token = _current_budget.set(budget)
        try:
            with self.assertRaises(RunDeadlineExceeded):
                model.invoke([HumanMessage("late")])
        finally:
            _current_budget.reset(token)
        self.assertEqual(provider.invoke.call_count, 0)
        self.assertTrue(budget.exhausted)

    def test_failure_and_bound_tools_each_consume_before_provider_call(self):
        provider = Mock()
        provider.bind_tools.return_value = provider
        provider.invoke.side_effect = [RuntimeError("provider"), AIMessage("ok")]
        model = BudgetedModel(provider, settings(), "supervisor").bind_tools([])
        budget = RequestBudget(2, Mock())
        token = _current_budget.set(budget)
        try:
            with self.assertRaises(RuntimeError):
                model.invoke([HumanMessage("one")])
            model.invoke([HumanMessage("two")])
            with self.assertRaises(CallBudgetExceeded):
                model.invoke([HumanMessage("three")])
            self.assertEqual(provider.invoke.call_count, 2)
            self.assertTrue(budget.exhausted)
        finally:
            _current_budget.reset(token)

    def test_real_graph_parallel_nodes_share_one_thread_safe_budget(self):
        provider = Mock()
        provider.invoke.return_value = AIMessage("ok")
        model = BudgetedModel(provider, settings(), "supervisor")
        def call(state):
            try:
                model.invoke([HumanMessage("synthetic")])
            except CallBudgetExceeded:
                pass
            return {}
        graph = StateGraph(AgentState)
        for name in ("left", "right"):
            graph.add_node(name, call)
            graph.add_edge(START, name)
            graph.add_edge(name, END)
        budget = RequestBudget(1, Mock())
        token = _current_budget.set(budget)
        try:
            list(graph.compile().stream({"messages": []}))
        finally:
            _current_budget.reset(token)
        self.assertEqual(provider.invoke.call_count, 1)
        self.assertEqual(budget.used, 1)
        self.assertTrue(budget.exhausted)
        self.assertIsNone(_current_budget.get())

    def test_disconnected_slot_owner_blocks_provider(self):
        connection = Mock()
        connection.execute.side_effect = RuntimeError("closed")
        budget = RequestBudget(2, connection)
        with self.assertRaises(RuntimeError):
            budget.consume()
        self.assertEqual(budget.used, 0)


class AdmissionHTTPTests(unittest.TestCase):
    setUp = web_fixtures.ChatTests.setUp
    post = web_fixtures.ChatTests.post

    def test_missing_quota_storage_fails_startup_without_secret_disclosure(self):
        with patch("business_travel.web.memory_connection", side_effect=RuntimeError("private-dsn")):
            with self.assertRaisesRegex(RuntimeError, "init-resources") as error:
                with self.client:
                    pass
        self.assertNotIn("private-dsn", str(error.exception))

    def test_preflight_rejection_preserves_headers_and_never_starts_run(self):
        with patch("business_travel.web.execution_resources",
                   side_effect=AdmissionRejected("daily_quota", 20)):
            for path in ("/api/chat", "/api/chat/stream"):
                response = self.client.post(path, json={
                    "request_id": "a" * 32, "message": "hello"},
                    headers={"Origin": "http://127.0.0.1:8000"})
                self.assertEqual(response.status_code, 429)
                self.assertEqual(response.headers["X-Travel-Admission"], "rejected")
                self.assertEqual(response.headers["Retry-After"], "20")
        self.runs.start.assert_not_called()
        self.graph.stream.assert_not_called()

    def test_mid_execution_limit_requires_review_not_admission_retry(self):
        with patch("business_travel.web.execution_resources",
                   return_value=nullcontext(Mock(exhausted=True))):
            response = self.post({"message": "hello"})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("X-Travel-Admission", response.headers)
        self.assertEqual(self.runs.finish.call_args.args[1], "failed")

"""Business dates survive updates without selecting a policy version."""

import unittest
from unittest.mock import patch

from langchain_core.messages import HumanMessage

from business_travel.policy_guard import render_policy
from tests import test_travel_graph as fixtures
from tests.test_travel_graph import slot_response


class PolicyDateTests(unittest.TestCase):
    def test_legacy_default_is_not_displayed_as_a_business_date(self):
        text = render_policy({"purpose": "explain", "policy_date": {
            "value": "2026-09-10", "source": "system_default", "selection": "inherited"}})
        self.assertNotIn("2026-09-10", text)
        self.assertNotIn("政策适用日期", text)


class PolicyDateGraphTests(unittest.TestCase):
    setUp = fixtures.GraphTests.setUp
    tearDown = fixtures.GraphTests.tearDown
    build = fixtures.GraphTests.build

    @staticmethod
    def result(*args, **kwargs):
        return {"purpose": "explain", "status": "ok",
                "sources": [], "rag": {"status": "answered", "proposal": {"claims": []}}}

    def test_unknown_date_stays_unknown_on_followup(self):
        graph = self.build([], slots=[
            slot_response({"destination": "甲"}, "policy_explain"),
            slot_response({"destination": "乙"}, "policy_explain", update=True)])
        with patch("business_travel.graph.explain_policy", side_effect=self.result) as rag:
            first = graph.invoke({"messages": [HumanMessage(content="甲的政策")]}, self.config)
            second = graph.invoke({"messages": [HumanMessage(content="改为乙")]}, self.config)
        self.assertEqual(rag.call_count, 2)
        self.assertTrue(all("expense_date" not in call.kwargs["facts"] for call in rag.call_args_list))
        self.assertEqual(second["slots"]["expense"]["id"], first["slots"]["expense"]["id"])
        self.assertNotIn("policy_date", second["slots"]["expense"])
        self.assertNotIn("expense_date", second["slots"]["expense"]["values"])

    def test_explicit_inherited_withdrawn_and_replaced_business_date(self):
        graph = self.build([], slots=[
            slot_response({"expense_date": "2026-09-09"}, "policy_explain"),
            slot_response({"destination": "乙"}, "policy_explain", update=True),
            slot_response({"expense_date": None}, "policy_explain",
                          quotes={"expense_date": "日期不确定"}, update=True),
            slot_response({"expense_date": "2026-09-12"}, "policy_explain", update=True)])
        with patch("business_travel.graph.explain_policy", side_effect=self.result) as rag:
            graph.invoke({"messages": [HumanMessage(content="2026-09-09出差的住宿标准")]}, self.config)
            inherited = graph.invoke({"messages": [HumanMessage(content="改为乙")]}, self.config)
            evidence = inherited["slots"]["expense"]["evidence"]["expense_date"]
            self.assertEqual(evidence["user_message"], 0)
            self.assertEqual(evidence["status"], "context_reviewed")
            withdrawn = graph.invoke({"messages": [HumanMessage(content="日期不确定")]}, self.config)
            self.assertEqual(rag.call_count, 3)
            self.assertNotIn("哪一天适用的政策", withdrawn["messages"][-1].content)
            replaced = graph.invoke({"messages": [HumanMessage(content="2026-09-12")]}, self.config)
        self.assertEqual([call.kwargs["facts"].get("expense_date") for call in rag.call_args_list],
                         ["2026-09-09", "2026-09-09", None, "2026-09-12"])
        self.assertNotIn("policy_date", replaced["slots"]["expense"])

"""Current published policy selection must not fabricate a travel date."""

import unittest
from unittest.mock import patch

from langchain_core.messages import HumanMessage

from test_policy_rag import chunk_row, database_doc, store_with_db
import test_travel_graph as graph_fixtures
from test_travel_graph import slot_response


class CurrentPolicyStoreTests(unittest.TestCase):
    def test_current_publication_is_scoped_and_not_selected_by_effective_date(self):
        store, conn, _ = store_with_db([
            [database_doc()], [{"chunk_id": "one"}], [{"chunk_id": "one"}], [chunk_row("one")]])
        result = store.search("交通标准")
        self.assertEqual(result["status"], "ok")
        query = next(call for call in conn.execute.call_args_list if "FROM policy_documents d" in call.args[0])
        self.assertEqual(query.args[1], ("travel",))
        self.assertIn("d.status='published'", query.args[0])
        self.assertIn("published_at DESC", query.args[0])
        self.assertIn("LIMIT 1", query.args[0])
        self.assertNotIn("effective_from<=", query.args[0])
        self.assertNotIn("as_of", result)

    def test_missing_current_policy_never_embeds_or_requests_a_date(self):
        store, _, _ = store_with_db([[]])
        self.assertEqual(store.search("一等座")["reason"], "no_published_policy")
        store.embedder.embed.assert_not_called()


class CurrentPolicyGraphTests(unittest.TestCase):
    setUp = graph_fixtures.GraphTests.setUp
    tearDown = graph_fixtures.GraphTests.tearDown
    build = graph_fixtures.GraphTests.build

    @staticmethod
    def answer(*args, **kwargs):
        return {"purpose": "explain", "status": "ok", "sources": [],
                "rag": {"status": "answered", "proposal": {"claims": []}}}

    def test_general_question_has_no_synthetic_policy_or_expense_date(self):
        graph = self.build([], slots=[slot_response({}, "policy_explain")])
        with patch("business_travel.graph.explain_policy", side_effect=self.answer) as rag:
            output = graph.invoke({"messages": [HumanMessage(content="一等座标准是什么？")]}, self.config)
        self.assertEqual(len(rag.call_args.args), 4)
        self.assertNotIn("expense_date", rag.call_args.kwargs["facts"])
        self.assertNotIn("policy_date", output["slots"]["expense"])
        self.assertNotIn("政策适用日期", output["messages"][-1].content)

    def test_withdrawing_trip_date_does_not_block_policy_consultation(self):
        graph = self.build([], slots=[
            slot_response({"expense_date": "2026-10-08"}, "policy_explain"),
            slot_response({"expense_date": None}, "policy_explain",
                          quotes={"expense_date": "日期没定"}, update=True)])
        with patch("business_travel.graph.explain_policy", side_effect=self.answer) as rag:
            graph.invoke({"messages": [HumanMessage(content="2026-10-08的出差住宿标准")]}, self.config)
            output = graph.invoke({"messages": [HumanMessage(content="日期没定，先解释政策")]}, self.config)
        self.assertEqual(rag.call_count, 2)
        self.assertIsNone(rag.call_args.kwargs["facts"]["expense_date"])
        self.assertNotIn("哪一天适用的政策", output["messages"][-1].content)

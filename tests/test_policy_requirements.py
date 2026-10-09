"""Policy intent, deterministic applicability and clarification boundary regressions."""

import json
import unittest
from unittest.mock import patch

from langchain_core.messages import HumanMessage

from business_travel.slots import extract_slots
from business_travel.policy_rag import explain_policy
from business_travel.policy_guard import render_policy
from test_slot_protocol import RawModel, reply, accepted
from test_policy_hybrid import FakeModel, answer, result, source, tool_response
from test_policy_rag import settings, store_with_db, database_doc, chunk_row
from tests import test_travel_graph as fixtures
from test_travel_graph import call, slot_response


class PolicyProtocolTests(unittest.TestCase):
    def extract(self, patch_data, *, attempts=1, extra=()):
        route = reply("TurnRoute", {"tasks": [{
            "operation": "new_task", "intent": "policy_explain", "question": "住宿标准"}]})
        model = RawModel([route, reply("ExpenseExtraction", {"patches": [patch_data]}), *extra])
        reviewer = RawModel([accepted()])
        return extract_slots(model, [HumanMessage("我的住宿上限是多少？")], {}, attempts,
                             reviewer=reviewer), model, reviewer

    def test_missing_policy_classification_is_rejected_before_review(self):
        data, _, reviewer = self.extract({"task_index": 0, "expense": {"kind": "hotel"}})
        self.assertEqual(data["status"], "error")
        self.assertFalse(reviewer.calls)

    def test_missing_kind_is_repaired_not_filled_from_question(self):
        invalid = {"task_index": 0, "expense": {},
                   "policy_query": {"category": "hotel", "mode": "personal_limit"}}
        valid = {**invalid, "expense": {"kind": "hotel"}}
        route = reply("TurnRoute", {"tasks": [{
            "operation": "new_task", "intent": "policy_explain", "question": "住宿标准"}]})
        data, model, reviewer = self.extract(invalid, attempts=2, extra=[
            route, reply("ExpenseExtraction", {"patches": [valid]})])
        self.assertEqual(data["status"], "ok")
        subject = data["tasks"][0]["subject"]
        self.assertEqual(subject["policy_query"], valid["policy_query"])
        self.assertEqual(subject["request_origin"], 0)
        self.assertEqual(len(reviewer.calls), 1)
        self.assertIn("policy_category_requires_matching_kind", model.calls[2][-1].content)

    def test_cross_topic_standard_repairs_invented_category_without_forced_clarification(self):
        route = reply("TurnRoute", {"tasks": [{
            "operation": "new_task", "intent": "policy_explain",
            "question": "目前我是院士，我的标准是什么"}]})
        proposal = {
            "task_index": 0,
            "expense": {"academic_title": "院士"},
            "evidence": {"academic_title": {"quote": "院士"}},
            "policy_query": {"category": "transport", "mode": "personal_limit"},
        }
        repaired = {**proposal, "policy_query": {"category": "other", "mode": "personal_limit"}}
        model = RawModel([route, reply("ExpenseExtraction", {"patches": [proposal]}),
                          route, reply("ExpenseExtraction", {"patches": [repaired]})])
        reviewer = RawModel([accepted()])
        current = {}
        result = extract_slots(
            model,
            [HumanMessage("目前我是院士，我的标准是什么")],
            current,
            2,
            reviewer=reviewer,
        )
        self.assertEqual(result["status"], "ok")
        subject = result["tasks"][0]["subject"]
        self.assertEqual(subject["policy_query"], repaired["policy_query"])
        self.assertEqual(subject["values"]["academic_title"], "院士")
        self.assertNotIn("kind", subject["values"])
        self.assertEqual(len(model.calls), 4)
        self.assertEqual(len(reviewer.calls), 1)
        self.assertEqual(current, {})

    def test_unrepaired_category_mismatch_is_not_treated_as_user_ambiguity(self):
        invalid = {"task_index": 0, "expense": {"kind": "meal"},
                   "policy_query": {"category": "hotel", "mode": "personal_limit"}}
        data, _, reviewer = self.extract(invalid)
        self.assertEqual(data["status"], "error")
        self.assertIn("policy_category_requires_matching_kind", data["validation_errors"])
        self.assertFalse(reviewer.calls)

    def test_genuinely_ambiguous_reference_uses_existing_route_clarification(self):
        question = "您指的是哪一项差旅标准？"
        model = RawModel([reply("TurnRoute", {"clarification": question})])
        reviewer = RawModel([])
        current = {}
        data = extract_slots(model, [HumanMessage("那个标准呢")], current, 2,
                             reviewer=reviewer)
        self.assertEqual(data["status"], "clarify")
        self.assertEqual(data["question"], question)
        self.assertFalse(reviewer.calls)
        self.assertEqual(current, {})

    def test_explicit_transport_standard_keeps_matching_kind(self):
        route = reply("TurnRoute", {"tasks": [{
            "operation": "new_task", "intent": "policy_explain",
            "question": "我的交通标准是什么"}]})
        proposal = {
            "task_index": 0,
            "expense": {"kind": "transport"},
            "policy_query": {"category": "transport", "mode": "personal_limit"},
        }
        model = RawModel([route, reply("ExpenseExtraction", {"patches": [proposal]})])
        reviewer = RawModel([accepted()])
        result = extract_slots(
            model,
            [HumanMessage("我的交通标准是什么")],
            {},
            1,
            reviewer=reviewer,
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["tasks"][0]["subject"]["values"]["kind"], "transport")
        self.assertEqual(len(reviewer.calls), 1)


class PolicyLookupTests(unittest.TestCase):
    def test_missing_city_and_rank_are_returned_by_store(self):
        store, _, _ = store_with_db([
            [database_doc(policy_profile="university")], [{"chunk_id": "one"}], [],
            [], [chunk_row("one")],
        ])
        data = store.search_for_facts("住宿标准", {"kind": "hotel"})
        self.assertEqual(data["status"], "ok")
        self.assertEqual(set(data["lodging_lookup"]["missing_fields"]),
                         {"destination", "lodging_standard"})


class PolicyRequirementTests(unittest.TestCase):
    def run_policy(self, proposals, *, mode="personal_limit", exact=None, query=True, retrieved=None):
        class Store:
            def search_for_facts(self, question, facts):
                return result(policy_profile="university",
                    lodging_lookup=exact or {
                        "status": "insufficient_information",
                        "reason": "lodging_inputs_required",
                        "missing_fields": ["destination", "lodging_standard"]},
                    **(retrieved or {}))
        self.writer = FakeModel([tool_response("PolicyAnswer", p) for p in proposals])
        self.reviewer = FakeModel([tool_response("PolicyAnswerReview", {"verdict": "accept"})] * 4)
        models = {"policy": self.writer, "policy_reviewer": self.reviewer}
        return explain_policy(Store(), lambda role: models[role], settings(),
            "只介绍住宿标准，不涉及目的地",
            facts={"kind": "hotel"}, user_request="我还没说目的地，我的住宿标准是多少？",
            policy_query={"category": "hotel", "mode": mode} if query is True else query)

    def test_legacy_or_invalid_classification_cannot_bypass_personal_guard(self):
        for query in (None, {}, {"category": "hotel", "mode": "unsupported"},
                      {"category": "meal", "mode": "personal_limit"}):
            with self.subTest(query=query):
                data = self.run_policy([answer()], query=query)
                self.assertNotEqual(data["rag"]["status"], "answered")
                self.assertFalse(self.writer.messages)
                self.assertIn("重新说明", render_policy(data))

    def test_exact_lookup_requires_current_row_evidence_and_date(self):
        for exact in (
            {"status": "ok", "row_ids": ["missing-row"], "date": None},
            {"status": "ok", "row_ids": ["b003-0"], "date": "2099-01-01"},
            {"status": "insufficient_information", "row_ids": ["missing-row"],
             "date": None, "missing_fields": ["expense_date"]},
            {"status": "insufficient_information", "missing_fields": ["policy_date"]},
        ):
            with self.subTest(exact=exact):
                data = self.run_policy([answer()], exact=exact)
                self.assertNotEqual(data["rag"]["status"], "answered")
                self.assertFalse(self.writer.messages)

    def test_complete_exact_lookup_does_not_require_unrelated_date(self):
        data = self.run_policy([answer()], exact={
            "status": "ok", "row_ids": ["b003-0"], "date": None})
        self.assertEqual(data["rag"]["status"], "answered")
        self.assertEqual(data["requirements"]["missing_fields"], [])
        self.assertEqual(len(self.writer.messages), 1)
        self.assertEqual(len(self.reviewer.messages), 1)

    def test_complete_lookup_still_rejects_model_reasking_resolved_conditions(self):
        data = self.run_policy([answer(decision="clarify", claims=[], question="哪天入住？",
                                      requested_fields=["expense_date"])] * 2,
                              exact={"status": "ok", "row_ids": ["b003-0"], "date": None})
        self.assertEqual(data["rag"]["status"], "unavailable")
        self.assertFalse(self.reviewer.messages)

    def test_deterministic_clarification_does_not_bypass_evidence_guards(self):
        for retrieved in (
            {"sources": []},
            {"context_incomplete": True},
            {"sources": [source(document_id=None)]},
            {"sources": [source(), source(document_id=8, version="another")]},
        ):
            with self.subTest(retrieved=retrieved):
                data = self.run_policy([], retrieved=retrieved)
                self.assertNotIn(data["rag"]["status"], {"answered", "clarify"})
                self.assertFalse(self.writer.messages)
                self.assertFalse(self.reviewer.messages)

    def test_personal_limit_missing_conditions_do_not_call_models(self):
        data = self.run_policy([])
        self.assertEqual(data["rag"]["status"], "clarify")
        self.assertEqual(data["status"], "insufficient_information")
        self.assertEqual(data["rag"]["proposal"]["requested_fields"],
                         ["destination", "lodging_standard"])
        self.assertEqual(data["rag"]["proposal"]["claims"], [])
        self.assertFalse(self.writer.messages)
        self.assertFalse(self.reviewer.messages)

    def test_model_draft_cannot_add_unrelated_conditions(self):
        data = self.run_policy([answer(decision="clarify", claims=[], question="哪天入住？",
                                      requested_fields=["expense_date"])] * 2)
        self.assertEqual(data["rag"]["status"], "clarify")
        self.assertEqual(data["rag"]["proposal"]["requested_fields"],
                         ["destination", "lodging_standard"])
        self.assertNotIn("日期", render_policy(data))
        self.assertFalse(self.writer.messages)
        self.assertFalse(self.reviewer.messages)

    def test_general_introduction_is_not_forced_to_supply_personal_conditions(self):
        data = self.run_policy([answer()], mode="general")
        self.assertEqual(data["rag"]["status"], "answered")
        self.assertEqual(len(self.writer.messages), 1)
        self.assertEqual(len(self.reviewer.messages), 1)

    def test_personal_limit_requires_exact_lookup_even_if_model_has_citations(self):
        data = self.run_policy([answer()], exact={"status": "error"})
        self.assertNotEqual(data["rag"]["status"], "answered")
        self.assertFalse(self.writer.messages)

    def test_deterministic_clarification_cannot_leak_free_text_claims_or_extra_fields(self):
        draft = answer(decision="clarify", claims=[],
                       question="请给住宿档次，另外也给日期。某地有特殊季节标准。",
                       requested_fields=["lodging_standard"])
        data = self.run_policy([draft], exact={"status": "insufficient_information",
            "reason": "lodging_inputs_required", "missing_fields": ["lodging_standard"]})
        expected = "请补充或核实：住宿附表人员档次。"
        self.assertEqual(data["rag"]["proposal"]["question"], expected)
        self.assertEqual(data["rag"]["proposal"]["requested_fields"], ["lodging_standard"])
        self.assertFalse(self.writer.messages)
        self.assertFalse(self.reviewer.messages)

    def test_invalid_requirements_fail_closed_without_model_calls(self):
        for exact in (
            {"status": "insufficient_information", "missing_fields": []},
            {"status": "insufficient_information", "missing_fields": ["destination", "destination"]},
            {"status": "insufficient_information", "missing_fields": [["destination"]]},
            {"status": "insufficient_information", "missing_fields": "destination"},
            {"status": "ok", "row_ids": ["b003-0"], "date": None,
             "missing_fields": ["destination"]},
        ):
            with self.subTest(exact=exact):
                data = self.run_policy([], exact=exact)
                self.assertEqual(data["rag"]["status"], "insufficient")
                self.assertEqual(data["reason"], "lodging_requirements_invalid")
                self.assertFalse(self.writer.messages)
                self.assertFalse(self.reviewer.messages)

    def test_seasonal_missing_date_uses_bound_row_without_model(self):
        data = self.run_policy([], exact={
            "status": "insufficient_information", "reason": "lodging_date_required",
            "row_ids": ["b003-0"], "date": None, "missing_fields": ["expense_date"]})
        self.assertEqual(data["rag"]["status"], "clarify")
        self.assertEqual(data["rag"]["proposal"]["requested_fields"], ["expense_date"])
        self.assertNotIn("元", data["rag"]["proposal"]["question"])
        self.assertFalse(self.writer.messages)
        self.assertFalse(self.reviewer.messages)


class PolicyTaskContextTests(unittest.TestCase):
    setUp = fixtures.GraphTests.setUp
    tearDown = fixtures.GraphTests.tearDown
    build = fixtures.GraphTests.build

    def test_code_clarification_reaches_task_and_chat_without_policy_models(self):
        class Store:
            def search_for_facts(self, question, facts):
                return result(policy_profile="university", lodging_lookup={
                    "status": "insufficient_information", "reason": "lodging_inputs_required",
                    "missing_fields": ["destination", "lodging_standard"]})

        self.policy = Store()
        # The graph fixture has no policy model: any such call would fail this outcome.
        graph = self.build([], slots=[
            slot_response({"kind": "hotel"}, "policy_explain",
                          policy_query={"category": "hotel", "mode": "personal_limit"})])
        output = graph.invoke({"messages": [HumanMessage("我的住宿上限是多少？")]}, self.config)
        task = next(iter(output["tasks"].values()))
        self.assertEqual(task["status"], "needs_clarification")
        data = output["slots"]["expense"]["result"]
        self.assertEqual(data["revision"], task["revision"])
        self.assertEqual(data["rag"]["proposal"]["requested_fields"],
                         ["destination", "lodging_standard"])
        self.assertIn(data["rag"]["proposal"]["question"], output["messages"][-1].content)

    def test_targeted_retraction_preserves_origin_and_other_subject(self):
        target = {}
        def retract(messages):
            context = json.loads(messages[-1].content)
            return call("SlotSubmission", {
                "operation": "update_facts", "target_id": target["subject_id"],
                "target_task_id": target["id"], "question": "第一件住宿标准",
                "expense": {"expense_date": None},
                "policy_query": {"category": "hotel", "mode": "personal_limit"},
                "evidence": {"expense_date": {
                    "user_message": context["current_user_index"], "quote": "第一件的日期撤回"}},
            }, "retract")
        graph = self.build([], slots=[
            slot_response({"kind": "hotel", "destination": "甲地", "expense_date": "2099-01-02"},
                          "policy_explain", policy_query={"category": "hotel", "mode": "personal_limit"}),
            slot_response({"kind": "transport", "destination": "乙地"}, "policy_explain",
                          policy_query={"category": "transport", "mode": "general"}),
            retract,
        ])
        reply_data = {"purpose": "explain", "status": "insufficient_information", "sources": [],
                      "rag": {"status": "clarify", "proposal": {"question": "请补充条件。"}}}
        with patch("business_travel.graph.explain_policy", return_value=reply_data) as explain:
            first = graph.invoke({"messages": [HumanMessage("甲地2099-01-02我的住宿标准")]}, self.config)
            target.update(next(iter(first["tasks"].values())))
            second = graph.invoke({"messages": [HumanMessage("另外介绍乙地交通规则")]}, self.config)
            other_id = next(key for key in second["slots"]["subjects"] if key != target["subject_id"])
            output = graph.invoke({"messages": [HumanMessage("第一件的日期撤回")]}, self.config)
        request = explain.call_args.kwargs
        context = request["request_context"]
        self.assertEqual(context["original_request"], "甲地2099-01-02我的住宿标准")
        self.assertEqual(context["task_id"], target["id"])
        self.assertEqual(context["subject_id"], target["subject_id"])
        self.assertEqual(context["revision"], 2)
        self.assertEqual(request["facts"]["destination"], "甲地")
        self.assertIsNone(request["facts"]["expense_date"])
        self.assertEqual(context["current_changes"], [{
            "field": "expense_date", "before": "2099-01-02", "after": None, "user_message": 2}])
        self.assertEqual(output["slots"]["subjects"][other_id], second["slots"]["subjects"][other_id])

    def test_general_scope_can_replace_personal_scope_without_changing_intent(self):
        graph = self.build([], slots=[
            slot_response({"kind": "hotel", "expense_date": "2099-01-02"}, "policy_explain",
                          policy_query={"category": "hotel", "mode": "personal_limit"}),
            slot_response({"expense_date": None}, "policy_explain", update=True,
                          quotes={"expense_date": "撤回日期"},
                          policy_query={"category": "hotel", "mode": "general"}),
        ])
        reply_data = {"purpose": "explain", "status": "insufficient_information", "sources": [],
                      "rag": {"status": "clarify", "proposal": {"question": "请补充条件。"}}}
        with patch("business_travel.graph.explain_policy", return_value=reply_data) as explain:
            first = graph.invoke({"messages": [HumanMessage("2099-01-02我的住宿标准")]}, self.config)
            second = graph.invoke({"messages": [HumanMessage("撤回日期，改为介绍住宿计价单位")]}, self.config)
        before, after = next(iter(first["tasks"].values())), next(iter(second["tasks"].values()))
        self.assertEqual(after["id"], before["id"])
        self.assertEqual(after["intent"], "policy_explain")
        self.assertEqual(after["revision"], before["revision"] + 1)
        self.assertEqual(explain.call_args.kwargs["policy_query"]["mode"], "general")
        self.assertIsNone(explain.call_args.kwargs["facts"]["expense_date"])

    def test_scope_only_update_changes_revision_without_inventing_fact_changes(self):
        graph = self.build([], slots=[
            slot_response({"kind": "hotel", "expense_date": "2099-01-02"}, "policy_explain",
                          policy_query={"category": "hotel", "mode": "personal_limit"}),
            slot_response({}, "policy_explain", update=True,
                          policy_query={"category": "hotel", "mode": "general"}),
        ])
        reply_data = {"purpose": "explain", "status": "insufficient_information", "sources": [],
                      "rag": {"status": "clarify", "proposal": {"question": "请补充条件。"}}}
        with patch("business_travel.graph.explain_policy", return_value=reply_data) as explain:
            first = graph.invoke({"messages": [HumanMessage("2099-01-02我的住宿标准")]}, self.config)
            second = graph.invoke({"messages": [HumanMessage("条件保留，只介绍制度的计价单位")]}, self.config)
        self.assertIsNone(second["slot_error"])
        self.assertEqual(second["slots"]["expense"]["values"], first["slots"]["expense"]["values"])
        self.assertEqual(second["slots"]["expense"]["revision"], first["slots"]["expense"]["revision"] + 1)
        self.assertEqual(explain.call_args.kwargs["policy_query"]["mode"], "general")
        self.assertEqual(explain.call_args.kwargs["request_context"]["current_changes"], [])


if __name__ == "__main__":
    unittest.main()

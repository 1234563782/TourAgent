"""Hybrid policy workflow contracts; synthetic evidence, no network or credentials."""

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

from docx import Document
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import ValidationError

from business_travel.graph import build_assistant
from business_travel.memory import TravelMemory
from business_travel.policy_documents import parse_docx
from business_travel.policy_guard import render_policy
from business_travel.policy_rag import PolicyAnswer, explain_policy
from business_travel.tasks import _business_outcome
from test_policy_rag import chunk_row, database_doc, settings, store_with_db
from test_travel_graph import ScriptedModel, call, review_response, slot_response


def tool_response(name, args):
    return AIMessage(content="", tool_calls=[
        {"id": "synthetic", "name": name, "args": args, "type": "tool_call"}])


def source(**changes):
    return {"source": "测试制度.docx", "document_id": 7, "version": "test-v2",
            "source_sha256": "a" * 64, "chunk": "b003-0", "clause": "3.2",
            "section": "住宿", "text": "住宿费按每间每晚含税金额核对。",
            **changes}


def result(**changes):
    return {"status": "ok", "sources": [source()],
            "context_incomplete": False, **changes}


def answer(**changes):
    return {"decision": "answer", "claims": [
        {"text": "住宿费核对的是每间每晚的含税金额。",
         "citations": [{"source_id": "S1", "quote": "每间每晚含税金额"}]}],
        "question": "", "search_query": "", "requested_fields": [], "limitations": [], **changes}


class FakeModel:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.messages = []
        self.bindings = []

    def bind_tools(self, tools, **kwargs):
        self.bindings.append((tools, kwargs))
        return self

    def invoke(self, messages):
        self.messages.append(messages)
        value = next(self.responses)
        if isinstance(value, Exception):
            raise value
        return value(messages) if callable(value) else value


class HybridTests(unittest.TestCase):
    def test_tool_schema_explains_cross_field_action_constraints(self):
        parameters = convert_to_openai_tool(PolicyAnswer)["function"]["parameters"]
        properties = parameters["properties"]
        for field, action in (("claims", "answer"), ("question", "clarify"),
                              ("search_query", "retrieve")):
            description = properties[field].get("description", "")
            self.assertIn(action, description)
            self.assertIn("其他", description)
        self.assertIn("互斥", properties["decision"].get("description", ""))

    def test_mixed_actions_still_rejected_and_valid_actions_preserved(self):
        for value in (
            answer(question="还有一个问题"),
            answer(search_query="额外检索"),
            answer(decision="clarify", question="请补充适用条件"),
            answer(decision="retrieve", claims=[], question="请补充", search_query="范围"),
        ):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                PolicyAnswer.model_validate(value)
        for decision in ("clarify", "retrieve", "insufficient", "conflict"):
            value = answer(decision=decision, claims=[],
                           question="请补充适用条件" if decision == "clarify" else "",
                           search_query="适用范围及例外" if decision == "retrieve" else "")
            self.assertEqual(PolicyAnswer.model_validate(value).decision, decision)

    def run_query(self, proposals=None, reviews=None, results=None, config=None, facts=None):
        self.store = Mock()
        self.store.search.side_effect = results if results is not None else [result()]
        self.writer = FakeModel(proposals if proposals is not None else [
            tool_response("PolicyAnswer", answer())])
        self.reviewer = FakeModel(reviews if reviews is not None else [
            tool_response("PolicyAnswerReview", {"verdict": "accept"})])
        models = {"policy": self.writer, "policy_reviewer": self.reviewer}
        return explain_policy(self.store, lambda role: models[role], config or settings(),
                              "住宿费用按什么单位核对？",
                              facts=facts if facts is not None else {"kind": "hotel"})

    def test_cited_answer_reaches_renderer(self):
        data = self.run_query()
        self.assertEqual(data["rag"]["status"], "answered")
        rendered = render_policy(data)
        self.assertIn("住宿费核对的是每间每晚的含税金额。", rendered)
        self.assertIn("[来源1]", rendered)
        self.assertNotIn("[S1]", rendered)
        self.assertIn("测试制度.docx", rendered)
        self.assertIn("test-v2", rendered)
        self.assertIn("3.2", rendered)
        self.assertNotIn("S1", rendered)
        self.assertIn("不是审批", rendered)
        for model, name in ((self.writer, "PolicyAnswer"), (self.reviewer, "PolicyAnswerReview")):
            self.assertEqual(model.bindings[0][1]["tool_choice"], name)

    def test_fabricated_or_partial_citations_fail_closed(self):
        for citation in ({"source_id": "S9", "quote": "每间每晚含税金额"},
                         {"source_id": "S1", "quote": "每晚100元"},
                         {"source_id": "S1", "quote": " "}):
            with self.subTest(citation=citation):
                data = self.run_query(proposals=[tool_response("PolicyAnswer", answer(
                    claims=[{"text": "伪造结论", "citations": [citation]}]))])
                self.assertNotEqual(data["rag"]["status"], "answered")
                self.assertNotIn("伪造结论", render_policy(data))
                self.assertEqual(len(self.reviewer.messages), 0)

    def test_semantic_rejection_does_not_publish_claim(self):
        data = self.run_query(proposals=[
            tool_response("PolicyAnswer", answer()),
            tool_response("PolicyFinalAnswer", answer(decision="insufficient", claims=[]))],
            reviews=[
            tool_response("PolicyAnswerReview", {"verdict": "reject"})],
            config=replace(settings(), policy_rag_max_answer_attempts=1))
        self.assertEqual(data["rag"]["status"], "insufficient")
        self.assertNotIn("住宿费核对的是", render_policy(data))
        self.assertEqual(len(self.writer.messages), 2)
        self.assertEqual(data["rag"]["stop_reason"], "review_budget_exhausted")

    def test_invalid_quote_can_be_repaired_but_is_never_normalized_by_server(self):
        invalid = answer(claims=[{"text": "错误草稿", "citations": [
            {"source_id": "S1", "quote": "每间 每晚含税金额"}]}])
        data = self.run_query(proposals=[tool_response("PolicyAnswer", invalid),
                                        tool_response("PolicyAnswer", answer())])
        self.assertEqual(data["rag"]["status"], "answered")
        self.assertEqual(self.store.search.call_count, 1)
        self.assertEqual(len(self.reviewer.messages), 1)
        payload = json.loads(self.writer.messages[1][-1].content)
        self.assertEqual(payload["validation_feedback"]["reason"], "unverified_policy_citation")
        self.assertNotIn("错误草稿", render_policy(data))

    def test_source_reference_is_resolved_to_exact_text_before_independent_review(self):
        text = "标准：\n一类人员  可选列车商务座。\n随行人员须满足正文条件。"
        draft = answer(claims=[{"text": "一类人员可选择商务座。",
                                "citations": [{"source_id": "S1"}]}])
        data = self.run_query(proposals=[tool_response("PolicyAnswer", draft)],
                              results=[result(sources=[source(text=text)])])
        self.assertEqual(data["rag"]["status"], "answered")
        citation = data["rag"]["proposal"]["claims"][0]["citations"][0]
        self.assertEqual(citation["quote"], text)
        reviewed = json.loads(self.reviewer.messages[0][-1].content)
        self.assertEqual(reviewed["proposal"]["claims"][0]["citations"][0]["quote"], text)
        self.assertEqual(reviewed["sources"][0]["text"], text)

    def test_resolved_reference_does_not_bypass_semantic_review_or_unknown_source_check(self):
        for source_id, reviews in (
            ("S1", [tool_response("PolicyAnswerReview", {"verdict": "reject",
                                                        "issues": ["unsupported_claim"]})]),
            ("S99", []),
        ):
            draft = answer(claims=[{"text": "任意费用都可以报销。",
                                    "citations": [{"source_id": source_id}]}])
            proposals = [tool_response("PolicyAnswer", draft)]
            if source_id == "S1":
                proposals.append(tool_response("PolicyFinalAnswer", draft))
                reviews.append(tool_response("PolicyEvidenceReview", {
                    "claim_checks": [{"claim_index": 0, "supported": False}],
                    "limitations_valid": True, "scope_preserved": True,
                    "coverage_complete": True, "conflict": False}))
            data = self.run_query(proposals=proposals,
                reviews=reviews, config=replace(settings(), policy_rag_max_answer_attempts=1))
            self.assertEqual(data["rag"]["status"], "insufficient" if source_id == "S1" else "unavailable")
            self.assertEqual(len(self.reviewer.messages), 2 if source_id == "S1" else 0)
            self.assertNotIn("任意费用都可以报销", render_policy(data))

    def test_review_rejection_can_repair_to_clarification(self):
        data = self.run_query(
            proposals=[tool_response("PolicyAnswer", answer()),
                       tool_response("PolicyAnswer", answer(decision="clarify", claims=[],
                                                           question="请补充适用地区和人员档次。"))],
            reviews=[tool_response("PolicyAnswerReview", {"verdict": "reject",
                                                          "issues": ["missing_user_conditions"]}),
                     tool_response("PolicyAnswerReview", {"verdict": "accept"})])
        self.assertEqual(data["rag"]["status"], "clarify")
        self.assertEqual(self.store.search.call_count, 1)
        payload = json.loads(self.writer.messages[1][-1].content)
        self.assertEqual(payload["validation_feedback"]["issues"], ["missing_user_conditions"])

    def test_repair_budget_and_provider_failure_are_bounded(self):
        bad = answer(claims=[{"text": "错误草稿", "citations": [
            {"source_id": "S1", "quote": "无法核实的文字"}]}])
        data = self.run_query(proposals=[tool_response("PolicyAnswer", bad)] * 2)
        self.assertEqual(len(self.writer.messages), 2)
        self.assertEqual(data["rag"]["status"], "unavailable")
        data = self.run_query(proposals=[RuntimeError("private-url")])
        self.assertEqual(len(self.writer.messages), 1)
        self.assertNotIn("private-url", json.dumps(data))
        data = self.run_query(proposals=[ValueError("private-provider-error")])
        self.assertEqual(len(self.writer.messages), 1)
        self.assertNotIn("private-provider-error", json.dumps(data))

    def test_failures_preserve_safe_stage_and_code_without_publishing_drafts(self):
        bad = answer(claims=[{"text": "未核实草稿", "citations": [
            {"source_id": "S1", "quote": "不存在的引用"}]}])
        cases = [
            ({"results": [RuntimeError("private-dsn")]}, "policy_retrieve", "policy_retrieval_failed"),
            ({"proposals": [TimeoutError("private-url")]}, "policy_generate", "model_timeout"),
            ({"reviews": [TimeoutError("private-url")]}, "policy_review", "model_timeout"),
            ({"proposals": [tool_response("PolicyAnswer", bad)] * 2},
             "policy_citations", "unverified_policy_citation"),
        ]
        for options, stage, code in cases:
            with self.subTest(stage=stage), patch("business_travel.policy_rag.record_event") as event:
                data = self.run_query(**options)
                diagnostic = data["rag"]["diagnostic"]
                self.assertEqual(diagnostic["stage"], stage)
                self.assertEqual(diagnostic["failure_code"], code)
                self.assertNotIn("proposal", data["rag"])
                self.assertNotIn("private-", json.dumps(data))
                rendered = render_policy(data)
                self.assertNotIn("未核实草稿", rendered)
                self.assertNotIn(code, rendered)
                self.assertTrue(any(c.kwargs.get("policy_stage") == stage for c in event.call_args_list))
                if stage == "policy_citations":
                    self.assertIn("引用", rendered)
                elif code == "model_timeout":
                    self.assertIn("超时", rendered)
                else:
                    self.assertIn("检索", rendered)

    def test_invalid_schema_feedback_identifies_field_and_discriminates_from_citations(self):
        invalid = answer(question="不应同时回答并追问")
        data = self.run_query(proposals=[
            tool_response("PolicyAnswer", invalid), tool_response("PolicyAnswer", answer())])
        self.assertEqual(data["rag"]["status"], "answered")
        feedback = json.loads(self.writer.messages[1][-1].content)["validation_feedback"]
        self.assertEqual(feedback["reason"], "invalid_policy_tool_arguments")
        self.assertTrue(feedback["validation_errors"])
        self.assertEqual(feedback["rejected_proposal"], invalid)
        self.assertNotIn("diagnostic", data["rag"])

    def test_rejected_clarification_retains_draft_for_targeted_repair(self):
        draft = answer(decision="clarify", claims=[],
                       question="额度为123元，请补充人员档次。")
        corrected = answer(decision="clarify", claims=[], question="请补充人员档次。")
        data = self.run_query(
            proposals=[tool_response("PolicyAnswer", draft), tool_response("PolicyAnswer", corrected)],
            reviews=[tool_response("PolicyAnswerReview", {"verdict": "reject", "issues": ["unsupported_claim"]}),
                     tool_response("PolicyAnswerReview", {"verdict": "accept"})])
        feedback = json.loads(self.writer.messages[1][-1].content)["validation_feedback"]
        self.assertEqual(feedback["rejected_proposal"], draft)
        self.assertEqual(data["rag"]["status"], "clarify")
        self.assertNotIn("123", render_policy(data))
        self.assertIn("不夹带", self.reviewer.messages[0][0].content)

    def test_exact_lookup_requires_same_date_and_cited_rows(self):
        exact = {"status": "ok", "date": "2026-09-14", "row_ids": ["b003-0"],
                 "cap": "123", "unit": "person_day"}
        facts = {"kind": "hotel", "expense_date": "2026-09-14"}
        self.run_query(results=[result(lodging_lookup=exact)], facts=facts)
        payload = json.loads(self.writer.messages[0][-1].content)
        self.assertEqual(payload["lodging_lookup"], exact)
        for changes in ({"date": "2026-09-15"}, {"row_ids": ["missing"]},
                        {"row_ids": []}, {"status": "unavailable"}):
            with self.subTest(changes=changes):
                self.run_query(results=[result(lodging_lookup={**exact, **changes})], facts=facts)
                payload = json.loads(self.writer.messages[0][-1].content)
                self.assertNotIn("lodging_lookup", payload)

    def test_unknown_business_date_does_not_accept_dated_lookup(self):
        self.run_query(results=[result(lodging_lookup={
            "status": "ok", "date": "2026-09-14", "row_ids": ["b003-0"], "cap": "123"})])
        self.assertNotIn("lodging_lookup", json.loads(self.writer.messages[0][-1].content))

    def test_missing_season_date_is_passed_without_fabricating_a_cap(self):
        exact = {"status": "insufficient_information", "date": None,
                 "missing_fields": ["expense_date"], "row_ids": ["b003-0"]}
        self.run_query(results=[result(lodging_lookup=exact)])
        payload = json.loads(self.writer.messages[0][-1].content)
        self.assertEqual(payload["lodging_lookup"], exact)
        self.assertNotIn("as_of", payload)
        self.assertNotIn("expense_date", payload["facts"])

    def test_incomplete_or_oversize_context_does_not_generate(self):
        for retrieval, config in ((result(context_incomplete=True), settings()),
                                  (result(sources=[source(text="文" * 3000)]),
                                   replace(settings(), policy_context_chars=1000))):
            with self.subTest(config=config.policy_context_chars):
                data = self.run_query(results=[retrieval], config=config)
                self.assertEqual(data["rag"]["status"], "incomplete")
                self.assertIn("不完整", render_policy(data))
                self.assertEqual(len(self.writer.messages), 0)

    def test_no_version_and_service_failure_do_not_retry(self):
        for retrieval in (result(status="empty", sources=[], reason="no_published_policy"),
                          result(status="unavailable", sources=[], reason="service_failed")):
            data = self.run_query(results=[retrieval])
            self.assertNotEqual(data["rag"]["status"], "answered")
            self.assertEqual(self.store.search.call_count, 1)
            self.assertEqual(len(self.writer.messages), 0)

    def test_malformed_retrieval_does_not_crash_or_generate(self):
        for retrieval in (None, [], result(status=[]), result(sources=None),
                          result(sources="invalid"), result(sources=[None]),
                          result(sources=[source(document_id={})]),
                          result(sources=[source(version=["test-v2"])])):
            with self.subTest(retrieval=retrieval):
                data = self.run_query(results=[retrieval])
                self.assertNotEqual(data["rag"]["status"], "answered")
                self.assertEqual(len(self.writer.messages), 0)
                self.assertEqual(self.store.search.call_count, 1)

    def test_untrusted_retrieval_reason_is_not_exposed(self):
        data = self.run_query(results=[result(
            status="empty", sources=[], reason="https://private.example?key=secret")])
        self.assertNotIn("secret", json.dumps(data))
        data = self.run_query(results=[result(
            status="empty", sources=[], reason="no_published_policy")])
        self.assertIn("当前尚无已发布政策", render_policy(data))

    def test_missing_facts_produces_reviewed_question_not_retrieval(self):
        data = self.run_query(proposals=[tool_response("PolicyAnswer", answer(
            decision="clarify", claims=[], question="请问您要了解哪个职级的标准？"))])
        self.assertEqual(data["rag"]["status"], "clarify")
        self.assertIn("哪个职级", render_policy(data))
        self.assertEqual(self.store.search.call_count, 1)
        self.assertEqual(_business_outcome("consult_policy", data)[0], "needs_clarification")
        prompt = self.reviewer.messages[0][0].content
        self.assertIn("本次只审核追问", prompt)
        self.assertNotIn("answer 只有", prompt)

    def test_retrieval_review_does_not_apply_answer_criteria(self):
        data = self.run_query(proposals=[tool_response("PolicyAnswer", answer(
            decision="retrieve", claims=[], search_query="住宿税费规则")),
            tool_response("PolicyFinalAnswer", answer(decision="insufficient", claims=[]))],
            config=replace(settings(), policy_rag_max_retrievals=1))
        self.assertEqual(data["rag"]["status"], "insufficient")
        prompt = self.reviewer.messages[0][0].content
        self.assertIn("本次只审核补检索", prompt)
        self.assertNotIn("answer 只有", prompt)

    def test_reviewed_rewrite_uses_current_policy_without_synthetic_date(self):
        rewrite = tool_response("PolicyAnswer", answer(
            decision="retrieve", claims=[], search_query="住宿计价单位与税费"))
        data = self.run_query(proposals=[rewrite, tool_response("PolicyAnswer", answer())],
                              reviews=[tool_response("PolicyAnswerReview", {"verdict": "accept"})] * 2,
                              results=[result(), result()])
        self.assertEqual(data["rag"]["status"], "answered")
        self.assertEqual(self.store.search.call_count, 2)
        self.assertTrue(all(len(c.args) == 1
                            for c in self.store.search.call_args_list))
        self.assertEqual(data["rag"]["retrievals"], 2)

    def test_repeated_or_rejected_rewrite_never_searches_again(self):
        for query, verdict in (("住宿费用按什么单位核对？", "accept"), ("改变日期", "reject")):
            data = self.run_query(proposals=[tool_response("PolicyAnswer", answer(
                decision="retrieve", claims=[], search_query=query))],
                reviews=[tool_response("PolicyAnswerReview", {"verdict": verdict})])
            self.assertNotEqual(data["rag"]["status"], "answered")
            self.assertEqual(self.store.search.call_count, 1)

    def test_budget_exhaustion_stops_retrieval(self):
        proposals = [tool_response("PolicyAnswer", answer(decision="retrieve", claims=[],
                     search_query=query)) for query in ("第一种不同表述", "第二种不同表述")]
        proposals.append(tool_response("PolicyFinalAnswer", answer(decision="insufficient", claims=[])))
        data = self.run_query(proposals=proposals,
                              reviews=[tool_response("PolicyAnswerReview", {"verdict": "accept"})] * 2,
                              results=[result(), result()])
        self.assertEqual(data["rag"]["status"], "insufficient")
        self.assertEqual(self.store.search.call_count, 2)
        self.assertEqual(len(self.writer.messages), 3)
        self.assertEqual(data["rag"]["stop_reason"], "retrieval_budget_exhausted")

    def test_version_change_during_retry_is_not_mixed(self):
        data = self.run_query(proposals=[tool_response("PolicyAnswer", answer(
            decision="retrieve", claims=[], search_query="住宿税费规定"))],
            results=[result(), result(sources=[source(document_id=8, version="test-v3")])])
        self.assertEqual(data["rag"]["status"], "version_changed")
        self.assertEqual(len(self.writer.messages), 1)

    def test_model_protocol_and_provider_errors_are_redacted(self):
        for response in (AIMessage(content='{"decision":"answer"}'),
                         AIMessage(content="", tool_calls=[]),
                         RuntimeError("https://secret.example?key=private")):
            data = self.run_query(proposals=[response])
            self.assertNotEqual(data["rag"]["status"], "answered")
            self.assertNotIn("private", json.dumps(data))
        data = self.run_query(reviews=[RuntimeError("secret-review-key")])
        self.assertNotIn("secret-review-key", json.dumps(data))
        self.assertNotEqual(data["rag"]["status"], "answered")

    def test_reference_text_is_data_and_reviewer_sees_original_question(self):
        data = self.run_query()
        payload = json.loads(self.reviewer.messages[0][-1].content)
        self.assertEqual(payload["question"], "住宿费用按什么单位核对？")
        self.assertEqual(payload["facts"], {"kind": "hotel"})
        self.assertIn("sources", payload)
        self.assertIn("proposal", payload)
        self.assertEqual(data["rag"]["status"], "answered")

    def test_raw_fallback_reports_truncation(self):
        text = render_policy({"purpose": "explain", **result(context_incomplete=True)})
        self.assertIn("不完整", text)

    def test_unversioned_sources_are_not_presented_as_current_policy(self):
        data = self.run_query(results=[result(sources=[source(document_id=None)])])
        self.assertEqual(data["rag"]["status"], "insufficient")
        self.assertEqual(len(self.writer.messages), 0)

    def test_facts_and_sources_cannot_be_replaced_by_model_arguments(self):
        data = self.run_query(proposals=[tool_response("PolicyAnswer", {
            **answer(), })])
        self.assertNotEqual(data["rag"]["status"], "answered")

    def test_multiple_tool_calls_rejected(self):
        response = tool_response("PolicyAnswer", answer())
        response.tool_calls.append({**response.tool_calls[0], "id": "second"})
        data = self.run_query(proposals=[response])
        self.assertNotEqual(data["rag"]["status"], "answered")

    def test_conflict_never_publishes_a_conclusion(self):
        data = self.run_query(reviews=[
            tool_response("PolicyAnswerReview", {"verdict": "conflict"})])
        self.assertEqual(data["rag"]["status"], "conflict")
        self.assertIn("冲突", render_policy(data))
        self.assertNotIn("住宿费核对的是", render_policy(data))

    def test_duplicate_sources_get_one_reference(self):
        data = self.run_query(results=[result(sources=[source(), source()])])
        self.assertEqual(len(data["sources"]), 1)
        self.assertEqual(data["rag"]["status"], "answered")

    def test_table_grid_is_not_sent_as_unbounded_extra_context(self):
        data = self.run_query(results=[result(sources=[source(table={"grid": ["secret-extra-row"]})])])
        self.assertEqual(data["rag"]["status"], "answered")
        self.assertNotIn("secret-extra-row", self.writer.messages[0][-1].content)


class LinkedContextTests(unittest.TestCase):
    def test_transitive_links_and_cycles_reach_closure(self):
        store, conn, _ = store_with_db([
            [database_doc()], [{"chunk_id": "one"}], [],
            [chunk_row("one", ["two"])], [chunk_row("two", ["one", "three"])],
            [chunk_row("three", ["two"])],
        ])
        data = store.search("制度")
        self.assertEqual([s["chunk"] for s in data["sources"]], ["one", "two", "three"])
        self.assertFalse(data["context_incomplete"])
        for call in conn.execute.call_args_list:
            if "policy_chunks" in call.args[0]:
                self.assertIn("document_id=%s", call.args[0])

    def test_missing_link_is_incomplete_not_silently_ignored(self):
        store, _, _ = store_with_db([
            [database_doc()], [{"chunk_id": "one"}], [],
            [chunk_row("one", ["missing"])], [],
        ])
        data = store.search("制度")
        self.assertEqual(data["status"], "ok")
        self.assertTrue(data["context_incomplete"])

    def test_transitive_links_respect_budget(self):
        store, _, _ = store_with_db([
            [database_doc()], [{"chunk_id": "one"}], [],
            [chunk_row("one", ["two"])], [chunk_row("two", ["three"])],
        ])
        store.settings = replace(store.settings, policy_context_limit=2)
        data = store.search("制度")
        self.assertEqual(len(data["sources"]), 2)
        self.assertTrue(data["context_incomplete"])


class GraphExplanationTests(unittest.TestCase):
    def test_two_policy_tasks_keep_separate_scope_and_full_user_evidence(self):
        original = "两趟独立出差分别去测试甲地和测试乙地，请分别解释住宿单位，不查酒店。"
        questions = ["解释测试甲地的住宿计价单位，不查酒店",
                     "解释测试乙地的住宿计价单位，不查酒店"]
        proposals = []
        for city, question in zip(("测试甲地", "测试乙地"), questions):
            proposals.append({
                "intent": "policy_explain", "question": question,
                "policy_query": {"category": "hotel", "mode": "general"},
                "expense": {"kind": "hotel", "destination": city},
                "evidence": {"destination": {"user_message": 0, "quote": city}},
            })
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = replace(settings(), storage_dir=root, legacy_memory_dir=root / "legacy")
            policy = Mock()
            policy.search.return_value = result()
            models = {
                "slots": ScriptedModel([call("SlotSubmission", {"tasks": proposals}, "two")]),
                "reviewer": FakeModel([review_response()]),
                "supervisor": FakeModel([]),
                "policy": FakeModel([tool_response("PolicyAnswer", answer()) for _ in proposals]),
                "policy_reviewer": FakeModel([
                    tool_response("PolicyAnswerReview", {"verdict": "accept"}) for _ in proposals]),
            }
            graph = build_assistant(lambda role: models[role], config,
                TravelMemory(root, root / "legacy", "synthetic-user"), policy,
                checkpointer=InMemorySaver(), provider=Mock(), information=Mock())
            output = graph.invoke({"messages": [HumanMessage(content=original)]},
                {"configurable": {"thread_id": "separate-policy"}, "recursion_limit": 30})
            self.assertEqual(len(output["tasks"]), 2)
            for role in ("policy", "policy_reviewer"):
                payloads = [json.loads(messages[-1].content) for messages in models[role].messages]
                self.assertEqual(len(payloads), 2)
                self.assertEqual({p["question"] for p in payloads}, set(questions))
                self.assertEqual(len({p["request_context"]["task_id"] for p in payloads}), 2)
                for payload in payloads:
                    context = payload["request_context"]
                    task = output["tasks"][context["task_id"]]
                    subject = output["slots"]["subjects"][task["subject_id"]]
                    self.assertEqual(task["status"], "completed")
                    self.assertEqual(context["scoped_request"], task["question"])
                    self.assertEqual(context["original_request"], original)
                    self.assertEqual(payload["user_request"], original)
                    self.assertEqual(payload["facts"], subject["values"])
                    self.assertIn(payload["facts"]["destination"], payload["question"])
            policy.check.assert_not_called()

    def test_multiturn_clarification_revision_and_date_recovery(self):
        """Scripted language decisions exercise the real checkpoint/task/RAG boundary."""
        text = "测试甲地普通员工住宿每间每晚310元。测试乙地普通员工住宿每间每晚370元。"
        evidence = source(text=text)
        def cited_cap(amount):
            return answer(claims=[{"text": f"住宿上限为每间每晚{amount}元。",
                "citations": [{"source_id": "S1", "quote": f"每间每晚{amount}元"}]}])

        turns = [
            ("2026-09-14的住宿限额是多少？", {"kind": "hotel", "expense_date": "2026-09-14"},
             "请解释住宿限额", "needs_clarification"),
            ("目的地是测试甲地。", {"destination": "测试甲地"},
             "请解释测试甲地的住宿限额", "needs_clarification"),
            ("我是普通员工。", {"employee_grade": "普通员工"},
             "请解释测试甲地普通员工的住宿限额", "completed"),
            ("改为测试乙地。", {"destination": "测试乙地"},
             "请解释测试乙地普通员工的住宿限额", "completed"),
            ("日期改为2026-09-01。", {"expense_date": "2026-09-01"},
             "请解释2026-09-01测试乙地普通员工的住宿限额", "partial"),
            ("日期改回2026-09-14。", {"expense_date": "2026-09-14"},
             "请解释2026-09-14测试乙地普通员工的住宿限额", "completed"),
        ]
        submissions = []
        for index, (_, values, question, _) in enumerate(turns):
            response = slot_response(values, "policy_explain", update=index > 0)
            def submit(messages, response=response, question=question):
                message = response(messages)
                message.tool_calls[0]["args"]["tasks"][0]["question"] = question
                return message
            submissions.append(submit)
        proposals = [
            answer(decision="clarify", claims=[], question="请补充城市和职级。"),
            answer(decision="clarify", claims=[], question="请补充职级。"),
            cited_cap(310), cited_cap(370), cited_cap(370),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = replace(settings(), storage_dir=root, legacy_memory_dir=root / "legacy")
            policy = Mock()
            policy.search.side_effect = [
                result(sources=[evidence]), result(sources=[evidence]),
                result(sources=[evidence]), result(sources=[evidence]),
                result(status="empty", sources=[], reason="no_published_policy"),
                result(sources=[evidence]),
            ]
            models = {
                "slots": ScriptedModel(submissions),
                "reviewer": FakeModel([review_response()] * len(turns)),
                "supervisor": FakeModel([]),
                "policy": FakeModel([tool_response("PolicyAnswer", p) for p in proposals]),
                "policy_reviewer": FakeModel([
                    tool_response("PolicyAnswerReview", {"verdict": "accept"})] * len(proposals)),
            }
            graph = build_assistant(lambda role: models[role], config,
                TravelMemory(root, root / "legacy", "synthetic-user"), policy,
                checkpointer=InMemorySaver(), provider=Mock(), information=Mock())
            thread = {"configurable": {"thread_id": "dialogue"}, "recursion_limit": 30}
            original_id = None
            for index, (question, _, task_question, status) in enumerate(turns):
                with self.subTest(turn=index + 1):
                    output = graph.invoke({"messages": [HumanMessage(content=question)]}, thread)
                    self.assertEqual(len(output["tasks"]), 1)
                    task = next(iter(output["tasks"].values()))
                    original_id = original_id or task["id"]
                    self.assertEqual(task["id"], original_id)
                    self.assertEqual(task["revision"], index + 1)
                    self.assertEqual(task["attempt"], index + 1)
                    self.assertEqual(task["status"], status)
                    self.assertEqual(task["question"], task_question)
                    subject = output["slots"]["subjects"][task["subject_id"]]
                    self.assertEqual(task["result_ref"]["revision"], subject["revision"])
                    self.assertEqual(subject["result"]["revision"], subject["revision"])
                    self.assertEqual(policy.search.call_args.args[0], task["canonical_question"])
                    if index >= 2:
                        self.assertEqual(subject["values"]["employee_grade"], "普通员工")
                    if index >= 3:
                        self.assertEqual(subject["values"]["destination"], "测试乙地")
                    if index == 4:
                        self.assertIn("当前尚无已发布政策", output["messages"][-1].content)
                        self.assertNotIn("370", output["messages"][-1].content)
                    if index == 5:
                        self.assertIn("370", output["messages"][-1].content)
            self.assertEqual(policy.search.call_count, len(turns))
            self.assertTrue(all(len(c.args) == 1 for c in policy.search.call_args_list))
            payload = json.loads(models["policy"].messages[-1][-1].content)
            self.assertEqual(payload["user_request"], turns[-1][0])
            self.assertEqual(payload["question"], output["tasks"][original_id]["canonical_question"])
            self.assertEqual(payload["facts"]["destination"], "测试乙地")
            self.assertEqual(payload["facts"]["employee_grade"], "普通员工")
            self.assertEqual(payload["request_context"]["original_request"], turns[0][0])
            self.assertEqual(payload["request_context"]["task_id"], original_id)
            self.assertEqual(payload["request_context"]["scoped_request"],
                             output["tasks"][original_id]["canonical_question"])
            self.assertEqual(payload["request_context"]["revision"], len(turns))
            self.assertEqual(payload["request_context"]["current_changes"], [{
                "field": "expense_date", "before": "2026-09-01", "after": "2026-09-14",
                "user_message": len(turns) - 1}])
            for role in ("slots", "reviewer"):
                completed_context = next(
                    payload for messages in models[role].messages
                    if (payload := json.loads(messages[-1].content))["current_user_index"] == 3)
                self.assertEqual(completed_context["pending_tasks"], [])
                self.assertEqual(completed_context["task_context"][0]["id"], original_id)
                if role == "slots":
                    self.assertEqual(completed_context["task_context"][0]["status"], "completed")
                else:
                    self.assertNotIn("status", completed_context["task_context"][0])
                    self.assertEqual(completed_context["mechanical_validation"], "passed")
                self.assertEqual(completed_context["task_context"][0]["intent"], "policy_explain")
            policy.check.assert_not_called()

    def run_graph(self, *, clarify=False, proposal=None, expected_retrievals=1):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = replace(settings(), storage_dir=root, legacy_memory_dir=root / "legacy")
            memory = TravelMemory(root, root / "legacy", "synthetic-user")
            policy = Mock()
            policy.search.return_value = result()
            proposal = proposal or (answer(decision="clarify", claims=[], question="请问是哪个职级？")
                                    if clarify else answer())
            models = {
                "slots": ScriptedModel([slot_response({"kind": "hotel", "expense_date": "2026-09-14"},
                                                  "policy_explain")]),
                "reviewer": FakeModel([review_response()]),
                "supervisor": FakeModel([]),
                "policy": FakeModel([tool_response("PolicyAnswer", proposal)] * expected_retrievals),
                "policy_reviewer": FakeModel([
                    tool_response("PolicyAnswerReview", {"verdict": "accept"})] * expected_retrievals),
            }
            graph = build_assistant(lambda role: models[role], config, memory, policy,
                                    checkpointer=InMemorySaver(), provider=Mock(), information=Mock())
            output = graph.invoke(
                {"messages": [HumanMessage(content="请解释2026-09-14的住宿费用计价标准")]},
                {"configurable": {"thread_id": "rag-test"}, "recursion_limit": 30})
            policy.check.assert_not_called()
            self.assertEqual(policy.search.call_count, expected_retrievals)
            self.assertEqual(len(models["policy_reviewer"].messages), expected_retrievals)
            self.assertEqual(len(policy.search.call_args.args), 1)
            payload = json.loads(models["policy"].messages[0][-1].content)
            self.assertEqual(payload["facts"]["expense_date"], "2026-09-14")
            self.assertNotIn("as_of", payload)
            return output

    def test_graph_final_answer_is_reviewed_explanation(self):
        output = self.run_graph()
        text = output["messages"][-1].content
        self.assertIn("住宿费核对的是", text)
        self.assertIn("[来源1]", text)
        self.assertNotIn("[S1]", text)
        self.assertNotIn("以下为检索到的制度原文", text)
        task = next(iter(output["tasks"].values()))
        self.assertEqual(task["status"], "completed")
        tool_data = json.loads(next(m.content for m in output["messages"]
                                   if isinstance(m, ToolMessage) and m.name == "consult_policy"))
        self.assertEqual(tool_data["rag"]["status"], "answered")
        self.assertEqual(tool_data["answer"], text)

    def test_graph_clarification_is_not_marked_complete(self):
        output = self.run_graph(clarify=True)
        self.assertIn("哪个职级", output["messages"][-1].content)
        task = next(iter(output["tasks"].values()))
        self.assertEqual(task["status"], "needs_clarification")

    def test_graph_partial_answer_preserves_claims_and_unresolved_work(self):
        proposal = answer(decision="partial", limitations=[{
            "topic": "具体限额", "reason": "missing_evidence",
            "detail": "目前仅找到计价单位依据，尚不能确定具体限额。",
            "next_step": "请向财务核实具体限额。",
        }])
        output = self.run_graph(proposal=proposal, expected_retrievals=2)
        task = next(iter(output["tasks"].values()))
        self.assertEqual(task["status"], "partial")
        text = output["messages"][-1].content
        self.assertIn("住宿费核对的是", text)
        self.assertIn("还需核实", text)
        self.assertIn("具体限额", text)


class BudgetSettingsTests(unittest.TestCase):
    def test_budget_values_have_explicit_environment_validation(self):
        import os
        from business_travel.settings import Settings

        for key, values in (("TRAVEL_POLICY_RAG_MAX_RETRIEVALS", ["0", "4", "bad"]),
                            ("TRAVEL_POLICY_RAG_MAX_ANSWER_ATTEMPTS", ["0", "4", "bad"]),
                            ("TRAVEL_POLICY_CONTEXT_CHARS", ["999", "100001", "bad"])):
            for value in values:
                with self.subTest(key=key, value=value), patch.dict(
                        os.environ, {"TRAVEL_LOAD_DOTENV": "0", key: value}, clear=True):
                    with self.assertRaises(ValueError):
                        Settings.from_env()
        with patch.dict(os.environ, {"TRAVEL_LOAD_DOTENV": "0",
                                    "TRAVEL_POLICY_RAG_MAX_RETRIEVALS": "1",
                                    "TRAVEL_POLICY_RAG_MAX_ANSWER_ATTEMPTS": "3",
                                    "TRAVEL_POLICY_CONTEXT_CHARS": "5000"}, clear=True):
            config = Settings.from_env()
            self.assertEqual(config.policy_rag_max_retrievals, 1)
            self.assertEqual(config.policy_rag_max_answer_attempts, 3)
            self.assertEqual(config.policy_context_chars, 5000)


class ChunkBoundaryTests(unittest.TestCase):
    def parse(self, paragraphs):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.docx"
            doc = Document()
            for text in paragraphs:
                doc.add_paragraph(text)
            doc.save(path)
            return parse_docx(path, policy_key="travel", version="new",
                              effective_from=date(2026, 9, 14), chunk_size=200)

    def test_sentence_boundaries_preserve_all_characters(self):
        text = "每次需要完整保留的政策句子。" * 30
        chunks = self.parse([text]).chunks
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(c.text.endswith("。") for c in chunks))
        self.assertEqual("".join(c.text for c in chunks), text)
        self.assertTrue(all(len(c.text) <= 200 for c in chunks))

    def test_unnumbered_parent_paragraph_siblings_are_linked(self):
        chunks = self.parse(["无编号长段落。" * 90]).chunks
        self.assertTrue(all(set(c.related_ids) == {x.id for x in chunks if x.id != c.id}
                            for c in chunks))

    def test_chinese_article_and_nested_numbering(self):
        for text, expected in (("第十二条 住宿要求", "第十二条"), ("3.2.1 特殊情况", "3.2.1")):
            chunks = self.parse([text, "这段补充属于同一个条款。"]).chunks
            self.assertEqual([c.clause for c in chunks], [expected, expected])
            self.assertIn(chunks[1].id, chunks[0].related_ids)

    def test_huge_sentence_hard_limit_no_loss(self):
        text = "长" * 655
        chunks = self.parse([text]).chunks
        self.assertEqual("".join(c.text for c in chunks), text)
        self.assertTrue(all(len(c.text) <= 200 for c in chunks))

    def test_reused_clause_number_does_not_link_other_section(self):
        chunks = self.parse(["第一章 国内", "1.1 国内要求", "第二章 境外", "1.1 境外要求"]).chunks
        domestic = next(c for c in chunks if c.text == "1.1 国内要求")
        abroad = next(c for c in chunks if c.text == "1.1 境外要求")
        self.assertNotIn(abroad.id, domestic.related_ids)


if __name__ == "__main__":
    unittest.main()

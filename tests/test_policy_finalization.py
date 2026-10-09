"""Bounded policy finalization with synthetic sources, never live credentials."""

import json
import unittest
from dataclasses import replace
from unittest.mock import Mock

from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import ValidationError

from business_travel.policy_guard import render_policy
from business_travel.policy_rag import PolicyAnswer, PolicyFinalAnswer, explain_policy
from business_travel.observability import safe_metadata
from business_travel.tasks import _business_outcome
from test_policy_hybrid import FakeModel, answer, result, source, tool_response
from test_policy_rag import settings


def retrieve(query):
    return tool_response("PolicyAnswer", answer(decision="retrieve", claims=[], search_query=query))


def partial():
    return answer(decision="partial", claims=[{
        "text": "测试地住宿附表的甲档为610元/人·天，乙档为390元/人·天。",
        "citations": [{"source_id": "S1"}],
    }], limitations=[{
        "topic": "个人住宿档次",
        "reason": "applicability_unknown",
        "detail": "当前资料不能确定您的身份对应哪一档，以上不是您的个人限额。",
        "next_step": "请向学校财务核实住宿人员档次。",
    }])


def accept_review(messages):
    payload = json.loads(messages[-1].content)
    proposal = payload["proposal"]
    if payload["finalize_only"] and proposal["decision"] in {"answer", "partial"}:
        return terminal_review(proposal=proposal)
    return tool_response("PolicyAnswerReview", {"verdict": "accept"})


def terminal_review(*, proposal=None, supported=True, conflict=False):
    return tool_response("PolicyEvidenceReview", {
        "claim_checks": [{"claim_index": index, "supported": supported}
                         for index in range(len((proposal or partial())["claims"]))],
        "limitations_valid": True, "scope_preserved": True,
        "coverage_complete": (proposal or partial())["decision"] == "answer",
        "conflict": conflict,
    })


class FinalizationTests(unittest.TestCase):
    def run_policy(self, proposals, *, results=None, reviews=None, config=None, **kwargs):
        self.store = Mock()
        self.store.search.side_effect = results or [result(sources=[source(
            text="测试地住宿费：甲档610元/人·天；乙档390元/人·天。")])]
        self.writer = FakeModel(proposals)
        self.reviewer = FakeModel(reviews if reviews is not None else [accept_review] * 8)
        models = {"policy": self.writer, "policy_reviewer": self.reviewer}
        return explain_policy(self.store, lambda role: models[role],
            config or replace(settings(), policy_rag_max_retrievals=1),
            "我的出差标准是什么？", **kwargs)

    def test_exhaustion_finalizes_partial_answer_without_more_search(self):
        data = self.run_policy([retrieve("补找住宿档次映射"),
                               tool_response("PolicyFinalAnswer", partial())])
        self.assertEqual(data["rag"]["status"], "partial")
        self.assertEqual(data["rag"]["stop_reason"], "retrieval_budget_exhausted")
        self.assertEqual(self.store.search.call_count, 1)
        self.assertEqual(_business_outcome("consult_policy", data)[0], "partial")
        self.assertEqual(len(self.reviewer.messages), 2)
        text = render_policy(data)
        self.assertIn("610元/人·天", text)
        self.assertIn("学校财务", text)
        self.assertNotIn("现有政策证据不足", text)
        payload = json.loads(self.writer.messages[-1][-1].content)
        self.assertEqual(payload["retrievals_remaining"], 0)
        self.assertTrue(payload["finalize_only"])
        self.assertEqual(payload["question"], "我的出差标准是什么？")

    def test_partial_answer_is_supported_before_budget_exhaustion(self):
        data = self.run_policy([tool_response("PolicyAnswer", partial())])
        self.assertEqual(data["rag"]["status"], "partial")
        self.assertEqual(len(self.writer.messages), 1)
        self.assertIn("个人住宿档次", render_policy(data))

    def test_missing_evidence_uses_remaining_retrieval_before_partial_return(self):
        draft = partial()
        draft["limitations"][0].update(
            topic="测试地住宿标准附表", reason="missing_evidence",
            detail="当前来源缺少住宿标准附表。")
        data = self.run_policy([
            tool_response("PolicyAnswer", draft),
            tool_response("PolicyAnswer", partial()),
        ], results=[result(), result(sources=[source(chunk="table",
            text="测试地住宿费：甲档610元/人·天；乙档390元/人·天。")])],
            config=replace(settings(), policy_rag_max_retrievals=2))
        self.assertEqual(self.store.search.call_count, 2)
        self.assertEqual(self.store.search.call_args.args[0], "测试地住宿标准附表")
        self.assertEqual(data["rag"]["status"], "partial")
        self.assertEqual(len(data["sources"]), 2)

    def test_same_version_evidence_survives_followup_retrieval(self):
        data = self.run_policy([
            retrieve("补找税费条件"),
            retrieve("再次补找映射"),
            tool_response("PolicyFinalAnswer", partial()),
        ], results=[
            result(sources=[source(text="测试地住宿费：甲档610元/人·天；乙档390元/人·天。")]),
            result(sources=[source(chunk="other", text="报销须提供合法票据。")]),
        ], config=replace(settings(), policy_rag_max_retrievals=2))
        self.assertEqual(data["rag"]["status"], "partial")
        self.assertEqual([s["source_id"] for s in data["sources"]], ["S1", "S2"])
        self.assertIn("甲档610", data["sources"][0]["text"])
        self.assertEqual(self.store.search.call_count, 2)

    def test_duplicate_query_also_finalizes_without_another_search(self):
        data = self.run_policy([retrieve("我的出差标准是什么？"),
                               tool_response("PolicyFinalAnswer", partial())],
            config=replace(settings(), policy_rag_max_retrievals=2))
        self.assertEqual(data["rag"]["status"], "partial")
        self.assertEqual(data["rag"]["stop_reason"], "repeated_query")
        self.assertEqual(self.store.search.call_count, 1)

    def test_partial_fabricated_citation_never_reaches_user(self):
        draft = partial()
        draft["claims"][0]["citations"] = [{"source_id": "S404"}]
        data = self.run_policy([retrieve("补找映射"),
                               tool_response("PolicyFinalAnswer", draft)],
            config=replace(settings(), policy_rag_max_retrievals=1, policy_rag_max_answer_attempts=1))
        self.assertEqual(data["rag"]["status"], "unavailable")
        self.assertNotIn("610", render_policy(data))
        self.assertEqual(len(self.reviewer.messages), 1)

    def test_unknown_rank_can_list_cited_standards_but_not_mark_complete(self):
        for identity in ("模拟职称甲", "模拟职称乙"):
            with self.subTest(identity=identity):
                data = self.run_policy([tool_response("PolicyAnswer", partial())],
                    results=[result(policy_profile="university", lodging_lookup={
                        "status": "insufficient_information",
                        "missing_fields": ["lodging_standard"],
                        "reason": "lodging_inputs_required"},
                        sources=[source(text="测试地住宿费：甲档610元/人·天；乙档390元/人·天。")])],
                    facts={"kind": "hotel", "destination": "测试地", "academic_title": identity},
                    policy_query={"category": "hotel", "mode": "personal_limit"})
                self.assertEqual(data["rag"]["status"], "partial")
                self.assertEqual(_business_outcome("consult_policy", data)[0], "partial")
                self.assertIn("学校财务", render_policy(data))

    def test_final_schema_and_runtime_both_forbid_retrieval(self):
        options = convert_to_openai_tool(PolicyFinalAnswer)["function"]["parameters"]
        self.assertNotIn("retrieve", options["properties"]["decision"]["enum"])
        illegal = answer(decision="retrieve", claims=[], search_query="继续检索")
        with self.assertRaises(ValidationError):
            PolicyFinalAnswer.model_validate(illegal)
        data = self.run_policy([retrieve("补找映射"),
            tool_response("PolicyFinalAnswer", illegal),
            tool_response("PolicyFinalAnswer", partial())])
        self.assertEqual(data["rag"]["status"], "partial")
        self.assertEqual(self.store.search.call_count, 1)
        self.assertEqual(len(self.writer.messages), 3)
        self.assertIn("validation_feedback", json.loads(self.writer.messages[-1][-1].content))

    def test_unknown_mapping_is_reviewed_by_content_not_reason_label(self):
        draft = partial()
        draft["limitations"][0]["reason"] = "missing_evidence"
        for verdict, expected in (("accept", "partial"), ("reject", "insufficient")):
            with self.subTest(verdict=verdict):
                proposals = [tool_response("PolicyAnswer", draft)]
                reviews = [tool_response("PolicyAnswerReview", {
                    "verdict": verdict,
                    "issues": [] if verdict == "accept" else ["wrong_applicability"]})]
                if verdict == "reject":
                    proposals.append(tool_response("PolicyFinalAnswer", draft))
                    reviews.append(terminal_review(supported=False))
                data = self.run_policy(proposals,
                    results=[result(policy_profile="university", lodging_lookup={
                        "status": "insufficient_information",
                        "missing_fields": ["lodging_standard"]})],
                    facts={"kind": "hotel", "destination": "测试地"},
                    policy_query={"category": "hotel", "mode": "personal_limit"},
                    config=replace(settings(), policy_rag_max_answer_attempts=1),
                    reviews=reviews)
                self.assertEqual(data["rag"]["status"], expected)
                self.assertEqual(len(self.reviewer.messages), 1 if verdict == "accept" else 2)
                payload = json.loads(self.reviewer.messages[0][-1].content)
                self.assertTrue(payload["requirements"]["reference_only"])
                if verdict == "reject":
                    self.assertNotIn("610", render_policy(data))

    def test_final_generation_repair_budget_does_not_loop_forever(self):
        illegal = answer(decision="retrieve", claims=[], search_query="继续检索")
        data = self.run_policy([retrieve("补找映射")] + [
            tool_response("PolicyFinalAnswer", illegal)] * 2)
        self.assertEqual(data["rag"]["status"], "unavailable")
        self.assertEqual(data["rag"]["diagnostic"]["failure_code"], "invalid_policy_tool_arguments")
        self.assertEqual(len(self.writer.messages), 3)
        self.assertEqual(self.store.search.call_count, 1)

    def test_partial_contract_requires_both_claims_and_limitations(self):
        for draft in (partial() | {"claims": []}, partial() | {"limitations": []},
                      partial() | {"decision": "answer"},
                      partial() | {"question": "另外请问"},
                      partial() | {"search_query": "额外检索"},
                      partial() | {"limitations": [partial()["limitations"][0] | {"detail": " "}]}):
            with self.subTest(draft=draft), self.assertRaises(ValidationError):
                PolicyAnswer.model_validate(draft)

    def test_final_partial_is_reviewed_and_rejected_drafts_never_render(self):
        for verdict in ("reject", "conflict"):
            with self.subTest(verdict=verdict):
                data = self.run_policy([retrieve("补找映射"),
                    tool_response("PolicyFinalAnswer", partial())],
                    reviews=[tool_response("PolicyAnswerReview", {"verdict": "accept"}),
                             terminal_review(supported=False, conflict=verdict == "conflict")],
                    config=replace(settings(), policy_rag_max_retrievals=1,
                                   policy_rag_max_answer_attempts=1))
                self.assertEqual(data["rag"]["status"], "conflict" if verdict == "conflict" else "insufficient")
                self.assertNotIn("610", render_policy(data))
                self.assertNotIn("proposal", data["rag"])
                self.assertEqual(len(self.reviewer.messages), 2)

    def test_reference_only_cannot_become_complete_personal_answer(self):
        data = self.run_policy([tool_response("PolicyAnswer", answer())],
            results=[result(policy_profile="university", lodging_lookup={
                "status": "insufficient_information", "missing_fields": ["lodging_standard"]})],
            facts={"kind": "hotel", "destination": "测试地"},
            policy_query={"category": "hotel", "mode": "personal_limit"},
            config=replace(settings(), policy_rag_max_answer_attempts=1))
        self.assertEqual(data["rag"]["status"], "unavailable")
        self.assertEqual(data["rag"]["diagnostic"]["failure_code"], "personal_applicability_unresolved")
        self.assertFalse(self.reviewer.messages)

    def test_final_can_return_complete_answer_or_no_evidence(self):
        for draft, expected in ((answer(), "answered"),
                                (answer(decision="insufficient", claims=[]), "insufficient")):
            with self.subTest(expected=expected):
                data = self.run_policy([retrieve("补找条件"),
                    tool_response("PolicyFinalAnswer", draft)], results=[result()])
                self.assertEqual(data["rag"]["status"], expected)
                self.assertEqual(self.store.search.call_count, 1)
        data = self.run_policy([], results=[result(status="empty", sources=[])])
        self.assertEqual(data["rag"]["status"], "insufficient")
        self.assertFalse(self.writer.messages)

    def test_final_provider_failure_remains_distinct_from_budget_stop(self):
        data = self.run_policy([retrieve("补找映射"), TimeoutError("private-provider")])
        self.assertEqual(data["rag"]["status"], "unavailable")
        self.assertEqual(data["rag"]["stop_reason"], "retrieval_budget_exhausted")
        self.assertEqual(data["rag"]["diagnostic"]["failure_code"], "model_timeout")
        self.assertNotIn("private-provider", json.dumps(data))
        self.assertIn("超时", render_policy(data))
        self.assertEqual(len(self.writer.messages), 2)

    def test_merged_evidence_stays_bounded_and_never_crosses_versions(self):
        for second, config, expected in (
            (result(sources=[source(chunk="new")]),
             replace(settings(), policy_context_limit=1), "incomplete"),
            (result(sources=[source(version="other")]), settings(), "version_changed"),
        ):
            with self.subTest(expected=expected):
                data = self.run_policy([retrieve("补找条件")],
                    results=[result(), second], config=config)
                self.assertEqual(data["rag"]["status"], expected)
                self.assertEqual(len(self.writer.messages), 1)

    def test_terminal_reason_is_safe_observability_metadata(self):
        for reason in ("repeated_query", "retrieval_budget_exhausted", "review_budget_exhausted"):
            data = {"status": "partial", "policy_stop_reason": reason}
            self.assertEqual(safe_metadata(data), data)
        self.assertNotIn("policy_stop_reason",
                         safe_metadata({"policy_stop_reason": "private text"}))


if __name__ == "__main__":
    unittest.main()

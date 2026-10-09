"""Semantic review exhaustion must not discard safely answerable policy evidence."""

import json
import unittest
from dataclasses import replace

from pydantic import ValidationError

from business_travel.policy_guard import render_policy
from business_travel.policy_rag import PolicyEvidenceReview
import test_policy_finalization as finalization
from test_policy_finalization import partial
from test_policy_hybrid import answer, result, source, tool_response
from test_policy_rag import settings


def evidence_review(**changes):
    return tool_response("PolicyEvidenceReview", {
        "claim_checks": [{"claim_index": 0, "supported": True}],
        "limitations_valid": True,
        "scope_preserved": True,
        "coverage_complete": False,
        "conflict": False,
        **changes,
    })


class ReviewExhaustionTests(unittest.TestCase):
    run_policy = finalization.FinalizationTests.run_policy

    def test_rejected_partial_enters_finalization_and_preserves_verified_answer(self):
        data = self.run_policy([
            tool_response("PolicyAnswer", partial()),
            tool_response("PolicyAnswer", partial()),
            tool_response("PolicyFinalAnswer", partial()),
        ], reviews=[
            tool_response("PolicyAnswerReview", {
                "verdict": "reject", "issues": ["incomplete_answer"]}),
            tool_response("PolicyAnswerReview", {
                "verdict": "reject", "issues": ["incomplete_answer"]}),
            evidence_review(),
        ])
        self.assertEqual(data["rag"]["status"], "partial")
        self.assertEqual(data["rag"]["stop_reason"], "review_budget_exhausted")
        self.assertIn("610元/人·天", render_policy(data))
        self.assertEqual(self.store.search.call_count, 1)
        self.assertEqual(len(self.writer.messages), 3)
        self.assertEqual(len(self.reviewer.messages), 3)
        payload = json.loads(self.writer.messages[-1][-1].content)
        self.assertTrue(payload["finalize_only"])
        self.assertEqual(payload["retrievals_remaining"], 0)
        self.assertEqual(payload["validation_feedback"]["issues"], ["incomplete_answer"])

    def test_final_rejection_is_bounded_and_never_publishes_unsafe_claim(self):
        for change in (
            {"claim_checks": [{"claim_index": 0, "supported": False}]},
            {"limitations_valid": False},
            {"scope_preserved": False},
        ):
            with self.subTest(change=change):
                data = self.run_policy([
                    tool_response("PolicyAnswer", partial()),
                    tool_response("PolicyFinalAnswer", partial()),
                ], reviews=[
                    tool_response("PolicyAnswerReview", {
                        "verdict": "reject", "issues": ["unsupported_claim"]}),
                    evidence_review(**change),
                ], config=replace(settings(), policy_rag_max_answer_attempts=1))
                self.assertEqual(data["rag"]["status"], "insufficient")
                self.assertNotIn("610", render_policy(data))
                self.assertNotIn("proposal", data["rag"])
                self.assertEqual(len(self.writer.messages), 2)
                self.assertEqual(self.store.search.call_count, 1)

    def test_incomplete_full_answer_is_not_accepted(self):
        data = self.run_policy([
            tool_response("PolicyAnswer", partial()),
            tool_response("PolicyFinalAnswer", partial() | {"decision": "answer", "limitations": []}),
        ], reviews=[
            tool_response("PolicyAnswerReview", {"verdict": "reject", "issues": ["incomplete_answer"]}),
            evidence_review(),
        ], config=replace(settings(), policy_rag_max_answer_attempts=1))
        self.assertEqual(data["rag"]["status"], "insufficient")
        self.assertNotIn("610", render_policy(data))

    def test_evidence_review_must_cover_every_claim_exactly_once(self):
        for checks in ([], [{"claim_index": 1, "supported": True}],
                       [{"claim_index": 0, "supported": True}] * 2):
            with self.subTest(checks=checks):
                data = self.run_policy([
                    tool_response("PolicyAnswer", partial()),
                    tool_response("PolicyFinalAnswer", partial()),
                ], reviews=[
                    tool_response("PolicyAnswerReview", {"verdict": "reject"}),
                    evidence_review(claim_checks=checks),
                ], config=replace(settings(), policy_rag_max_answer_attempts=1))
                self.assertEqual(data["rag"]["status"], "unavailable")
                self.assertNotIn("610", render_policy(data))
                self.assertEqual(data["rag"]["diagnostic"]["failure_code"],
                                 "invalid_policy_evidence_review" if checks
                                 else "invalid_policy_tool_arguments")

    def test_rejected_claim_feedback_can_be_repaired_within_final_budget(self):
        data = self.run_policy([
            tool_response("PolicyAnswer", partial()),
            tool_response("PolicyAnswer", partial()),
            tool_response("PolicyFinalAnswer", partial()),
            tool_response("PolicyFinalAnswer", partial()),
        ], reviews=[
            tool_response("PolicyAnswerReview", {"verdict": "reject"})] * 2 + [
            evidence_review(claim_checks=[{"claim_index": 0, "supported": False}]),
            evidence_review(),
        ])
        self.assertEqual(data["rag"]["status"], "partial")
        feedback = json.loads(self.writer.messages[-1][-1].content)["validation_feedback"]
        self.assertEqual(feedback["unsupported_claim_indices"], [0])
        self.assertEqual(len(self.writer.messages), 4)
        self.assertEqual(len(self.reviewer.messages), 4)

    def test_finalization_can_ask_missing_conditions_without_claims(self):
        data = self.run_policy([
            tool_response("PolicyAnswer", partial()),
            tool_response("PolicyFinalAnswer", answer(
                decision="clarify", claims=[], question="请提供目的地。", requested_fields=["destination"])),
        ], reviews=[
            tool_response("PolicyAnswerReview", {"verdict": "reject"}),
            tool_response("PolicyAnswerReview", {"verdict": "accept"}),
        ], config=replace(settings(), policy_rag_max_answer_attempts=1))
        self.assertEqual(data["rag"]["status"], "clarify")
        self.assertIn("目的地", render_policy(data))

    def test_two_retrieval_trace_reaches_terminal_evidence_review(self):
        data = self.run_policy([
            tool_response("PolicyAnswer", partial()),
            finalization.retrieve("补找其他差旅标准"),
            tool_response("PolicyAnswer", partial()),
            tool_response("PolicyAnswer", partial()),
            tool_response("PolicyFinalAnswer", partial()),
        ], results=[
            result(sources=[source(text="测试地甲档610元/人·天；乙档390元/人·天。")]),
            result(sources=[source(chunk="second", text="应当提供合法票据。")]),
        ], reviews=[
            tool_response("PolicyAnswerReview", {"verdict": "reject", "issues": ["incomplete_answer"]}),
            tool_response("PolicyAnswerReview", {"verdict": "accept"}),
            tool_response("PolicyAnswerReview", {"verdict": "reject", "issues": ["incomplete_answer"]}),
            tool_response("PolicyAnswerReview", {"verdict": "reject", "issues": ["incomplete_answer"]}),
            evidence_review(),
        ], config=replace(settings(), policy_rag_max_retrievals=2))
        self.assertEqual(data["rag"]["status"], "partial")
        self.assertEqual(data["rag"]["stop_reason"], "review_budget_exhausted")
        self.assertEqual(self.store.search.call_count, 2)
        self.assertEqual(len(self.writer.messages), 5)
        self.assertEqual(len(self.reviewer.messages), 5)
        payload = json.loads(self.reviewer.messages[-1][-1].content)
        self.assertEqual([item["source_id"] for item in payload["sources"]], ["S1", "S2"])
        self.assertEqual(self.reviewer.bindings[-1][1]["tool_choice"], "PolicyEvidenceReview")

    def test_final_semantic_rejection_cannot_restart_finalization(self):
        for attempts in (1, 2, 3):
            with self.subTest(attempts=attempts):
                data = self.run_policy(
                    [tool_response("PolicyAnswer", partial())] * attempts
                    + [tool_response("PolicyFinalAnswer", partial())] * attempts,
                    reviews=[tool_response("PolicyAnswerReview", {
                        "verdict": "reject", "issues": ["incomplete_answer"]})] * attempts
                    + [evidence_review(limitations_valid=False)] * attempts,
                    config=replace(settings(), policy_rag_max_answer_attempts=attempts))
                self.assertEqual(data["rag"]["status"], "insufficient")
                self.assertEqual(len(self.writer.messages), 2 * attempts)
                self.assertEqual(len(self.reviewer.messages), 2 * attempts)
                self.assertEqual(self.store.search.call_count, 1)
                self.assertNotIn("610", render_policy(data))

    def test_terminal_provider_error_is_not_retried_or_published(self):
        data = self.run_policy([
            tool_response("PolicyAnswer", partial()), TimeoutError("private-provider")],
            reviews=[tool_response("PolicyAnswerReview", {"verdict": "reject"})],
            config=replace(settings(), policy_rag_max_answer_attempts=1))
        self.assertEqual(data["rag"]["status"], "unavailable")
        self.assertEqual(data["rag"]["stop_reason"], "review_budget_exhausted")
        self.assertEqual(data["rag"]["diagnostic"]["failure_code"], "model_timeout")
        self.assertEqual(len(self.writer.messages), 2)
        self.assertNotIn("private-provider", json.dumps(data))
        self.assertNotIn("610", render_policy(data))

    def test_terminal_citation_error_cannot_bypass_local_validation(self):
        invalid = partial()
        invalid["claims"][0]["citations"] = [{"source_id": "S404"}]
        data = self.run_policy([
            tool_response("PolicyAnswer", partial()),
            tool_response("PolicyFinalAnswer", invalid)],
            reviews=[tool_response("PolicyAnswerReview", {"verdict": "reject"})],
            config=replace(settings(), policy_rag_max_answer_attempts=1))
        self.assertEqual(data["rag"]["diagnostic"]["failure_code"], "unverified_policy_citation")
        self.assertEqual(len(self.reviewer.messages), 1)
        self.assertNotIn("610", render_policy(data))

    def test_evidence_review_requires_explicit_typed_safety_decisions(self):
        valid = evidence_review().tool_calls[0]["args"]
        for field in ("limitations_valid", "scope_preserved", "coverage_complete", "conflict"):
            with self.subTest(field=field):
                missing = {key: value for key, value in valid.items() if key != field}
                with self.assertRaises(ValidationError):
                    PolicyEvidenceReview.model_validate(missing)
                with self.assertRaises(ValidationError):
                    PolicyEvidenceReview.model_validate(valid | {field: "true"})
        with self.assertRaises(ValidationError):
            PolicyEvidenceReview.model_validate(valid | {
                "claim_checks": [{"claim_index": False, "supported": True}]})


if __name__ == "__main__":
    unittest.main()

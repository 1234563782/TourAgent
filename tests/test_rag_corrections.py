"""Regressions for evidence, identity rules and review protocol; no external calls."""

import copy
import json
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from pydantic import ValidationError

from business_travel.policy_guard import render_policy
from business_travel.policy_package import prepare_package
from business_travel.policy_rag import PolicyAnswerReview, explain_policy
from evals.rag_retrieval_eval import _metrics, run_eval
import test_policy_hybrid as hybrid
from test_policy_hybrid import FakeModel, answer, source, tool_response
from test_policy_rag import chunk_row, database_doc, settings, store_with_db


def presentation_fixture():
    sources = [source(source_id="S1", text="一类人员可乘坐高铁商务座、一等座、二等座。", clause="第六条"),
               source(source_id="S2", chunk="lodging-r9", source="住宿费标准明细表.xls",
                      clause="", section="住宿费标准明细表", sheet="Sheet1", cell_range="B9:K9",
                      text="地区：北京；单位：元/人·天；部级：1100。")]
    return {"purpose": "explain", "sources": sources, "rag": {
        "status": "answered", "proposal": {"claims": [
            {"text": "高铁可乘商务座、一等座、二等座。", "citations": [{"source_id": "S1"}]},
            {"text": "北京部级住宿参考上限为1100元/人·天。", "citations": [{"source_id": "S2"}]},
        ]}}}


class EvidencePresentationTests(unittest.TestCase):
    def test_each_claim_has_reference_and_original_quote_in_matching_detail(self):
        data = presentation_fixture()
        original = copy.deepcopy(data)
        text = render_policy(data)
        self.assertIn("高铁可乘商务座、一等座、二等座。 [来源1]", text)
        self.assertIn("北京部级住宿参考上限为1100元/人·天。 [来源2]", text)
        for index, item in enumerate(data["sources"], 1):
            self.assertIn(f":::details 政策依据 · 来源{index}", text)
            self.assertIn(item["text"], text)
        self.assertIn("Sheet1 / B9:K9", text)
        self.assertNotIn("S1", text)
        self.assertEqual(data, original)

    def test_multiple_excerpts_from_one_source_share_one_detail(self):
        data = presentation_fixture()
        data["sources"][0]["text"] = "一类可乘商务座。二类可乘一等座。"
        data["rag"]["proposal"]["claims"] = [
            {"text": "一类可乘商务座。", "citations": [{"source_id": "S1", "quote": "一类可乘商务座。"}]},
            {"text": "二类可乘一等座。", "citations": [{"source_id": "S1", "quote": "二类可乘一等座。"}]}]
        text = render_policy(data)
        self.assertEqual(text.count(":::details 政策依据 · 来源1"), 1)
        self.assertIn("原文摘录：\n一类可乘商务座。\n\n二类可乘一等座。", text)

    def test_unverified_saved_claim_is_not_rendered_and_rule_source_is_labeled(self):
        data = presentation_fixture()
        data["rag"]["proposal"]["claims"][0]["citations"][0]["source_id"] = "missing"
        data["sources"][1]["source_type"] = "reviewed_rule"
        text = render_policy(data)
        self.assertNotIn("高铁可乘商务座、一等座、二等座。", text)
        self.assertIn("配置摘录", text)
        self.assertIn("非制度原文", text)


class ReviewProtocolTests(unittest.TestCase):
    def test_accept_with_issues_is_invalid(self):
        with self.assertRaises(ValidationError):
            PolicyAnswerReview(verdict="accept", issues=["unsupported_claim"])

    def test_contradictory_review_does_not_publish_the_answer(self):
        probe = hybrid.HybridTests()
        data = probe.run_query(reviews=[tool_response("PolicyAnswerReview", {
            "verdict": "accept", "issues": ["unsupported_claim"]})],
            config=replace(settings(), policy_rag_max_answer_attempts=1))
        self.assertNotEqual(data["rag"]["status"], "answered")

    def test_contradictory_review_can_be_repaired_within_existing_attempt_limit(self):
        probe = hybrid.HybridTests()
        data = probe.run_query(
            proposals=[tool_response("PolicyAnswer", answer())] * 2,
            reviews=[tool_response("PolicyAnswerReview", {"verdict": "accept", "issues": ["unsupported_claim"]}),
                     tool_response("PolicyAnswerReview", {"verdict": "accept"})])
        self.assertEqual(data["rag"]["status"], "answered")


class IdentityRetrievalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = prepare_package(Path(__file__).resolve().parents[1] /
                                     "policies/hfut-2026/manifest.json", company_id="audit")

    def make_store(self):
        lodging = next(c for c in self.bundle.chunks if c.id == "lodging-r9")
        row = {"chunk_id": lodging.id, "clause": lodging.clause, "section": lodging.section,
               "content": lodging.text, "table_data": lodging.table, "related_ids": []}
        store, conn, _ = store_with_db([
            [database_doc(policy_profile="university")], [{"chunk_id": "one"}], [],
            {"rules": self.bundle.rules}, [{"data": r} for r in self.bundle.lodging_rows],
            [row, chunk_row("one")]])
        self.conn = conn
        return store

    def test_transport_identity_does_not_select_a_lodging_column(self):
        facts = {"kind": "hotel", "destination": "北京", "academic_title": "院士"}
        original = copy.deepcopy(facts)
        result = self.make_store().search_for_facts("我的住宿标准是多少？", facts)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["resolved_standards"]["transport_standard"], "一类")
        self.assertIsNone(result["resolved_standards"]["lodging_standard"])
        self.assertEqual(result["lodging_lookup"]["missing_fields"], ["lodging_standard"])
        self.assertNotIn("cap", result["lodging_lookup"])
        self.assertEqual(facts, original)
        mapping = next(s for s in result["sources"] if s.get("source_type") == "reviewed_rule")
        self.assertNotIn("lodging_categories", mapping["text"])
        self.assertNotIn("部级", mapping["text"])

    def test_resolved_grade_reaches_writer_and_reviewer_with_unchanged_facts(self):
        store = self.make_store()
        facts = {"kind": "hotel", "destination": "北京", "academic_title": "院士"}
        writer = FakeModel([tool_response("PolicyAnswer", answer(
            decision="partial",
            claims=[{"text": "模拟政策", "citations": [{"source_id": "S1"}]}],
            limitations=[{
                "topic": "个人住宿档次",
                "reason": "applicability_unknown",
                "detail": "已说明院士身份，但住宿附表档次尚未明确，不能确定个人适用档次。",
                "next_step": "请补充部级、司局级或其他人员档次，或向学校财务核实。",
            }]))])
        reviewer = FakeModel([tool_response("PolicyAnswerReview", {"verdict": "accept"})])
        models = {"policy": writer, "policy_reviewer": reviewer}
        data = explain_policy(store, lambda role: models[role], settings(), "我的住宿上限是多少？",
                              facts=facts, policy_query={"category": "hotel", "mode": "personal_limit"})
        self.assertEqual(data["rag"]["status"], "partial")
        self.assertTrue(data["requirements"]["reference_only"])
        for model in (writer, reviewer):
            payload = json.loads(model.messages[0][-1].content)
            self.assertEqual(payload["facts"], facts)
            self.assertIsNone(payload["resolved_standards"].get("lodging_standard"))
            self.assertIn("lodging_standard", payload["requirements"]["missing_fields"])

    def test_identity_mapping_keeps_explicit_grades_conflicts_and_student_conditions(self):
        store = self.make_store()
        cases = [
            ({"academic_title": "院士"}, "一类", None),
            ({"transport_standard": "二类"}, "二类", None),
            ({"transport_standard": "三类"}, "三类", None),
            ({"technical_grade": 1}, "二类", None),
            ({"academic_title": "院士", "lodging_standard": "其他人员"}, "一类", "其他人员"),
            ({"academic_title": "院士", "transport_standard": "三类"}, None, None),
            ({"academic_title": "未知身份"}, None, None),
            ({"personnel_type": "student"}, None, None),
            ({"personnel_type": "student", "student_activity_eligible": False}, None, None),
            ({"personnel_type": "student", "student_activity_eligible": True}, "三类", None),
            ({"employee_grade": "院士"}, None, None),
        ]
        for facts, category, lodging in cases:
            with self.subTest(facts=facts):
                conn = Mock()
                conn.execute.return_value.fetchone.return_value = {"rules": self.bundle.rules}
                resolved, evidence = store._explanation_standards(
                    conn, database_doc(policy_profile="university"), facts)
                self.assertEqual(resolved.get("transport_standard"), category)
                self.assertEqual(resolved.get("lodging_standard"), lodging)
                self.assertEqual(bool(evidence), bool(category or lodging))

    def test_missing_destination_or_seasonal_date_still_requires_clarification(self):
        for destination, expected in [(None, "destination"), ("拉萨", "expense_date")]:
            with self.subTest(destination=destination):
                store, conn, _ = store_with_db([])
                chunks = {c.id: {"chunk_id": c.id, "clause": c.clause, "section": c.section,
                                 "content": c.text, "table_data": c.table, "related_ids": []}
                          for c in self.bundle.chunks}
                chunks["one"] = chunk_row("one")

                def execute(sql, params=None):
                    cursor = Mock()
                    if "FROM policy_documents" in sql:
                        cursor.fetchall.return_value = [database_doc(policy_profile="university")]
                    elif sql.startswith("SELECT rules"):
                        cursor.fetchone.return_value = {"rules": self.bundle.rules}
                    elif sql.startswith("SELECT data"):
                        cursor.fetchall.return_value = [{"data": r} for r in self.bundle.lodging_rows]
                    elif sql.startswith("SELECT * FROM policy_chunks"):
                        cursor.fetchall.return_value = [chunks[key] for key in params[1]]
                    elif "SELECT chunk_id FROM policy_chunks" in sql and "ORDER BY embedding::vector" in sql:
                        # The production path may use the configured HNSW query;
                        # this fixture only needs one vector candidate to exercise
                        # the missing-destination clarification contract.
                        cursor.fetchall.return_value = [{"chunk_id": "one"}]
                    elif sql.startswith("WITH scoped"):
                        cursor.fetchall.return_value = [{"chunk_id": "one"}]
                    else:
                        cursor.fetchall.return_value = []
                    return cursor

                conn.execute.side_effect = execute
                factory = Mock(side_effect=AssertionError("No model call needed for missing conditions"))
                facts = {"kind": "hotel", "academic_title": "院士", "destination": destination}
                if destination:
                    facts["lodging_standard"] = "部级"
                data = explain_policy(store, factory, settings(), "我的住宿标准是多少？", facts=facts,
                                      policy_query={"category": "hotel", "mode": "personal_limit"})
                self.assertEqual(data["rag"]["status"], "clarify")
                self.assertIn(expected, data["rag"]["proposal"]["requested_fields"])
                factory.assert_not_called()


class RetrievalEvaluationTests(unittest.TestCase):
    def test_default_evaluation_uses_production_sources_and_budget(self):
        store = Mock()
        store.settings = settings()
        store.search_for_facts.return_value = {
            "status": "ok", "context_incomplete": False,
            "sources": [source(chunk="linked-target")],
            "rankings": {"vector_ids": ["initial"], "keyword_ids": [], "rrf_ids": ["initial"]}}
        case = {"case_id": "closure", "query": "住宿", "gold_chunk_ids": ["linked-target"],
                "facts": {"kind": "hotel", "academic_title": "院士", "destination": "北京"}}
        rows, metrics = run_eval(store, [case])
        self.assertEqual(rows[0]["final_source_ids"], ["linked-target"])
        self.assertEqual(metrics["context_target_coverage"], 1)
        self.assertIsNone(metrics["answer_citation_target_coverage"])
        store.search_for_facts.assert_called_once_with(case["query"], case["facts"], include_rankings=True)
        store.settings = replace(settings(), policy_context_chars=1000)
        store.search_for_facts.return_value["sources"][0]["text"] = "证据" * 2000
        rows, metrics = run_eval(store, [case])
        self.assertFalse(rows[0]["context_usable"])
        self.assertEqual(metrics["context_target_coverage"], 0)

    def test_legacy_measurements_do_not_claim_to_measure_answer_citations(self):
        row = {"vector_ids": ["x"], "keyword_ids": ["x"], "rrf_ids": ["x"],
               "gold_chunk_ids": ["x"], "final_source_ids": ["x"]}
        self.assertIsNone(_metrics([row])["answer_citation_target_coverage"])

    def test_failed_empty_malformed_and_mixed_contexts_do_not_score_as_usable(self):
        store = Mock(settings=settings())
        cases = [
            {"status": "unavailable", "sources": []},
            {"status": "empty", "sources": []},
            {"status": "ok", "sources": [source(), source(chunk="two", version="another")]},
            {"status": "ok", "sources": [source()], "context_incomplete": True},
            {"status": "ok", "sources": [{"text": "invalid metadata"}]},
            {"status": "ok", "sources": [None]},
            {"status": "ok", "sources": None},
        ]
        for retrieved in cases:
            with self.subTest(retrieved=retrieved):
                store.search_for_facts.return_value = retrieved
                rows, metrics = run_eval(store, [{"query": "住宿", "gold_chunk_ids": ["b003-0"]}])
                self.assertFalse(rows[0]["context_usable"])
                self.assertEqual(metrics["context_target_coverage"], 0)

    def test_retrieval_exception_is_recorded_without_aborting_other_cases(self):
        store = Mock(settings=settings())
        store.search_for_facts.side_effect = [RuntimeError("internal details"),
                                              {"status": "ok", "sources": [source()]}]
        rows, metrics = run_eval(store, [{"query": "住宿", "gold_chunk_ids": ["b003-0"]}] * 2)
        self.assertEqual(metrics["context_target_coverage"], 0.5)
        self.assertEqual(rows[0]["retrieval_status"], "unavailable")
        self.assertNotIn("internal details", json.dumps(rows))

    def test_baseline_remains_selectable(self):
        store, _, _ = store_with_db([[database_doc()], [{"chunk_id": "one", "version": "test"}]])
        with patch("evals.rag_retrieval_eval._ranked_ids", return_value={
                "vector_ids": ["one"], "keyword_ids": [], "rrf_ids": ["one"]}):
            rows, metrics = run_eval(store, [{"query": "住宿", "gold_chunk_ids": ["one"]}], mode="baseline")
        self.assertEqual(rows[0]["evaluation_mode"], "baseline")
        self.assertEqual(metrics["context_target_coverage"], 1)

    def test_gold_from_different_policy_version_is_reported_as_not_comparable(self):
        store = Mock(settings=settings())
        store.search_for_facts.return_value = {"status": "ok", "sources": [source()]}
        _, metrics = run_eval(store, [{"query": "住宿", "gold_chunk_ids": ["b003-0"],
                                      "gold_version": "outdated-policy"}])
        self.assertIsNone(metrics["context_target_coverage"])
        self.assertEqual(metrics["gold_version_mismatch_cases"], 1)
        self.assertEqual(metrics["scored_cases"], 0)

    def test_service_failure_remains_in_score_denominator_when_gold_has_version(self):
        store = Mock(settings=settings())
        store.search_for_facts.return_value = {"status": "unavailable", "sources": []}
        _, metrics = run_eval(store, [{"query": "住宿", "gold_chunk_ids": ["b003-0"],
                                      "gold_version": "test-v2"}])
        self.assertEqual(metrics["context_target_coverage"], 0)
        self.assertEqual(metrics["scored_cases"], 1)


if __name__ == "__main__":
    unittest.main()

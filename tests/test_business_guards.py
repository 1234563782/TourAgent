"""Regression cases for observed policy and planning failures."""

import json
import unittest
from datetime import date
from unittest.mock import Mock

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from business_travel.contracts import Candidate, TransportRequest
from business_travel.graph import build_assistant, finalize_business_reply
from business_travel.graph import agent_graph
from business_travel.plan_checks import check_timeline
from business_travel.slots import SlotError, SlotTask as SlotSubmission, stage_submission, extract_slots
from business_travel.policy_rules import ExpenseFacts
from tests import test_travel_graph as fixtures
from tests.test_travel_graph import FixtureProvider, ScriptedModel, call, slot_response, PLAN_TEXT, review_response


class BusinessGuardTests(unittest.TestCase):
    setUp = fixtures.GraphTests.setUp
    tearDown = fixtures.GraphTests.tearDown

    def test_previous_turn_policy_is_not_reused(self):
        answer = AIMessage(content="请提供新的费用信息")
        messages = [
            HumanMessage(content="上一轮"),
            ToolMessage(name="consult_policy", tool_call_id="old", content='{"rule_checks": []}'),
            AIMessage(content="旧回复"), HumanMessage(content="新的问题"),
        ]
        self.assertIs(finalize_business_reply(messages, answer), answer)

    def test_cancelled_save_cannot_be_rewritten_as_saved(self):
        messages = [HumanMessage(content="保存"),
                    ToolMessage(name="save_trip", tool_call_id="save", content='{"status":"cancelled"}')]
        response = finalize_business_reply(messages, AIMessage(content="已经永久保存"))
        self.assertIn("未写入", response.content)
        self.assertNotIn("永久保存", response.content)

    def test_policy_and_save_results_are_both_preserved(self):
        messages = [HumanMessage(content="核对并保存"),
                    ToolMessage(name="consult_policy", tool_call_id="policy", content='{"rule_checks":[]}'),
                    ToolMessage(name="save_trip", tool_call_id="save", content='{"status":"saved"}')]
        response = finalize_business_reply(messages, AIMessage(content="合规"))
        self.assertIn("信息不足", response.content)
        self.assertIn("行程草案已保存", response.content)

    def test_retest_report_keeps_original_answer_and_failed_review(self):
        from scripts.render_business_retest import render
        record = {"id": "synthetic", "expected": "不得擅自批准", "turns": [{
            "question": "合规吗？", "answer": "错误回答：已批准", "status": "completed",
            "seconds": 1, "events": [], "memory": {"preferences": []},
        }]}
        report = render([record], {"synthetic": {"status": "未通过", "reason": "越权"}},
                        "2099-01-01", ["synthetic.json"])
        self.assertIn(record["turns"][0]["question"], report)
        self.assertIn(record["turns"][0]["answer"], report)
        self.assertIn("未通过1组", report)
        with self.assertRaises(ValueError):
            render([record, record], {"synthetic": {}}, "2099-01-01", [])

    def test_fabricated_and_out_of_range_citations_rejected(self):
        for field, value, index, quote in [("expense_date", "2099-09-08", 9, "2099-09-08"),
                                           ("destination", "上海", 0, "上海")]:
            with self.subTest(field=field), self.assertRaises(SlotError):
                stage_submission(SlotSubmission(intent="policy_check", expense={field: value},
                    evidence={field: {"user_message": index, "quote": quote}}), {}, ["北京"])

    def test_evidence_does_not_infer_currency_or_nightly_basis(self):
        # Verdict fixtures test enforcement only; real semantic cases live in the independent probe.
        cases = [("currency", "CNY", "400元", False),
                 ("basis", "room_night", "一晚400元", False),
                 ("special_approval", True, "并非有专项审批", False),
                 ("special_approval", True, "是否有专项审批", False),
                 ("long_assignment_days", 2, "2099-02-02", False),
                 ("long_assignment_days", 5, "三十五天", False),
                 ("long_assignment_days", 2, "出差两天", True),
                 ("basis", "room_night", "已核实该晚每间实际含税价600元人民币", True),
                 ("basis", "room_night", "每间实际含税价600元人民币", False)]
        for field, value, text, accepted in cases:
            with self.subTest(field=field, text=text):
                proposal = slot_response({field: value}, "policy_check", {field: text})
                verdict = review_response() if accepted else review_response("revise", ["unsupported meaning"])
                result = extract_slots(ScriptedModel([proposal]), [HumanMessage(content=text)], {}, 1,
                                       reviewer=ScriptedModel([verdict]))
                self.assertEqual(result["status"] == "ok", accepted)

    def test_policy_retries_stop_at_configured_limit(self):
        from dataclasses import replace
        policy = Mock()
        policy.search.return_value = {"status": "ok", "sources": []}
        models = {
            "reviewer": ScriptedModel([]),
            "supervisor": ScriptedModel([
                call("consult_policy", {"question": "核对", "expense_date": "2099-09-08"}, "policy"),
                AIMessage(content="不能核实"),
            ]),
            "slots": ScriptedModel([
                call("SlotSubmission", {"intent": "policy_check", "expense": {
                    "kind": "hotel", "expense_date": "2099-09-08", "destination": "上海",
                }}, "check"),
            ]),
        }
        graph = build_assistant(lambda role: models[role], replace(self.settings, slot_max_attempts=1),
                                self.memory, policy, provider=FixtureProvider())
        result = graph.invoke({"messages": [HumanMessage(content="核对住宿")]})
        self.assertIn("未通过校验", result["messages"][-1].content)
        self.assertEqual(result["slot_error"]["reason"], "slot_validation_failed")
        self.assertEqual(len(result["slot_error"]["validation_errors"]), 1)
        policy.check.assert_not_called()

    def test_wrapped_schema_error_gives_safe_actionable_hint(self):
        from langchain_core.tools import tool

        @tool
        def validate_input(facts: ExpenseFacts) -> dict:
            """Synthetic input validation."""
            return {"status": "ok"}

        model = ScriptedModel([
            call("validate_input", {"facts": {"kind": "hotel", "expense_date": "2099-09-08",
                  "destination": "上海", "evidence": {"secret": "synthetic-secret"}}}, "bad"),
            AIMessage(content="输入需修正"),
        ])
        result = agent_graph(model, [validate_input], "Test").invoke(
            {"messages": [HumanMessage(content="test")]})
        response = next(message for message in result["messages"] if isinstance(message, ToolMessage))
        payload = json.loads(response.content)
        self.assertIn({"field": "facts.evidence", "type": "extra_forbidden"}, payload["issues"])
        self.assertIn("Follow the tool schema", payload["hint"])
        self.assertNotIn("check_travel_policy", payload["hint"])
        self.assertNotIn("synthetic-secret", response.content)

    def test_missing_transport_times_are_unverified(self):
        candidate = Candidate(id="unknown", kind="flight", label="Synthetic", source="fixture",
                              observed_at="2099-01-01", request_date=date(2099, 9, 8))
        result = check_timeline([candidate], TransportRequest(**self.trip))
        self.assertEqual(result["status"], "unverified")
        self.assertEqual(result["unverified_candidates"], ["unknown"])
    def test_rule_failure_cannot_be_rewritten_as_compliant(self):
        policy = Mock()
        policy.search.return_value = {"status": "ok", "sources": []}
        policy.check.return_value = {
            "status": "insufficient_information", "reason": "required_facts_missing",
            "missing_fields": ["long_assignment_days"], "approved": False,
        }
        models = {
            "reviewer": ScriptedModel([review_response()]),
            "supervisor": ScriptedModel([
                call("consult_policy", {"question": "能报销吗", "expense_date": "2099-09-08"}, "policy"),
                AIMessage(content="肯定合规，能报销。"),
            ]),
            "slots": ScriptedModel([
                slot_response({
                    "kind": "hotel", "expense_date": "2099-09-08", "destination": "上海",
                }, "policy_check"),
            ]),
        }
        graph = build_assistant(lambda role: models[role], self.settings, self.memory, policy,
                                provider=FixtureProvider())
        result = graph.invoke({"messages": [HumanMessage(content="2099-09-08上海能报销吗？")]})
        self.assertNotIn("肯定合规", result["messages"][-1].content)
        self.assertIn("信息不足", result["messages"][-1].content)
        policy.check.assert_called_once()

    def test_unsourced_currency_and_basis_never_reach_rule_engine(self):
        policy = Mock()
        policy.search.return_value = {"status": "ok", "sources": []}
        policy.check.return_value = {"status": "insufficient_information", "sources": []}
        models = {
            "reviewer": ScriptedModel([review_response("revise", ["currency and basis unsupported"])] * 3),
            "supervisor": ScriptedModel([
                call("consult_policy", {"question": "能报销吗", "expense_date": "2099-09-08"}, "policy"),
                AIMessage(content="请补充信息"),
            ]),
            "slots": ScriptedModel([
                call("SlotSubmission", {"intent": "policy_check", "expense": {
                    "kind": "hotel", "expense_date": "2099-09-08", "destination": "上海",
                    "amount": 400, "currency": "CNY", "basis": "room_night",
                }, "evidence": {
                    "expense_date": {"user_message": 0, "quote": "2099-09-08"},
                    "destination": {"user_message": 0, "quote": "上海"},
                    "amount": {"user_message": 0, "quote": "400元"},
                    "currency": {"user_message": 0, "quote": "400元"},
                    "basis": {"user_message": 0, "quote": "一晚400元"},
                }}, "check"),
            ] * 3),
        }
        graph = build_assistant(lambda role: models[role], self.settings, self.memory, policy,
                                provider=FixtureProvider())
        graph.invoke({"messages": [HumanMessage(content="2099-09-08上海住酒店一晚400元能报销吗？")]})
        policy.check.assert_not_called()

    def test_arrival_after_hotel_checkout_is_not_complete_draft(self):
        trip = {**self.trip, "check_in": "2099-09-08", "check_out": "2099-09-09"}
        candidates = [
            Candidate(id="out", kind="flight", label="Synthetic", source="fixture",
                      observed_at="2099-01-01", request_date=date(2099, 9, 8),
                      departure="2099-09-08 22:00:00", arrival="2099-09-10 08:00:00"),
            Candidate(id="hotel", kind="hotel", label="Synthetic hotel", source="fixture",
                      observed_at="2099-01-01", request_date=date(2099, 9, 8)),
        ]
        from business_travel.contracts import SearchResult
        provider = Mock()
        provider.search.side_effect = [
            SearchResult(status="ok", candidates=[candidates[0]]),
            SearchResult(status="ok", candidates=[candidates[1]]),
        ]
        models = {
            "reviewer": ScriptedModel([review_response()]),
            "slots": ScriptedModel([slot_response(trip, "plan_trip", scope="combined")]),
            "supervisor": ScriptedModel([
                call("plan_trip", {"request": trip}, "plan"), AIMessage(content="完美行程，没有冲突。")]),
            "planner": ScriptedModel([
                call("PlanSelection", {"selected_ids": ["out", "hotel"]}, "submit"),
            ]),
        }
        graph = build_assistant(lambda role: models[role], self.settings, self.memory, self.policy,
                                provider=provider)
        result = graph.invoke({"messages": [HumanMessage(content=PLAN_TEXT + "，入住2099-09-08，退房2099-09-09")]})
        self.assertEqual(result["draft"]["status"], "incomplete")
        self.assertEqual(result["draft"]["selected"], [])
        self.assertEqual(result["draft"]["rejected_selections"][0]["timeline_check"]["status"], "conflict")
        self.assertIn("已排除冲突组合", result["messages"][-1].content)
        self.assertNotIn("没有冲突", result["messages"][-1].content)


if __name__ == "__main__":
    import unittest
    unittest.main()

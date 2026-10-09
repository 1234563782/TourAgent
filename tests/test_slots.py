"""Structured extraction tests use synthetic messages and the production validators."""

import json
import unittest
from copy import deepcopy
from dataclasses import replace
from unittest.mock import Mock

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError

from business_travel.graph import build_assistant
from business_travel.slots import SlotError, SlotTask as SlotSubmission, extract_slots, stage_submission as merge_submission
from tests import test_travel_graph as fixtures
from tests.test_travel_graph import FixtureProvider, ScriptedModel, call, slot_response, review_response


def reviewed_submission(submission, current, users, *, accepted):
    """Exercise commit gating with an explicit fixture verdict, not language understanding."""
    verdict = review_response() if accepted else review_response("revise", ["unsupported field meaning"])
    result = extract_slots(ScriptedModel([call("SlotSubmission",
        submission.model_dump(mode="json", exclude_unset=True), "s")]),
        [HumanMessage(content=text) for text in users], current, 1,
        reviewer=ScriptedModel([verdict]))
    if result["status"] != "ok":
        raise SlotError(result["reason"])
    return result["slots"]


class SlotTests(unittest.TestCase):
    def test_completed_goal_context_prevents_unattributed_intent_switch(self):
        current, users = self.initial()
        original = deepcopy(current)
        subject = current["expense"]
        prior = [{"id": "explanation", "intent": "policy_explain",
                  "question": "解释住宿上限", "subject_id": subject["id"],
                  "revision": subject["revision"], "status": "completed", "attempt": 1}]
        question = "目的地改为武汉，其他条件不变。"
        patch = self.update(current, {"destination": "武汉"}, {"destination": "武汉"})
        args = patch.model_dump(mode="json", exclude_unset=True)
        args.pop("action")
        args["operation"] = "update_facts"
        response = AIMessage(content="", tool_calls=[{
            "name": "SlotSubmission", "args": {"tasks": [args]}, "id": "switch"}])
        result = extract_slots(ScriptedModel([response]),
            [HumanMessage(content=text) for text in users + [question]], current, 1,
            reviewer=ScriptedModel([review_response()]), task_context=prior)
        self.assertEqual(result["status"], "error")
        self.assertIn("extra_forbidden", str(result["validation_errors"]))
        self.assertEqual(current, original)

    def test_explicit_goal_change_requires_review_not_just_a_matching_quote(self):
        current, users = self.initial()
        subject = current["expense"]
        prior = [{"id": "explanation", "intent": "policy_explain",
                  "question": "解释住宿上限", "subject_id": subject["id"],
                  "revision": subject["revision"], "status": "completed", "attempt": 1}]
        question = "现在核验这笔实际费用。"
        args = self.update(current, {}, {}).model_dump(mode="json", exclude_unset=True)
        args["intent_change_evidence"] = {"user_message": 1, "quote": question}
        for verdict, expected in ((review_response(), "ok"),
                                  (review_response("revise", ["goal change not authorized"]), "error")):
            with self.subTest(expected=expected):
                reviewer = Mock()
                reviewer.bind_tools.return_value = ScriptedModel([verdict])
                result = extract_slots(ScriptedModel([call("SlotSubmission", args, "switch")]),
                    [HumanMessage(content=text) for text in users + [question]], current, 1,
                    reviewer=reviewer, task_context=prior)
                self.assertEqual(result["status"], expected)
        args["intent_change_evidence"]["user_message"] = 0
        result = extract_slots(ScriptedModel([call("SlotSubmission", args, "stale")]),
            [HumanMessage(content=text) for text in users + [question]], current, 1,
            reviewer=ScriptedModel([]), task_context=prior)
        self.assertEqual(result["status"], "error")

    def test_delegation_preserves_request_and_existing_business_state(self):
        current, users = self.initial()
        current = extract_slots(ScriptedModel([call("SlotSubmission", {"tasks": [
            {"intent": "other", "question": "查看当前状态"}]}, "initial")]),
            [HumanMessage(content=users[0])], current, 1,
            reviewer=ScriptedModel([review_response()]))["slots"]
        original = deepcopy(current)
        question = "请删除长期偏好，但保留本次行程。"
        result = extract_slots(ScriptedModel([call("SlotSubmission", {"tasks": [
            {"intent": "other", "question": question}]}, "delegate")]),
            [HumanMessage(content=text) for text in users + [question]], current, 1,
            reviewer=ScriptedModel([review_response()]))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["slots"], original)
        self.assertEqual(current, original)
        self.assertEqual(result["tasks"], [
            {"intent": "other", "question": question, "subject": None, "depends_on": []}])

    def initial(self):
        args = {"intent": "policy_check", "expense": {
            "kind": "hotel", "expense_date": "2099-09-08", "destination": "上海", "amount": 400},
            "evidence": {key: {"user_message": 0, "quote": value} for key, value in {
                "expense_date": "2099-09-08", "destination": "上海", "amount": "400元"}.items()}}
        users = ["2099-09-08上海住宿400元"]
        state = merge_submission(SlotSubmission.model_validate(args), {}, users)
        state["expense"]["result"] = {"status": "within_checked_rules"}
        return state, users

    def update(self, state, values, quotes, index=1):
        old = state["expense"]
        return SlotSubmission.model_validate({
            "intent": "policy_check", "action": "update", "target_id": old["id"],
            "base_revision": old["revision"], "expense": values,
            "evidence": {key: {"user_message": index, "quote": quote} for key, quote in quotes.items()},
        })

    def test_amount_update_preserves_conditions_and_invalidates_result(self):
        current, users = self.initial()
        original = deepcopy(current)
        submission = self.update(current, {"amount": 500}, {"amount": "500元"})
        updated = merge_submission(submission, current, users + ["其他不变改为500元"])
        self.assertEqual(current, original)
        self.assertEqual(updated["expense"]["values"], {
            "kind": "hotel", "expense_date": "2099-09-08", "destination": "上海", "amount": "500"})
        self.assertEqual(updated["expense"]["id"], current["expense"]["id"])
        self.assertEqual(updated["expense"]["revision"], 2)
        self.assertIsNone(updated["expense"]["result"])
        self.assertEqual(updated["expense"]["history"][-1]["before"], "400")

    def test_new_expense_never_inherits_old_conditions(self):
        current, users = self.initial()
        updated = merge_submission(SlotSubmission(intent="policy_check", expense={"kind": "meal"}),
                                   current, users + ["另一笔伙食补助"])
        self.assertEqual(updated["expense"]["values"], {"kind": "meal"})
        self.assertNotEqual(updated["expense"]["id"], current["expense"]["id"])

    def test_explicit_null_clears_but_omission_preserves(self):
        current, users = self.initial()
        patch = self.update(current, {"amount": None}, {"amount": "金额不确定"})
        updated = merge_submission(patch, current, users + ["金额不确定"])
        self.assertIsNone(updated["expense"]["values"]["amount"])
        self.assertEqual(updated["expense"]["values"]["destination"], "上海")

    def test_unsupported_null_is_rejected(self):
        current, users = self.initial()
        patch = self.update(current, {"amount": None}, {"amount": "其他不变"})
        with self.assertRaises(SlotError):
            reviewed_submission(patch, current, users + ["其他不变"], accepted=False)

    def test_old_message_cannot_supply_new_amount(self):
        current, users = self.initial()
        patch = self.update(current, {"amount": 400}, {"amount": "400元"}, index=0)
        with self.assertRaisesRegex(SlotError, "current_message"):
            merge_submission(patch, current, users + ["改为500元"])

    def test_one_bad_field_rejects_entire_patch(self):
        current, users = self.initial()
        original = deepcopy(current)
        patch = self.update(current, {"amount": 500, "currency": "CNY"},
                            {"amount": "500元", "currency": "500元"})
        with self.assertRaises(SlotError):
            reviewed_submission(patch, current, users + ["改为500元"], accepted=False)
        self.assertEqual(current, original)

    def test_stale_revision_and_unknown_id_rejected(self):
        current, users = self.initial()
        good = self.update(current, {"amount": 500}, {"amount": "500元"})
        for changes in ({"base_revision": 0}, {"target_id": "other-user-subject"}):
            with self.subTest(changes=changes), self.assertRaises(SlotError):
                merge_submission(good.model_copy(update=changes), current, users + ["500元"])

    def test_nonbusiness_intent_cannot_change_slots(self):
        with self.assertRaises(SlotError):
            merge_submission(SlotSubmission(intent="other", expense={"amount": 500}), {}, ["500元"])

    def test_extra_fields_and_invalid_dates_fail_schema(self):
        for expense in ({"apikey": "private"}, {"expense_date": "2099-02-30"}, {"amount": -1}):
            with self.subTest(expense=expense), self.assertRaises(ValidationError):
                SlotSubmission(intent="policy_check", expense=expense)

    def test_explicit_date_verification_clause_can_support_short_citation(self):
        patch = SlotSubmission(intent="policy_check", expense={"date_verified": True},
                               evidence={"date_verified": {"user_message": 0, "quote": "2099-09-08"}})
        current = reviewed_submission(patch, {}, ["2099-09-08价格和日期已核实"], accepted=True)
        self.assertTrue(current["expense"]["values"]["date_verified"])
        with self.assertRaises(SlotError):
            reviewed_submission(patch, {}, ["2099-09-08"], accepted=False)

    def test_chinese_month_day_inherits_current_year(self):
        state, _ = self.initial()
        patch = self.update(state, {"expense_date": "2099-09-10"}, {"expense_date": "9月10日"})
        result = merge_submission(patch, state, ["原问题", "改为9月10日"])
        self.assertEqual(result["expense"]["values"]["expense_date"], "2099-09-10")

    def test_basis_short_quote_requires_explicit_same_clause_unit(self):
        patch = SlotSubmission(intent="policy_check", expense={"basis": "room_night"},
                               evidence={"basis": {"user_message": 0, "quote": "每间"}})
        result = reviewed_submission(patch, {}, ["已核实该晚每间实际含税价600元人民币"], accepted=True)
        self.assertEqual(result["expense"]["values"]["basis"], "room_night")
        with self.assertRaises(SlotError):
            reviewed_submission(patch, {}, ["每间600元"], accepted=False)

    def test_grade_must_preserve_complete_cited_name(self):
        for field in ("employee_grade", "roommate_grade"):
            evidence = {field: {"user_message": 0, "quote": "普通员工"}}
            with self.assertRaises(SlotError):
                reviewed_submission(SlotSubmission(intent="policy_check", expense={field: "普通"},
                                                evidence=evidence), {}, ["两位普通员工"], accepted=False)
            result = reviewed_submission(SlotSubmission(intent="policy_check", expense={field: "普通员工"},
                                                     evidence=evidence), {}, ["两位普通员工"], accepted=True)
            self.assertEqual(result["expense"]["values"][field], "普通员工")

    def test_extraction_retries_omitted_explicit_date_and_overnight(self):
        bad = {"intent": "policy_check", "expense": {"kind": "meal"}}
        good = {"intent": "policy_check", "expense": {
            "kind": "meal", "expense_date": "2099-09-08", "overnight_trip": True},
            "evidence": {key: {"user_message": 0, "quote": quote} for key, quote in {
                "expense_date": "2099-09-08", "overnight_trip": "过夜"}.items()}}
        model = ScriptedModel([call("SlotSubmission", bad, "bad"), call("SlotSubmission", good, "ok")])
        result = extract_slots(model, [HumanMessage(content="2099-09-08出差过夜，核对伙食补助")], {}, 2,
            reviewer=ScriptedModel([review_response("revise", ["explicit date and overnight omitted"]),
                                    review_response()]))
        self.assertTrue(result["slots"]["expense"]["values"].get("overnight_trip"))
        self.assertEqual(result["slots"]["expense"]["values"]["expense_date"], "2099-09-08")

    def test_extraction_retries_omitted_explicit_tax_flag(self):
        bad = {"intent": "policy_check", "expense": {"kind": "hotel"}}
        good = {"intent": "policy_check", "expense": {"kind": "hotel", "taxes_confirmed": True},
                "evidence": {"taxes_confirmed": {"user_message": 0, "quote": "含税"}}}
        model = ScriptedModel([call("SlotSubmission", bad, "bad"), call("SlotSubmission", good, "ok")])
        result = extract_slots(model, [HumanMessage(content="住宿价含税")], {}, 2,
            reviewer=ScriptedModel([review_response("revise", ["explicit tax flag omitted"]), review_response()]))
        self.assertTrue(result["slots"]["expense"]["values"]["taxes_confirmed"])

    def test_abbreviated_checkout_requires_unambiguous_explicit_month(self):
        submission = SlotSubmission(intent="plan_trip", trip={"query_scope": "hotel", "check_out": "2099-09-11"},
                                    evidence={"check_out": {"user_message": 0, "quote": "11日退房"},
                                              "query_scope": {"user_message": 0, "quote": "入住"}})
        result = reviewed_submission(submission, {}, ["2099-09-10出发，9月10日入住、11日退房"], accepted=True)
        self.assertEqual(result["trip"]["values"]["check_out"], "2099-09-11")
        with self.assertRaises(SlotError):
            reviewed_submission(submission, {}, ["2099-09-10出发，10月入住、11日退房"], accepted=False)

    def test_empty_update_invalidates_prior_result_before_recheck(self):
        state, users = self.initial()
        result = merge_submission(self.update(state, {}, {}), state, users + ["再核对一下"])
        self.assertEqual(result["expense"]["revision"], state["expense"]["revision"])
        self.assertIsNone(result["expense"]["result"])

    def test_exactly_one_designated_call_required_and_retries_are_bounded(self):
        multiple = AIMessage(content="", tool_calls=[
            {"name": "SlotSubmission", "args": {"intent": "other"}, "id": str(i)} for i in range(2)])
        for response in (AIMessage(content='{"intent":"other"}'), multiple,
                         call("another_tool", {"intent": "other"}, "wrong")):
            with self.subTest(response=response):
                model = Mock()
                model.bind_tools.return_value = model
                model.invoke.return_value = response
                result = extract_slots(model, [HumanMessage(content="你好")], {}, 2,
                                       reviewer=ScriptedModel([]))
                self.assertEqual(result["reason"], "slot_validation_failed")
                self.assertEqual(model.invoke.call_count, 2)
                self.assertEqual(model.bind_tools.call_args.kwargs["tool_choice"], "TurnRoute")

    def test_invalid_submission_can_be_corrected(self):
        model = ScriptedModel([AIMessage(content="普通文本"), call("SlotSubmission", {"intent": "other"}, "ok")])
        self.assertEqual(extract_slots(model, [HumanMessage(content="你好")], {}, 2,
            reviewer=ScriptedModel([review_response()]))["status"], "ok")

    def test_model_and_binding_failures_are_redacted(self):
        for operation in ("bind_tools", "invoke"):
            model = Mock()
            model.bind_tools.return_value = model
            getattr(model, operation).side_effect = TimeoutError("secret-api-key")
            result = extract_slots(model, [HumanMessage(content="你好")], {}, 3, reviewer=ScriptedModel([]))
            self.assertEqual(result["error_category"], "TimeoutError")
            self.assertNotIn("secret-api-key", str(result))

    def test_http_failures_report_only_status_not_response_body(self):
        import httpx
        from openai import APIStatusError
        for status in (402, 429, 500, 503):
            with self.subTest(status=status):
                error = APIStatusError("secret-api-key", response=httpx.Response(
                    status, request=httpx.Request("POST", "https://example.invalid/secret-api-key")),
                    body={"message": "secret-api-key"})
                for role in ("slots", "reviewer"):
                    model = Mock()
                    model.bind_tools.return_value = model
                    model.invoke.side_effect = error
                    extractor = model if role == "slots" else ScriptedModel([
                        call("SlotSubmission", {"intent": "other"}, "ok")])
                    reviewer = model if role == "reviewer" else ScriptedModel([])
                    result = extract_slots(extractor, [HumanMessage(content="你好")], {}, 1,
                                           reviewer=reviewer)
                    self.assertEqual(result["http_status"], status)
                    self.assertNotIn("secret-api-key", str(result))

    def test_validation_errors_do_not_echo_extra_secret_values(self):
        model = ScriptedModel([call("SlotSubmission", {"intent": "other", "extra": "secret-api-key"}, "bad")])
        result = extract_slots(model, [HumanMessage(content="你好")], {}, 1, reviewer=ScriptedModel([]))
        self.assertNotIn("secret-api-key", str(result))


class SlotGraphTests(unittest.TestCase):
    setUp = fixtures.GraphTests.setUp
    tearDown = fixtures.GraphTests.tearDown

    def test_amount_change_checks_new_value_once_without_supervisor_rewrite(self):
        facts = {"kind": "hotel", "expense_date": "2099-09-08", "destination": "上海", "amount": 400}
        models = {"supervisor": ScriptedModel([]), "reviewer": ScriptedModel([review_response()] * 2),
                  "slots": ScriptedModel([
            slot_response(facts, "policy_check", {"amount": "400元"}),
            slot_response({"amount": 500}, "policy_check", {"amount": "500元"}, update=True)])}
        policy = Mock()
        policy.check.side_effect = [
            {"status": "within_checked_rules", "amount": "400", "approved": False},
            {"status": "over_standard", "amount": "500", "excess": "80", "approved": False}]
        graph = build_assistant(lambda role: models[role], self.settings, self.memory, policy,
                                checkpointer=InMemorySaver(), provider=FixtureProvider())
        first = graph.invoke({"messages": [HumanMessage(content="2099-09-08上海400元")]}, self.config)
        result = graph.invoke({"messages": [HumanMessage(content="其他不变，改为500元")]}, self.config)
        self.assertEqual([str(c.args[0].amount) for c in policy.check.call_args_list], ["400", "500"])
        self.assertIn("超出所核对", result["messages"][-1].content)
        self.assertIn("80", result["messages"][-1].content)
        self.assertEqual(result["slots"]["expense"]["result"]["revision"], 2)
        self.assertEqual(first["slots"]["expense"]["revision"], 1)

    def test_thread_cannot_update_another_threads_subject(self):
        slots = ScriptedModel([slot_response({"kind": "hotel"}, "policy_check"),
                              call("SlotSubmission", {"intent": "policy_check", "action": "update",
                                   "target_id": "foreign", "base_revision": 1, "expense": {}}, "bad")])
        models = {"slots": slots, "supervisor": ScriptedModel([]),
                  "reviewer": ScriptedModel([review_response()])}
        graph = build_assistant(lambda role: models[role],
                                replace(self.settings, slot_max_attempts=1), self.memory, self.policy,
                                checkpointer=InMemorySaver(), provider=FixtureProvider())
        graph.invoke({"messages": [HumanMessage(content="核对酒店")]}, self.config)
        result = graph.invoke({"messages": [HumanMessage(content="继续")]},
                              {"configurable": {"thread_id": "different"}})
        self.assertEqual(result["slot_error"]["reason"], "slot_validation_failed")
        self.assertFalse(result.get("slots"))


if __name__ == "__main__":
    unittest.main()

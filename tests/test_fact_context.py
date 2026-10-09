"""Trust boundaries use real graph execution; scripted models do not measure language accuracy."""

import json
import unittest
from copy import deepcopy

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command
from pydantic import ValidationError

from business_travel.fact_context import confirm_subject, fact_context
from business_travel.policy_guard import render_policy, render_fact_context
from business_travel.slots import SlotSubmission, SlotTask, extract_slots, stage_submission
from tests import test_travel_graph as fixtures
from tests.test_travel_graph import ScriptedModel, call, slot_response, review_response, PLAN_TEXT


class FactContextTests(unittest.TestCase):
    def subject(self):
        text = "单位承担午饭，早晚饭自付"
        result = extract_slots(
            ScriptedModel([slot_response({"kind": "meal", "covered_meals": ["lunch"]},
                                        "policy_check", {"covered_meals": text})]),
            [HumanMessage(content=text)], {}, 1, reviewer=ScriptedModel([review_response()]))
        return result["slots"]["expense"]

    def test_model_review_is_not_confirmation_or_verification(self):
        fields = fact_context(self.subject())["fields"]
        self.assertEqual(fields["kind"]["source"], "model_inference")
        self.assertEqual(fields["covered_meals"]["source"], "user_statement")
        self.assertEqual(fields["covered_meals"]["review_status"], "model_reviewed")
        self.assertFalse(fields["covered_meals"]["user_confirmed"])
        self.assertFalse(fields["covered_meals"]["business_verified"])

    def test_model_cannot_submit_trust_metadata(self):
        for field in ("provenance", "fact_confirmation", "business_verified", "user_confirmed"):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                SlotSubmission.model_validate({"tasks": [{"intent": "other", field: True}]})

    def test_legacy_context_does_not_invent_origin(self):
        subject = {"id": "old", "revision": 1, "values": {"special_approval": True},
                   "validation": "context_reviewed",
                   "evidence": {"special_approval": {"quote": "审批通过", "status": "context_reviewed"}}}
        record = fact_context(subject)["fields"]["special_approval"]
        self.assertEqual(record["source"], "unknown")
        self.assertFalse(record["user_confirmed"])
        self.assertFalse(record["business_verified"])

    def test_free_text_is_not_translated_as_an_enum(self):
        answer = render_fact_context(fact_context({"values": {"purpose": "hotel", "covered_meals": ["lunch"]}}))
        self.assertIn("出差目的：hotel", answer)
        self.assertIn("免费提供的餐次：午餐", answer)

    def test_confirmation_is_bound_to_exact_version_and_does_not_mutate(self):
        subject = self.subject()
        before = deepcopy(subject)
        confirmed = confirm_subject(subject, subject["id"], subject["revision"])
        self.assertEqual(subject, before)
        record = fact_context(confirmed)["fields"]["covered_meals"]
        self.assertTrue(record["user_confirmed"])
        self.assertFalse(record["business_verified"])
        for ident, revision in [("different", subject["revision"]), (subject["id"], 999)]:
            with self.assertRaises(ValueError):
                confirm_subject(subject, ident, revision)

    def test_any_patch_invalidates_subject_acknowledgment_including_retraction(self):
        subject = self.subject()
        subject = confirm_subject(subject, subject["id"], subject["revision"])
        current = {"expense": subject, "subjects": {subject["id"]: subject}}
        for value in (["dinner"], None):
            patch = SlotTask(intent="policy_check", action="update", target_id=subject["id"],
                             base_revision=subject["revision"], expense={"covered_meals": value},
                             evidence={"covered_meals": {"user_message": 0, "quote": "更正餐次"}})
            updated = stage_submission(patch, current, ["更正餐次"])
            self.assertNotIn("fact_confirmation", updated["expense"])
            self.assertFalse(any(r["user_confirmed"] for r in fact_context(updated["expense"])["fields"].values()))
            self.assertTrue(fact_context(subject)["fields"]["covered_meals"]["user_confirmed"])

    def test_empty_patch_preserves_acknowledgment(self):
        subject = self.subject()
        subject = confirm_subject(subject, subject["id"], subject["revision"])
        patch = SlotTask(intent="policy_check", action="update", target_id=subject["id"],
                         base_revision=subject["revision"], expense={})
        updated = stage_submission(patch, {"expense": subject}, ["再核对"])
        self.assertTrue(fact_context(updated["expense"])["fields"]["covered_meals"]["user_confirmed"])

    def test_answer_exposes_even_wrong_reviewed_assumption_without_claiming_verified(self):
        subject = self.subject()
        subject["values"]["covered_meals"] = ["breakfast", "dinner"]
        answer = render_policy({"rule_checks": [{"facts": subject["values"],
            "fact_context": fact_context(subject),
            "result": {"status": "within_checked_rules", "amount": "42", "currency": "CNY"}}]})
        self.assertIn("免费提供的餐次：早餐、晚餐", answer)
        self.assertNotIn("尚未确认表述", answer)
        self.assertIn("如果有任何信息需要修改", answer)
        self.assertIn("不代表业务系统已核实", answer)
        self.assertIn("42", answer)


class FactConfirmationGraphTests(unittest.TestCase):
    setUp = fixtures.GraphTests.setUp
    tearDown = fixtures.GraphTests.tearDown
    build = fixtures.GraphTests.build

    @staticmethod
    def subject_context(messages):
        block = next(message.content for message in messages
                     if message.type == "system" and message.content.startswith("Current subject references"))
        return json.loads(block.split("\n", 1)[1])

    @staticmethod
    def request_confirmation(messages):
        context = FactConfirmationGraphTests.subject_context(messages)
        subject = context[0]
        return call("confirm_facts", {"subject_id": subject["subject_id"],
                                     "revision": subject["revision"]}, "confirm")

    def test_confirmation_requires_interrupt_and_is_not_a_save(self):
        graph = self.build([self.request_confirmation, AIMessage(content="已确认")],
                           slots=[slot_response(self.trip, "plan_trip"), slot_response()])
        graph.invoke({"messages": [HumanMessage(content=PLAN_TEXT)]}, self.config)
        pending = graph.invoke({"messages": [HumanMessage(content="确认刚才的表述")]}, self.config)
        self.assertEqual(pending["__interrupt__"][0].value["action"], "confirm_facts")
        self.assertFalse(fact_context(pending["slots"]["trip"])["fields"]["origin"]["user_confirmed"])
        result = graph.invoke(Command(resume=True), self.config)
        record = fact_context(result["slots"]["trip"])["fields"]["origin"]
        self.assertTrue(record["user_confirmed"])
        self.assertFalse(record["business_verified"])
        self.assertEqual(self.memory.read()["trip_history"], [])
        self.assertIn("不代表业务系统核实", result["messages"][-1].content)

    def test_rejection_does_not_confirm(self):
        for response in (False, "true", {"confirmed": True}):
            with self.subTest(response=response):
                graph = self.build([self.request_confirmation, AIMessage(content="结束")],
                    slots=[slot_response(self.trip, "plan_trip"), slot_response()])
                graph.invoke({"messages": [HumanMessage(content=PLAN_TEXT)]}, self.config)
                graph.invoke({"messages": [HumanMessage(content="确认表述")]}, self.config)
                result = graph.invoke(Command(resume=response), self.config)
                self.assertFalse(fact_context(result["slots"]["trip"])["fields"]["origin"]["user_confirmed"])

    def test_unknown_subject_never_interrupts(self):
        graph = self.build([call("confirm_facts", {"subject_id": "not-found", "revision": 1}, "bad"),
                            AIMessage(content="结束")])
        result = graph.invoke({"messages": [HumanMessage(content="确认")]}, self.config)
        self.assertNotIn("__interrupt__", result)
        self.assertIn("版本已变化", result["messages"][-1].content)

    def test_stale_version_never_interrupts_or_confirms(self):
        def stale(messages):
            context = self.subject_context(messages)[0]
            return call("confirm_facts", {"subject_id": context["subject_id"],
                                         "revision": context["revision"] + 1}, "stale")
        graph = self.build([stale, AIMessage(content="结束")],
                           slots=[slot_response(self.trip, "plan_trip"), slot_response()])
        graph.invoke({"messages": [HumanMessage(content=PLAN_TEXT)]}, self.config)
        result = graph.invoke({"messages": [HumanMessage(content="确认")]}, self.config)
        self.assertNotIn("__interrupt__", result)
        self.assertFalse(fact_context(result["slots"]["trip"])["fields"]["origin"]["user_confirmed"])

    def test_save_confirmation_is_not_fact_confirmation_and_keeps_provenance(self):
        graph = self.build([call("save_trip", {}, "save"), AIMessage(content="保存")],
                           slots=[slot_response(self.trip, "plan_trip"), slot_response()])
        first = graph.invoke({"messages": [HumanMessage(content=PLAN_TEXT)]}, self.config)
        self.assertIn(":::details 查看本次查询条件", first["messages"][-1].content)
        self.assertIn("按你提供的信息整理", first["messages"][-1].content)
        self.assertIn("出发地：Beijing", first["messages"][-1].content)
        pending = graph.invoke({"messages": [HumanMessage(content="保存")]}, self.config)
        payload = pending["__interrupt__"][0].value
        self.assertEqual(payload["scope"], "save_draft_only_not_fact_confirmation_or_approval")
        graph.invoke(Command(resume=True), self.config)
        record = self.memory.read()["trip_history"][0]["fact_context"]["fields"]["origin"]
        self.assertFalse(record["user_confirmed"])
        self.assertEqual(record["source"], "user_statement")

    def test_correction_after_confirmation_resets_confirmation(self):
        graph = self.build([self.request_confirmation, AIMessage(content="确认完成")],
            slots=[slot_response(self.trip, "plan_trip"), slot_response(),
                   slot_response({"departure_date": "2099-09-10"}, "plan_trip", update=True)])
        graph.invoke({"messages": [HumanMessage(content=PLAN_TEXT)]}, self.config)
        graph.invoke({"messages": [HumanMessage(content="确认")]}, self.config)
        graph.invoke(Command(resume=True), self.config)
        result = graph.invoke({"messages": [HumanMessage(content="改为2099-09-10")]}, self.config)
        self.assertFalse(fact_context(result["slots"]["trip"])["fields"]["origin"]["user_confirmed"])
        self.assertFalse(result["draft"]["fact_context"]["fields"]["departure_date"]["user_confirmed"])

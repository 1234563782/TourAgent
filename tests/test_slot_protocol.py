"""Native staged wire contracts: no atomic-fixture adapter or natural-language heuristics."""

import json
import unittest
from copy import deepcopy

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import ValidationError

from business_travel.context_budget import encoded_size
from business_travel.slot_protocol import (
    TurnRoute, TripExtraction, ExpenseExtraction, protocol_tool,
)
from business_travel.slots import SlotSubmission, extract_slots


def reply(name, args):
    return AIMessage(content="", tool_calls=[{"name": name, "id": name, "args": args}])


class RawModel:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []
        self.bindings = []

    def bind_tools(self, tools, **kwargs):
        self.bindings.append((tools, kwargs))
        return self

    def invoke(self, messages):
        self.calls.append(deepcopy(messages))
        response = next(self.replies)
        if isinstance(response, Exception):
            raise response
        return response(messages) if callable(response) else response


def accepted():
    return reply("SemanticReview", {"verdict": "accept"})


class SlotProtocolTests(unittest.TestCase):
    def run_proposal(self, replies, *, text="南京酒店；核验住宿金额319元", current=None,
                     review=None, **options):
        self.model = RawModel(replies)
        self.reviewer = RawModel([review or accepted()])
        state = deepcopy(current or {})
        before = deepcopy(state)
        result = extract_slots(self.model, [HumanMessage(text)], state, 1,
                               reviewer=self.reviewer, **options)
        self.assertEqual(state, before)
        return result

    @staticmethod
    def new(intent, question="请求"):
        return {"operation": "new_task", "intent": intent, "question": question}

    @staticmethod
    def hotel(index=0):
        return {"task_index": index, "trip": {"query_scope": "hotel", "destination": "南京"},
                "evidence": {"query_scope": {"quote": "酒店"}, "destination": {"quote": "南京"}}}

    @staticmethod
    def expense(index=0):
        return {"task_index": index, "expense": {"kind": "hotel", "amount": 319},
                "evidence": {"amount": {"quote": "319元"}}}

    def test_domain_can_clarify_ambiguous_meaning_without_committing_or_retrying(self):
        for intent, name, text, question in (
            ("policy_check", "ExpenseExtraction", "帮我核对这笔460的车费", "460指金额还是其他含义？"),
            ("plan_trip", "TripExtraction", "帮我查明天去乙地的G字头，那个两百的", "两百指预算还是车次？"),
        ):
            with self.subTest(domain=name):
                model = RawModel([
                    reply("TurnRoute", {"tasks": [self.new(intent, text)]}),
                    reply(name, {"clarification": question}),
                ])
                reviewer = RawModel([])
                current = {}
                result = extract_slots(model, [HumanMessage(text)], current, 3, reviewer=reviewer)
                self.assertEqual(result, {"status": "clarify", "question": question})
                self.assertEqual(current, {})
                self.assertEqual(len(model.calls), 2)
                self.assertFalse(reviewer.calls)

    def test_domain_clarification_is_mutually_exclusive_with_patches(self):
        for contract, patch in ((TripExtraction, self.hotel()), (ExpenseExtraction, self.expense())):
            with self.subTest(contract=contract.__name__):
                with self.assertRaises(ValidationError):
                    contract.model_validate({"patches": [patch], "clarification": "哪一笔？"})
                with self.assertRaises(ValidationError):
                    contract.model_validate({"patches": [], "clarification": " "})

    def test_school_identity_is_preserved_in_both_domain_contracts(self):
        fields = {"personnel_type": "staff", "technical_grade": 5,
                  "lodging_standard": "其他人员", "destination_province": "江苏"}
        quotes = {"personnel_type": "教职工", "technical_grade": "专业技术五级",
                  "lodging_standard": "住宿其他人员档", "destination_province": "江苏"}
        for intent, name, domain, base in (
            ("plan_trip", "TripExtraction", "trip", self.hotel()),
            ("policy_check", "ExpenseExtraction", "expense", self.expense()),
        ):
            patch = deepcopy(base)
            patch[domain].update(fields)
            patch["evidence"].update({key: {"quote": value} for key, value in quotes.items()})
            result = self.run_proposal([
                reply("TurnRoute", {"tasks": [self.new(intent)]}),
                reply(name, {"patches": [patch]}),
            ], text="教职工，专业技术五级，住宿其他人员档，江苏南京酒店；核验住宿金额319元")
            self.assertEqual(result["status"], "ok")
            values = result["tasks"][0]["subject"]["values"]
            self.assertEqual({key: values[key] for key in fields}, fields)
            self.assertFalse(values.get("employee_grade"))

    def test_each_domain_receives_only_its_contract(self):
        for intent, name, patch, absent in [
            ("plan_trip", "TripExtraction", self.hotel(), "covered_meals"),
            ("policy_check", "ExpenseExtraction", self.expense(), "hotel_child_ages"),
        ]:
            with self.subTest(intent=intent):
                result = self.run_proposal([
                    reply("TurnRoute", {"tasks": [self.new(intent)]}),
                    reply(name, {"patches": [patch]}),
                ])
                self.assertEqual(result["status"], "ok")
                self.assertEqual([binding[1]["tool_choice"] for binding in self.model.bindings],
                                 ["TurnRoute", name])
                self.assertNotIn(absent, json.dumps(self.model.bindings[-1][0]))
                self.assertEqual(result["tasks"][0]["subject"]["revision"], 1)
                evidence = result["tasks"][0]["subject"]["evidence"]
                self.assertTrue(all(item["user_message"] == 0 for item in evidence.values()))

    def test_mixed_batch_preserves_indices_and_dependencies(self):
        route = {"tasks": [self.new("policy_check", "费用"), self.new("plan_trip", "酒店"),
                           {**self.new("other", "解释结果"), "depends_on": [0, 1]}]}
        result = self.run_proposal([
            reply("TurnRoute", route),
            reply("TripExtraction", {"patches": [self.hotel(1)]}),
            reply("ExpenseExtraction", {"patches": [self.expense(0)]}),
        ])
        self.assertEqual(result["status"], "ok")
        self.assertEqual([t["intent"] for t in result["tasks"]], ["policy_check", "plan_trip", "other"])
        self.assertEqual(result["tasks"][2]["depends_on"], [0, 1])
        self.assertEqual(result["tasks"][0]["subject"]["values"]["amount"], "319")
        self.assertEqual(result["tasks"][1]["subject"]["values"]["destination"], "南京")

    def test_same_domain_is_extracted_in_one_batch(self):
        result = self.run_proposal([
            reply("TurnRoute", {"tasks": [self.new("plan_trip"), self.new("plan_trip")]}),
            reply("TripExtraction", {"patches": [self.hotel(1), self.hotel(0)]}),
        ])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(self.model.calls), 2)
        self.assertEqual(len(self.reviewer.calls), 1)
        self.assertNotEqual(result["tasks"][0]["subject"]["id"], result["tasks"][1]["subject"]["id"])

    def test_text_multiple_and_malformed_tool_calls_are_not_parsed_as_commands(self):
        valid = reply("TurnRoute", {"tasks": [self.new("other")]})
        multiple = valid.model_copy(update={"tool_calls": valid.tool_calls * 2})
        malformed = AIMessage(content="", invalid_tool_calls=[
            {"name": "TurnRoute", "id": "bad", "args": "{", "error": "invalid json"}])
        for response in (AIMessage(content=json.dumps(valid.tool_calls[0]["args"])), multiple, malformed):
            with self.subTest(response=response):
                result = self.run_proposal([response])
                self.assertEqual(result["status"], "error")
                self.assertFalse(self.reviewer.calls)

    def test_recovery_binds_current_authorization_but_preserves_selected_fact_source(self):
        route = {"tasks": [self.new("policy_check")], "recovery": {
            "action": "retry", "user_messages": [0], "evidence": {"quote": "重试刚才失败的核验"}}}
        model = RawModel([
            reply("TurnRoute", route),
            reply("ExpenseExtraction", {"patches": [{
                "task_index": 0, "expense": {"kind": "hotel", "amount": 319},
                "evidence": {"amount": {"user_message": 0, "quote": "319元"}}}]}),
        ])
        result = extract_slots(model, [HumanMessage("核验住宿319元"), HumanMessage("重试刚才失败的核验")],
            {}, 1, reviewer=RawModel([accepted()]),
            pending_turns=[{"user_message": 0, "content": "核验住宿319元", "retryable": True}])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["resolved_pending_turns"], [0])
        subject = result["tasks"][0]["subject"]
        self.assertEqual(subject["evidence"]["amount"]["user_message"], 0)
        self.assertEqual(subject["recovery_authorization"]["evidence"]["user_message"], 1)

    def test_discard_has_no_domain_extraction_and_no_business_commit(self):
        route = {"recovery": {"action": "discard", "user_messages": [0], "evidence": {"quote": "放弃"}}}
        model, reviewer = RawModel([reply("TurnRoute", route)]), RawModel([accepted()])
        result = extract_slots(model, [HumanMessage("核验住宿319元"), HumanMessage("放弃")],
            {}, 1, reviewer=reviewer,
            pending_turns=[{"user_message": 0, "content": "核验住宿319元", "retryable": True}])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["tasks"], [])
        self.assertEqual(result["slots"], {})
        self.assertEqual(result["resolved_pending_turns"], [0])
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(len(reviewer.calls), 1)

    def test_missing_duplicate_unknown_and_wrong_domain_patches_rejected_atomically(self):
        for patches in ([], [self.hotel(), self.hotel()], [self.hotel(1)], [self.expense()]):
            with self.subTest(patches=patches):
                result = self.run_proposal([
                    reply("TurnRoute", {"tasks": [self.new("plan_trip")]}),
                    reply("TripExtraction", {"patches": patches}),
                ])
                self.assertEqual(result["status"], "error")
                self.assertNotIn("slots", result)
                self.assertFalse(self.reviewer.calls)

    def test_failure_in_second_domain_keeps_entire_batch_uncommitted(self):
        result = self.run_proposal([
            reply("TurnRoute", {"tasks": [self.new("plan_trip"), self.new("policy_check")]}),
            reply("TripExtraction", {"patches": [self.hotel()]}),
            TimeoutError("private"),
        ])
        self.assertEqual(result["reason"], "slot_model_unavailable")
        self.assertNotIn("private", str(result))
        self.assertNotIn("slots", result)
        self.assertFalse(self.reviewer.calls)

    def test_route_cannot_submit_versions_status_or_business_fields(self):
        for extra in ({"base_revision": 1}, {"expected_attempt": 0}, {"expense": {}},
                      {"evidence": {}}, {"prior_intent": "other"}):
            with self.subTest(extra=extra), self.assertRaises(ValidationError):
                TurnRoute.model_validate({"tasks": [{**self.new("other"), **extra}]})
        for extra in ({"expected_revision": 1}, {"expected_status": "ready"}, {"expected_attempt": 1}):
            with self.subTest(extra=extra), self.assertRaises(ValidationError):
                TurnRoute.model_validate({"task_controls": [{
                    "action": "cancel", "target_id": "task-a", "evidence": {"quote": "取消"}, **extra}]})
        for extra in ({"intent": "plan_trip"}, {"intent": None},
                      {"intent_change_evidence": {"quote": "更新"}}):
            with self.subTest(extra=extra), self.assertRaises(ValidationError):
                TurnRoute.model_validate({"tasks": [{
                    "operation": "update_facts", "target_id": "s", **extra}]})

    def test_previous_public_tool_is_rejected_not_a_fallback(self):
        result = self.run_proposal([reply("SlotSubmission", {"tasks": [self.new("other")]})])
        self.assertEqual(result["reason"], "slot_validation_failed")
        self.assertIn("TurnRoute", str(result["validation_errors"]))

    def test_ambiguous_target_returns_question_without_extracting_or_committing(self):
        for args in ({"clarification": "要修改哪一笔费用？"},
                     {"tasks": [{"operation": "update_facts", "question": "改为319元"}]}):
            with self.subTest(args=args):
                result = self.run_proposal([reply("TurnRoute", args)])
                self.assertEqual(result["status"], "clarify")
                self.assertEqual(len(self.model.calls), 1)
                self.assertNotIn("slots", result)

    def test_control_binds_current_metadata_and_requires_independent_review(self):
        subject = {"id": "subject", "domain": "trip", "revision": 4, "values": {}, "evidence": {}}
        task = {"id": "task", "subject_id": "subject", "revision": 4,
                "intent": "plan_trip", "status": "failed", "attempt": 3}
        route = {"task_controls": [{"action": "resume", "target_id": "task",
                                    "evidence": {"quote": "重试酒店"}}]}
        for verdict, status in ((accepted(), "ok"),
                                (reply("SemanticReview", {"verdict": "revise", "issues": ["negated"]}), "error")):
            with self.subTest(status=status):
                result = self.run_proposal([reply("TurnRoute", route)], text="重试酒店",
                    pending_tasks=[task], current={"subjects": {"subject": subject}}, review=verdict)
                self.assertEqual(result["status"], status)
                self.assertEqual(len(self.model.calls), 1)
                if status == "ok":
                    control = result["task_controls"][0]
                    self.assertEqual([control["expected_" + key] for key in ("revision", "attempt", "status")],
                                     [4, 3, "failed"])

    def test_no_patch_can_change_routing_or_bind_another_subject(self):
        for extra in ({"intent": "policy_check"}, {"target_id": "another"}, {"base_revision": 100}):
            with self.subTest(extra=extra):
                result = self.run_proposal([
                    reply("TurnRoute", {"tasks": [self.new("plan_trip")]}),
                    reply("TripExtraction", {"patches": [{**self.hotel(), **extra}]}),
                ])
                self.assertEqual(result["status"], "error")

    def test_unselected_historical_evidence_is_rejected(self):
        model = RawModel([
            reply("TurnRoute", {"tasks": [self.new("policy_check")]}),
            reply("ExpenseExtraction", {"patches": [{
                "task_index": 0, "expense": {"amount": 319},
                "evidence": {"amount": {"user_message": 0, "quote": "319元"}}}]}),
        ])
        result = extract_slots(model, [HumanMessage("319元"), HumanMessage("金额未知")], {}, 1,
                               reviewer=RawModel([accepted()]))
        self.assertEqual(result["status"], "error")
        self.assertIn("cite_exact_current_message", str(result))

    def test_retry_budget_is_bounded_across_stages(self):
        model = RawModel([
            reply("TurnRoute", {"tasks": [self.new("plan_trip")]}),
            reply("TripExtraction", {"patches": []}),
            reply("TurnRoute", {"tasks": [self.new("plan_trip")]}),
            reply("TripExtraction", {"patches": [self.hotel()]}),
        ])
        result = extract_slots(model, [HumanMessage("南京酒店")], {}, 2, reviewer=RawModel([accepted()]))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(model.calls), 4)
        retry = json.loads(model.calls[2][-1].content)
        self.assertIn("assigned_task_indices", str(retry["validation_errors"]))

    def test_same_intent_change_rejected_then_model_can_repair_as_fact_update(self):
        subject = {"id": "s", "domain": "trip", "revision": 3, "intent": "plan_trip",
                   "values": {"query_scope": "hotel", "destination": "成都"}, "evidence": {},
                   "history": []}
        ledger = [{"id": "t", "subject_id": "s", "revision": 3, "intent": "plan_trip",
                   "status": "completed", "attempt": 1}]
        request = {"operation": "change_intent", "intent": "plan_trip", "target_id": "s",
                   "target_task_id": "t", "question": "查询武汉住宿",
                   "intent_change_evidence": {"quote": "目的地换为武汉后再查住宿"}}
        repaired = {key: value for key, value in request.items()
                    if key not in {"intent", "intent_change_evidence"}}
        repaired["operation"] = "update_facts"
        model = RawModel([
            reply("TurnRoute", {"tasks": [request]}),
            reply("TurnRoute", {"tasks": [{**repaired, "intent": "plan_trip"}]}),
            reply("TurnRoute", {"tasks": [repaired]}),
            reply("TripExtraction", {"patches": [{"task_index": 0, "trip": {"destination": "武汉"},
                "evidence": {"destination": {"quote": "武汉"}}}]}),
        ])
        current = {"trip": subject, "subjects": {"s": subject}}
        before = deepcopy(current)
        result = extract_slots(model, [HumanMessage("目的地换为武汉后再查住宿")], current, 3,
                               reviewer=RawModel([accepted()]), task_context=ledger)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(current, before)
        self.assertEqual([binding[1]["tool_choice"] for binding in model.bindings],
                         ["TurnRoute", "TurnRoute", "TurnRoute", "TripExtraction"])
        retry = json.loads(model.calls[1][-1].content)
        self.assertEqual(retry["validation_errors"], ["change_intent_requires_different_intent"])
        self.assertIn("update_facts", retry["validation_guidance"])
        schema_retry = json.loads(model.calls[2][-1].content)
        self.assertEqual(schema_retry["validation_errors"][0][0],
                         {"field": "tasks.0.update_facts.intent", "error": "extra_forbidden"})
        self.assertIn("OMIT", schema_retry["validation_guidance"])
        task = result["tasks"][0]
        self.assertEqual((task["intent"], task["operation"], task["target_task_id"]),
                         ("plan_trip", "update_facts", "t"))
        self.assertEqual(task["subject"]["revision"], 4)
        self.assertEqual(task["subject"]["values"]["destination"], "武汉")

    def test_trip_schema_explains_short_trip_duration_and_per_room_occupancy(self):
        properties = protocol_tool(TripExtraction)["function"]["parameters"]["properties"]
        trip = properties["patches"]["items"]["properties"]["trip"]["properties"]
        self.assertIn("short trips", trip["long_assignment_days"].get("description", ""))
        self.assertIn("per room", trip["room_occupants"].get("description", ""))

    def test_evidence_mapping_shape_is_rejected_then_repaired_by_model(self):
        bad = {**self.hotel(), "evidence": {"quote": "南京酒店"}}
        model = RawModel([
            reply("TurnRoute", {"tasks": [self.new("plan_trip")]}),
            reply("TripExtraction", {"patches": [bad]}),
            reply("TurnRoute", {"tasks": [self.new("plan_trip")]}),
            reply("TripExtraction", {"patches": [self.hotel()]}),
        ])
        reviewer = RawModel([accepted()])
        result = extract_slots(model, [HumanMessage("南京酒店")], {}, 2, reviewer=reviewer)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(reviewer.calls), 1)
        retry = json.loads(model.calls[2][-1].content)
        self.assertEqual(retry["validation_errors"][0][0],
                         {"field": "patches.0.evidence.quote", "error": "model_type"})
        self.assertIn("field names", retry["validation_guidance"])
        for contract in (TripExtraction, ExpenseExtraction):
            evidence = protocol_tool(contract)["function"]["parameters"]["properties"][
                "patches"]["items"]["properties"]["evidence"]
            self.assertIn("field names", evidence.get("description", ""))

    def occupancy_patch(self, **fields):
        patch = self.hotel()
        patch["trip"].update(fields)
        patch["evidence"].update({field: {"quote": "入住安排"} for field in fields})
        return patch

    def test_complete_single_room_party_cannot_silently_omit_or_contradict_occupancy(self):
        for adults, children in ((1, []), (3, []), (2, [4, 8])):
            for occupancy in ({}, {"room_occupants": adults + len(children) + 1}):
                with self.subTest(adults=adults, children=children, occupancy=occupancy):
                    result = self.run_proposal([
                        reply("TurnRoute", {"tasks": [self.new("plan_trip")]}),
                        reply("TripExtraction", {"patches": [self.occupancy_patch(
                            hotel_rooms=1, hotel_adults=adults, hotel_child_ages=children, **occupancy)]}),
                    ], text="南京酒店入住安排")
                    self.assertEqual(result["status"], "error")
                    self.assertIn("single_room_occupancy_requires_reconciliation", str(result))
                    self.assertNotIn("slots", result)
                    self.assertFalse(self.reviewer.calls)

    def test_unknown_retracted_or_multiroom_occupancy_is_not_invented(self):
        for fields in (
            {"hotel_rooms": 2, "hotel_adults": 3, "hotel_child_ages": []},
            {"hotel_rooms": 1, "hotel_adults": 2},
            {"hotel_rooms": 1, "hotel_adults": 2, "hotel_child_ages": None},
            {"hotel_rooms": 1, "hotel_child_ages": []},
            {"hotel_rooms": 1, "hotel_adults": 2, "hotel_child_ages": [], "room_occupants": None},
        ):
            with self.subTest(fields=fields):
                result = self.run_proposal([
                    reply("TurnRoute", {"tasks": [self.new("plan_trip")]}),
                    reply("TripExtraction", {"patches": [self.occupancy_patch(**fields)]}),
                ], text="南京酒店入住安排")
                self.assertEqual(result["status"], "ok")
                self.assertIsNone(result["slots"]["trip"]["values"].get("room_occupants"))

    def test_single_room_repair_is_resubmitted_by_model_and_reviewed_not_autofilled(self):
        route = reply("TurnRoute", {"tasks": [self.new("plan_trip")]})
        party = {"hotel_rooms": 1, "hotel_adults": 2, "hotel_child_ages": [7]}
        model = RawModel([
            route, reply("TripExtraction", {"patches": [self.occupancy_patch(**party)]}),
            route, reply("TripExtraction", {"patches": [
                self.occupancy_patch(**party, room_occupants=3)]}),
        ])
        reviewer = RawModel([accepted()])
        result = extract_slots(model, [HumanMessage("南京酒店入住安排")], {}, 2, reviewer=reviewer)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["slots"]["trip"]["values"]["room_occupants"], 3)
        self.assertEqual(len(model.calls), 4)
        self.assertEqual(len(reviewer.calls), 1)
        retry = json.loads(model.calls[2][-1].content)
        self.assertIn("single room", retry["validation_guidance"])

    def test_empty_fact_update_is_rejected_before_review_for_each_domain(self):
        for intent, domain, tool in (("plan_trip", "trip", "TripExtraction"),
                                     ("policy_check", "expense", "ExpenseExtraction")):
            with self.subTest(domain=domain):
                subject = {"id": "s", "domain": domain, "revision": 2, "intent": intent,
                           "values": {}, "evidence": {}, "history": []}
                ledger = [{"id": "t", "subject_id": "s", "revision": 2, "intent": intent,
                           "status": "needs_clarification", "attempt": 2}]
                result = self.run_proposal([
                    reply("TurnRoute", {"tasks": [{"operation": "update_facts",
                        "target_id": "s", "target_task_id": "t"}]}),
                    reply(tool, {"patches": [{"task_index": 0, domain: {}}]}),
                ], current={domain: subject, "subjects": {"s": subject}}, task_context=ledger)
                self.assertEqual(result["status"], "error")
                self.assertIn("update_facts_requires_submitted_fields", str(result))
                self.assertNotIn("slots", result)
                self.assertFalse(self.reviewer.calls)

    def test_empty_update_retry_can_delegate_without_changing_existing_task(self):
        subject = {"id": "s", "domain": "trip", "revision": 2, "intent": "plan_trip",
                   "values": {"query_scope": "hotel"}, "evidence": {}, "history": []}
        ledger = [{"id": "t", "subject_id": "s", "revision": 2, "intent": "plan_trip",
                   "status": "needs_clarification", "attempt": 2}]
        current = {"trip": subject, "subjects": {"s": subject}}
        model = RawModel([
            reply("TurnRoute", {"tasks": [{"operation": "update_facts",
                "target_id": "s", "target_task_id": "t"}]}),
            reply("TripExtraction", {"patches": [{"task_index": 0, "trip": {}}]}),
            reply("TurnRoute", {"tasks": [self.new("other", "保留待办，暂不执行")]}),
        ])
        reviewer = RawModel([accepted()])
        result = extract_slots(model, [HumanMessage("保留待办，暂不执行")], current, 2,
                               reviewer=reviewer, task_context=ledger, pending_tasks=ledger)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["slots"], current)
        self.assertEqual([task["intent"] for task in result["tasks"]], ["other"])
        self.assertEqual(result["task_controls"], [])
        self.assertEqual(len(reviewer.calls), 1)
        retry = json.loads(model.calls[-1][-1].content)
        self.assertEqual(retry["validation_errors"], ["update_facts_requires_submitted_fields"])
        self.assertIn("authorization", retry["validation_guidance"])

    def test_schema_budget_has_no_repeated_domain_per_operation(self):
        previous = encoded_size(convert_to_openai_tool(SlotSubmission))
        sizes = {contract.__name__: encoded_size(protocol_tool(contract))
                 for contract in (TurnRoute, TripExtraction, ExpenseExtraction)}
        self.assertLess(sum(sizes.values()), previous)
        self.assertLess(max(sizes.values()), previous / 2)


if __name__ == "__main__":
    unittest.main()

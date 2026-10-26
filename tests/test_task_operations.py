"""Operation isolation tests submit raw tool calls, without fixture migration."""

import json
import unittest
from copy import deepcopy

from langchain_core.messages import HumanMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import ValidationError

from business_travel.slots import extract_slots
from business_travel.slot_protocol import TurnRoute
from business_travel.tasks import reconcile_tasks
from tests.test_travel_graph import ScriptedModel, review_response
from tests.test_slot_protocol import RawModel, reply


class TaskOperationTests(unittest.TestCase):
    def setUp(self):
        self.subject = {"id": "subject-a", "revision": 3, "domain": "expense",
                        "intent": "policy_explain", "values": {"kind": "hotel", "destination": "甲"},
                        "policy_query": {"category": "hotel", "mode": "personal_limit"},
                        "history": [], "evidence": {}, "result": {"status": "old"},
                        "fact_confirmation": {"revision": 3}, "validation": "context_reviewed"}
        self.current = {"expense": self.subject, "subjects": {"subject-a": self.subject}}
        self.task = {"id": "task-a", "subject_id": "subject-a", "revision": 3,
                     "intent": "policy_explain", "question": "解释标准", "status": "completed",
                     "attempt": 1, "dependencies": [], "result_ref": {"revision": 3}}
        self.update = {"operation": "update_facts", "target_id": "subject-a",
                       "expense": {"destination": "乙"},
                       "policy_query": {"category": "other", "mode": "general"},
                       "question": "解释乙的标准",
                       "evidence": {"destination": {"user_message": 0, "quote": "乙"}}}

    def extract(self, proposal=None, *, ledger=None, verdict=None, text="改为乙"):
        original = deepcopy(self.current)
        args = deepcopy(proposal or self.update)
        patch = {"task_index": 0}
        for key in ("expense", "trip", "evidence", "policy_query"):
            if key in args:
                patch[key] = args.pop(key)
        if "intent_change_evidence" in args:
            args["intent_change_evidence"].pop("user_message", None)
        result = extract_slots(RawModel([
            reply("TurnRoute", {"tasks": [args]}), reply("ExpenseExtraction", {"patches": [patch]})]),
            [HumanMessage(content=text)],
            self.current, 1, reviewer=ScriptedModel([verdict or review_response()]),
            task_context=[self.task] if ledger is None else ledger)
        self.assertEqual(self.current, original)
        return result

    def test_public_schema_forbids_old_protocol_and_goal_on_fact_update(self):
        variants = [
            {"intent": "policy_explain", "expense": {}},
            {**self.update, "intent": "policy_check"},
            {**self.update, "action": "update"},
            {"operation": "new_task", "intent": "policy_explain", "target_id": "subject-a"},
            {**self.update, "operation": "change_intent", "intent": "policy_check"},
            {**self.update, "prior_intent": "policy_check"},
        ]
        for args in variants:
            with self.subTest(args=args):
                with self.assertRaises(ValidationError):
                    route = {key: value for key, value in args.items() if key not in {"expense", "evidence"}}
                    TurnRoute.model_validate({"tasks": [route]})
                self.assertEqual(self.extract(args)["status"], "error")
        schema = convert_to_openai_tool(TurnRoute)["function"]["parameters"]
        self.assertIn("tasks", schema["properties"])

    def test_update_inherits_exact_goal_and_task(self):
        result = self.extract()
        self.assertEqual(result["status"], "ok")
        record = result["tasks"][0]
        self.assertEqual(record["intent"], "policy_explain")
        self.assertEqual(record["target_task_id"], self.task["id"])
        tasks, queue = reconcile_tasks({self.task["id"]: self.task}, result["tasks"], result["slots"])
        self.assertEqual(queue, [self.task["id"]])
        self.assertEqual(tasks[self.task["id"]]["revision"], 4)
        self.assertIsNone(tasks[self.task["id"]]["result_ref"])
        self.assertNotIn("fact_confirmation", result["slots"]["expense"])

    def test_followup_inherits_policy_scope_and_canonical_goal_when_omitted(self):
        args = {**self.update, "expense": {"destination": "北京"}, "question": ""}
        args["evidence"] = {"destination": {"user_message": 0, "quote": "北京"}}
        args.pop("policy_query")
        result = self.extract(args, text="北京")
        self.assertEqual(result["status"], "ok")
        record = result["tasks"][0]
        self.assertEqual(record["question"], self.task["question"])
        self.assertEqual(result["slots"]["expense"]["policy_query"],
                         self.subject["policy_query"])

    def test_fact_update_cannot_replace_canonical_goal_with_short_patch(self):
        args = {**self.update, "expense": {"destination": "北京"}, "question": "目的地北京"}
        args["evidence"] = {"destination": {"user_message": 0, "quote": "北京"}}
        args.pop("policy_query")
        result = self.extract(args, text="北京")
        self.assertEqual(result["status"], "ok")
        tasks, _ = reconcile_tasks({self.task["id"]: self.task}, result["tasks"], result["slots"])
        self.assertEqual(tasks[self.task["id"]]["question"], "目的地北京")
        self.assertEqual(tasks[self.task["id"]]["canonical_question"], self.task["question"])

    def test_explicit_policy_scope_change_uses_new_canonical_goal(self):
        result = self.extract()
        self.assertEqual(result["status"], "ok")
        tasks, _ = reconcile_tasks({self.task["id"]: self.task}, result["tasks"], result["slots"])
        self.assertEqual(tasks[self.task["id"]]["question"], "解释乙的标准")
        self.assertEqual(tasks[self.task["id"]]["canonical_question"], "解释乙的标准")

    def test_policy_scope_expansion_preserves_confirmed_destination(self):
        args = {**self.update, "expense": {}, "evidence": {},
                "policy_query": {"category": "hotel", "mode": "personal_limit",
                                 "coverage": "all_regions"},
                "question": "所有地区的住宿标准都列举一下"}
        result = self.extract(args, text="所有地区的住宿标准都列举一下")
        self.assertEqual(result["status"], "ok")
        subject = result["slots"]["expense"]
        self.assertEqual(subject["values"]["destination"], "甲")
        self.assertEqual(subject["policy_query"]["coverage"], "all_regions")
        self.assertEqual(subject["policy_query"]["mode"], "personal_limit")

    def test_policy_scope_change_uses_current_request_when_question_is_omitted(self):
        args = {**self.update, "expense": {}, "evidence": {}, "question": ""}
        result = self.extract(args, text="改成综合政策说明")
        self.assertEqual(result["status"], "ok")
        tasks, _ = reconcile_tasks({self.task["id"]: self.task}, result["tasks"], result["slots"])
        self.assertEqual(tasks[self.task["id"]]["canonical_question"], "改成综合政策说明")

    def test_failed_committed_task_update_does_not_become_pending_turn_recovery(self):
        task = {**self.task, "status": "failed"}
        args = {key: value for key, value in self.update.items()
                if key not in {"expense", "evidence", "policy_query"}}
        patch = {key: value for key, value in self.update.items()
                 if key in {"expense", "evidence", "policy_query"}}
        reviewer = RawModel([review_response()])
        result = extract_slots(RawModel([
            reply("TurnRoute", {"tasks": [args]}),
            reply("ExpenseExtraction", {"patches": [{"task_index": 0, **patch}]}),
        ]), [HumanMessage("改为乙")], self.current, 1, reviewer=reviewer,
            task_context=[task], pending_tasks=[task])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["resolved_pending_turns"], [])
        reviewed = json.loads(reviewer.calls[0][-1].content)
        self.assertEqual(reviewed["mechanical_validation"], "passed")
        for key in ("task_context", "pending_tasks"):
            self.assertEqual(reviewed[key][0]["id"], task["id"])
            self.assertEqual(reviewed[key][0]["intent"], task["intent"])
            self.assertNotIn("status", reviewed[key][0])
            self.assertNotIn("attempt", reviewed[key][0])
        self.assertEqual(reviewed["pending_turns"], [])
        self.assertIsNone(reviewed["proposal"].get("recovery"))
        ledger, queue = reconcile_tasks({task["id"]: task}, result["tasks"], result["slots"])
        self.assertEqual(queue, [task["id"]])
        self.assertEqual(ledger[task["id"]]["revision"], 4)

    def test_stale_unknown_cancelled_and_inflight_references_rejected(self):
        for patch, task_patch in [
            ({"base_revision": 2}, {}), ({}, {"revision": 2}), ({"target_id": "missing"}, {}),
            ({"target_task_id": "missing"}, {}),
            ({"target_task_id": "task-a"}, {"status": "cancelled"}),
            ({}, {"status": "running"}), ({}, {"status": "awaiting_confirmation"}),
        ]:
            with self.subTest(patch=patch, task_patch=task_patch):
                result = self.extract({**self.update, **patch}, ledger=[{**self.task, **task_patch}])
                self.assertEqual(result["status"], "error")

    def test_multiple_goals_require_task_reference(self):
        ledger = [self.task, {**self.task, "id": "task-b", "intent": "policy_check"}]
        self.assertEqual(self.extract(ledger=ledger)["status"], "clarify")
        args = {**self.update, "target_task_id": "task-b"}
        args.pop("policy_query")
        result = self.extract(args, ledger=ledger)
        self.assertEqual(result["tasks"][0]["intent"], "policy_check")
        self.assertEqual(result["tasks"][0]["target_task_id"], "task-b")

    def test_goal_switch_keeps_id_invalidates_confirmation_and_dependents(self):
        args = {**self.update, "operation": "change_intent", "intent": "policy_check",
                "expense": {}, "evidence": {},
                "intent_change_evidence": {"user_message": 0, "quote": "核验实际费用"}}
        args.pop("policy_query")
        result = self.extract(args, text="核验实际费用")
        self.assertEqual(result["status"], "ok")
        subject = result["slots"]["expense"]
        self.assertEqual(subject["revision"], 4)
        self.assertIsNone(subject["result"])
        self.assertNotIn("fact_confirmation", subject)
        self.assertEqual(subject["goal_history"][0]["before"], "policy_explain")
        dependent = {**self.task, "id": "dependent", "subject_id": None, "revision": None,
                     "intent": "other", "dependencies": ["task-a"]}
        tasks, queue = reconcile_tasks({"task-a": self.task, "dependent": dependent},
                                      result["tasks"], result["slots"])
        self.assertEqual(queue, ["task-a"])
        self.assertEqual(tasks["task-a"]["intent"], "policy_check")
        self.assertEqual(tasks["dependent"]["status"], "blocked")
        self.assertIsNone(tasks["dependent"]["result_ref"])

    def test_review_rejection_and_clarification_never_commit(self):
        for verdict, status in [(review_response("revise", ["wrong target"]), "error"),
                                (review_response("clarify", question="修改哪个事项？"), "clarify")]:
            with self.subTest(status=status):
                self.assertEqual(self.extract(verdict=verdict)["status"], status)

    def test_same_goal_and_cross_domain_changes_rejected(self):
        for intent in ("policy_explain", "plan_trip"):
            with self.subTest(intent=intent):
                args = {**self.update, "operation": "change_intent", "intent": intent,
                        "intent_change_evidence": {"user_message": 0, "quote": "乙"}}
                self.assertEqual(self.extract(args)["status"], "error")

    def test_reconciliation_rechecks_reference_version(self):
        result = self.extract()
        result["tasks"][0]["base_revision"] = 1
        with self.assertRaisesRegex(ValueError, "stale_target_task_revision"):
            reconcile_tasks({"task-a": self.task}, result["tasks"], result["slots"])

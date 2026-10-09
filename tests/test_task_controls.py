"""Protocol fixtures exercise real task transitions, not natural-language classification."""

import json
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.sqlite import SqliteSaver

from business_travel.contracts import SearchResult
from business_travel.slots import SlotSubmission, SlotError, extract_slots, submission_tasks
from business_travel.tasks import TaskControlError, reconcile_tasks, dispatch_task, validate_task_controls
from tests import test_travel_graph as fixtures
from tests.test_travel_graph import call, slot_response, ScriptedModel, review_response


def control_response(action, *, choose=lambda tasks: tasks[0], overrides=None):
    def respond(messages):
        context = json.loads(messages[-1].content)
        task = choose(context["pending_tasks"])
        control = {
            "action": action, "target_id": task["id"],
            "expected_revision": task["revision"], "expected_attempt": task["attempt"],
            "expected_status": task["status"],
            "evidence": {"user_message": context["current_user_index"],
                         "quote": context["current_message"]},
            **(overrides or {}),
        }
        return call("SlotSubmission", {"tasks": [], "task_controls": [control]}, "control")
    return respond


def dependency_batch(messages):
    context = json.loads(messages[-1].content)
    quote = {"user_message": context["current_user_index"], "quote": context["current_message"]}
    return call("SlotSubmission", {"tasks": [
        {"intent": "plan_trip", "question": "查询交通",
         "trip": {"query_scope": "transport", "origin": "A", "destination": "B", "transport": "flight"},
         "evidence": {key: quote for key in ("query_scope", "origin", "destination", "transport")}},
        {"intent": "policy_explain", "question": "查询成功后解释差旅制度",
         "expense": {}, "depends_on": [0],
         "policy_query": {"category": "other", "mode": "general"}},
    ]}, "batch")


class TaskControlGraphTests(unittest.TestCase):
    setUp = fixtures.GraphTests.setUp
    tearDown = fixtures.GraphTests.tearDown
    build = fixtures.GraphTests.build

    def test_cancel_only_turn_has_answer_no_query_and_survives_smalltalk(self):
        provider = Mock()
        graph = self.build([AIMessage(content="您好")], provider=provider, slots=[
            slot_response({"destination": "B"}, "plan_trip", scope="hotel"),
            control_response("cancel"), slot_response()])
        first = graph.invoke({"messages": [HumanMessage(content="查B酒店")]}, self.config)
        ident = next(iter(first["tasks"]))
        cancelled = graph.invoke({"messages": [HumanMessage(content="取消刚才的查询任务")]}, self.config)
        self.assertIsNone(cancelled["slot_error"])
        self.assertEqual(cancelled["tasks"][ident]["status"], "cancelled")
        self.assertIn("已取消", cancelled["messages"][-1].content)
        self.assertEqual(cancelled["tasks"][ident]["attempt"], 1)
        self.assertEqual(cancelled["slots"], first["slots"])
        result = graph.invoke({"messages": [HumanMessage(content="你好")]}, self.config)
        self.assertEqual(result["tasks"][ident]["status"], "cancelled")
        provider.search.assert_not_called()

    def test_explicit_resume_after_failed_read_only_query(self):
        provider = Mock()
        provider.search.side_effect = [SearchResult(status="error"), SearchResult(status="empty")]
        graph = self.build([], provider=provider, slots=[
            slot_response({"destination": "B", "check_in": "2099-10-03",
                           "check_out": "2099-10-05"}, "plan_trip", scope="hotel"),
            control_response("resume")])
        first = graph.invoke({"messages": [HumanMessage(
            content="B酒店2099-10-03入住2099-10-05退房")]}, self.config)
        ident = next(iter(first["tasks"]))
        self.assertEqual(first["tasks"][ident]["status"], "failed")
        result = graph.invoke({"messages": [HumanMessage(content="重新查询刚才的酒店")]}, self.config)
        self.assertIsNone(result["slot_error"])
        self.assertEqual(result["tasks"][ident]["status"], "partial")
        self.assertEqual(result["tasks"][ident]["attempt"], 2)
        self.assertEqual(result["tasks"][ident]["revision"], first["tasks"][ident]["revision"])
        self.assertEqual(provider.search.call_count, 2)

    def test_acknowledgment_without_action_preserves_pending_task_and_slots(self):
        provider = Mock()
        graph = self.build([AIMessage(content="好的，保留待办，等您确定。")],
                           provider=provider, slots=[
            slot_response({"destination": "B"}, "plan_trip", scope="hotel"),
            slot_response()])
        first = graph.invoke({"messages": [HumanMessage(content="查B酒店")]}, self.config)
        ident = next(iter(first["tasks"]))
        result = graph.invoke({"messages": [HumanMessage(content="保持现状，等我决定")]}, self.config)
        self.assertIsNone(result["slot_error"])
        self.assertEqual(result["tasks"][ident], first["tasks"][ident])
        self.assertEqual(result["slots"], first["slots"])
        self.assertEqual(result["messages"][-1].content, "好的，保留待办，等您确定。")
        provider.search.assert_not_called()

    def test_dependency_completion_then_explicit_cross_turn_resume(self):
        graph = self.build([AIMessage(content="您好")], slots=[
            dependency_batch,
            slot_response({"departure_date": "2099-10-03"}, "plan_trip", update=True),
            slot_response(),
            control_response("resume", choose=lambda tasks: next(
                task for task in tasks if task["intent"] == "policy_explain"))])
        first = graph.invoke({"messages": [HumanMessage(
            content="A到B机票，查询成功后解释差旅制度")]}, self.config)
        parent, dependent = first["tasks"]
        self.assertEqual(first["tasks"][dependent]["status"], "blocked")
        second = graph.invoke({"messages": [HumanMessage(content="2099-10-03出发")]}, self.config)
        self.assertEqual(second["tasks"][parent]["status"], "completed")
        self.assertEqual(second["tasks"][dependent]["status"], "blocked")
        third = graph.invoke({"messages": [HumanMessage(content="你好")]}, self.config)
        self.assertEqual(third["tasks"][dependent]["status"], "blocked")
        result = graph.invoke({"messages": [HumanMessage(content="继续刚才的政策说明任务")]}, self.config)
        self.assertIsNone(result["slot_error"])
        self.assertNotEqual(result["tasks"][dependent]["status"], "blocked")
        self.assertEqual(result["tasks"][dependent]["attempt"], 1)
        self.assertEqual(result["tasks"][dependent]["dependencies"], [parent])

    def test_unmet_dependency_remains_blocked_on_resume(self):
        graph = self.build([], slots=[dependency_batch, control_response(
            "resume", choose=lambda tasks: tasks[1])])
        first = graph.invoke({"messages": [HumanMessage(
            content="A到B机票，查询成功后解释差旅制度")]}, self.config)
        ident = list(first["tasks"])[1]
        result = graph.invoke({"messages": [HumanMessage(content="继续政策说明")]}, self.config)
        self.assertIsNone(result["slot_error"])
        self.assertEqual(result["tasks"][ident]["status"], "blocked")
        self.assertEqual(result["tasks"][ident]["attempt"], 0)
        self.assertIn("前置任务", result["messages"][-1].content)

    def test_cancel_is_persisted_after_reopening_checkpoint(self):
        path = str(Path(self.directory.name) / "task-controls.sqlite")
        with SqliteSaver.from_conn_string(path) as saver:
            graph = self.build([], checkpointer=saver, slots=[
                slot_response({"destination": "B"}, "plan_trip", scope="hotel")])
            first = graph.invoke({"messages": [HumanMessage(content="B酒店")]}, self.config)
        ident = next(iter(first["tasks"]))
        with SqliteSaver.from_conn_string(path) as saver:
            graph = self.build([], checkpointer=saver, slots=[control_response("cancel")])
            result = graph.invoke({"messages": [HumanMessage(content="取消查询任务")]}, self.config)
            self.assertEqual(result["tasks"][ident]["status"], "cancelled")
        with SqliteSaver.from_conn_string(path) as saver:
            graph = self.build([], checkpointer=saver)
            self.assertEqual(graph.get_state(self.config).values["tasks"][ident]["status"], "cancelled")

    def test_stale_control_does_not_publish_other_slot_changes(self):
        def mixed(messages):
            context = json.loads(messages[-1].content)
            response = control_response("cancel", overrides={"expected_attempt": 99})(messages)
            response.tool_calls[0]["args"]["tasks"] = [{
                "intent": "policy_explain", "question": context["current_message"], "expense": {},
                "policy_query": {"category": "other", "mode": "general"}}]
            return response
        graph = self.build([], slots=[
            slot_response({"destination": "B"}, "plan_trip", scope="hotel"), mixed, mixed])
        first = graph.invoke({"messages": [HumanMessage(content="B酒店")]}, self.config)
        result = graph.invoke({"messages": [HumanMessage(content="取消查询并解释政策")]}, self.config)
        self.assertIsNotNone(result["slot_error"])
        self.assertEqual(result["tasks"], first["tasks"])
        self.assertEqual(result["slots"], first["slots"])

    def test_cancelling_partial_query_clears_draft_and_blocks_dependents(self):
        provider = Mock()
        provider.search.return_value = SearchResult(status="empty")
        graph = self.build([], provider=provider, slots=[
            dependency_batch,
            slot_response({"departure_date": "2099-10-03"}, "plan_trip", update=True),
            control_response("cancel", choose=lambda tasks: tasks[0]),
            control_response("resume", choose=lambda tasks: tasks[0])])
        first = graph.invoke({"messages": [HumanMessage(
            content="A到B机票，查询成功后解释差旅制度")]}, self.config)
        parent, dependent = first["tasks"]
        partial = graph.invoke({"messages": [HumanMessage(content="2099-10-03出发")]}, self.config)
        self.assertIsNotNone(partial["draft"])
        cancelled = graph.invoke({"messages": [HumanMessage(content="取消机票查询任务")]}, self.config)
        self.assertEqual(cancelled["tasks"][parent]["status"], "cancelled")
        self.assertEqual(cancelled["tasks"][dependent]["status"], "blocked")
        self.assertIsNone(cancelled["draft"])
        result = graph.invoke({"messages": [HumanMessage(content="继续政策说明")]}, self.config)
        self.assertEqual(result["tasks"][dependent]["status"], "blocked")
        self.assertEqual(result["tasks"][dependent]["attempt"], 0)
        self.assertIn("前置任务已取消", result["messages"][-1].content)
        provider.search.assert_called_once()


class TaskControlTransitionTests(unittest.TestCase):
    def ledger(self, intent="plan_trip", status="blocked"):
        return {"task": {
            "id": "task", "intent": intent, "question": "synthetic request",
            "subject_id": "subject" if intent != "other" else None,
            "revision": 1 if intent != "other" else None, "attempt": 0,
            "status": status, "dependencies": [], "missing_fields": [], "result_ref": None,
        }}

    def test_update_preserves_dependencies_and_cannot_execute_early(self):
        tasks = self.ledger()
        tasks["task"]["dependencies"] = ["parent"]
        subject = {"id": "subject", "revision": 2}
        tasks, queue = reconcile_tasks(tasks, [
            {"intent": "plan_trip", "question": "补充日期", "subject": subject}],
            {"subjects": {"subject": subject}})
        self.assertEqual(tasks["task"]["dependencies"], ["parent"])
        task, _ = dispatch_task(tasks, queue[0], {"subjects": {"subject": subject}})
        self.assertEqual(task["status"], "blocked")

    def test_cancelled_task_never_returns_to_blocked_on_revision_change(self):
        tasks = self.ledger(status="cancelled")
        updated, queue = reconcile_tasks(tasks, [], {"subjects": {
            "subject": {"id": "subject", "revision": 2}}})
        self.assertEqual(updated["task"]["status"], "cancelled")
        self.assertEqual(queue, [])

    def test_new_request_does_not_overwrite_cancelled_audit_record(self):
        tasks = self.ledger(status="cancelled")
        subject = {"id": "subject", "revision": 1}
        updated, queue = reconcile_tasks(tasks, [
            {"intent": "plan_trip", "question": "重新提出查询", "subject": subject}],
            {"subjects": {"subject": subject}})
        self.assertEqual(updated["task"], tasks["task"])
        self.assertNotEqual(queue, ["task"])

    def test_empty_submission_is_not_a_valid_noop(self):
        with self.assertRaises(SlotError):
            submission_tasks(SlotSubmission.model_validate({"tasks": []}), 8)

    def control(self, task, action="resume", **changes):
        return {"action": action, "target_id": task["id"], "expected_revision": task["revision"],
                "expected_attempt": task["attempt"], "expected_status": task["status"], **changes}

    def test_control_checks_revision_attempt_status_and_unknown_id(self):
        tasks = self.ledger()
        original = deepcopy(tasks)
        slots = {"subjects": {"subject": {"id": "subject", "revision": 1}}}
        for override in ({"expected_revision": 2}, {"expected_attempt": 2},
                         {"expected_status": "failed"}, {"target_id": "foreign-task"}):
            with self.subTest(override=override), self.assertRaises(TaskControlError):
                reconcile_tasks(tasks, [], slots, [self.control(tasks["task"], **override)])
        self.assertEqual(tasks, original)

    def test_running_confirmation_and_terminal_tasks_cannot_be_controlled(self):
        for status in ("running", "awaiting_confirmation", "completed", "cancelled"):
            for action in ("resume", "cancel"):
                tasks = self.ledger(status=status)
                with self.subTest(status=status, action=action), self.assertRaises(TaskControlError):
                    validate_task_controls(tasks, [self.control(tasks["task"], action)], {})

    def test_delegated_history_cannot_resume_but_can_cancel(self):
        tasks = self.ledger(intent="other")
        with self.assertRaisesRegex(TaskControlError, "fresh_request"):
            reconcile_tasks(tasks, [], {}, [self.control(tasks["task"])])
        updated, queue = reconcile_tasks(tasks, [], {}, [self.control(tasks["task"], "cancel")])
        self.assertEqual(updated["task"]["status"], "cancelled")
        self.assertEqual(queue, ["task"])

    def test_old_subject_cannot_resume_but_can_be_cancelled(self):
        tasks = self.ledger()
        slots = {"subjects": {"subject": {"id": "subject", "revision": 2}}}
        with self.assertRaisesRegex(TaskControlError, "subject_changed"):
            reconcile_tasks(tasks, [], slots, [self.control(tasks["task"])])
        updated, _ = reconcile_tasks(tasks, [], slots, [self.control(tasks["task"], "cancel")])
        self.assertEqual(updated["task"]["status"], "cancelled")

    def test_duplicate_control_and_control_plus_update_are_rejected(self):
        tasks = self.ledger()
        subject = {"id": "subject", "revision": 1}
        slots = {"subjects": {"subject": subject}}
        control = self.control(tasks["task"])
        with self.assertRaisesRegex(TaskControlError, "one_control_per_task"):
            reconcile_tasks(tasks, [], slots, [control, control])
        with self.assertRaisesRegex(TaskControlError, "same_subject"):
            reconcile_tasks(tasks, [{"intent": "plan_trip", "question": "update", "subject": subject}],
                            slots, [control])

    def test_selected_resumes_are_ordered_by_dependencies_not_model_order(self):
        tasks = self.ledger()
        tasks["dependent"] = {**deepcopy(tasks["task"]), "id": "dependent",
                              "subject_id": "second", "dependencies": ["task"]}
        slots = {"subjects": {
            "subject": {"id": "subject", "revision": 1}, "second": {"id": "second", "revision": 1}}}
        updated, queue = reconcile_tasks(tasks, [], slots, [
            self.control(tasks["dependent"]), self.control(tasks["task"])])
        self.assertEqual(queue, ["task", "dependent"])
        self.assertEqual(updated["dependent"]["dependencies"], ["task"])

    def test_cycle_from_reused_task_ids_is_rejected(self):
        subject = {"id": "subject", "revision": 1}
        other = {"id": "second", "revision": 1}
        with self.assertRaisesRegex(TaskControlError, "cyclic"):
            reconcile_tasks({}, [
                {"intent": "plan_trip", "question": "first", "subject": subject},
                {"intent": "plan_trip", "question": "second", "subject": other, "depends_on": [0]},
                {"intent": "plan_trip", "question": "first again", "subject": subject, "depends_on": [1]},
            ], {"subjects": {"subject": subject, "second": other}})

    def test_completed_dependency_with_stale_subject_cannot_authorize_dispatch(self):
        tasks = self.ledger(status="completed")
        tasks["dependent"] = {**deepcopy(tasks["task"]), "id": "dependent", "status": "ready",
                              "subject_id": "second", "dependencies": ["task"]}
        task, _ = dispatch_task(tasks, "dependent", {"subjects": {
            "subject": {"id": "subject", "revision": 2}, "second": {"id": "second", "revision": 1}}})
        self.assertEqual(task["status"], "blocked")
        self.assertEqual(task["attempt"], 0)


class TaskControlExtractionTests(unittest.TestCase):
    def setUp(self):
        self.task = TaskControlTransitionTests().ledger()["task"]
        self.slots = {"subjects": {"subject": {"id": "subject", "revision": 1, "values": {}}}}
        self.control = {
            **TaskControlTransitionTests().control(self.task, "cancel"),
            "evidence": {"user_message": 0, "quote": "取消查询"}}

    def extract(self, *, text="取消查询", control=None, verdict=None):
        self.reviewer = Mock(wraps=ScriptedModel([verdict or review_response()]))
        self.reviewer.bind_tools.return_value = self.reviewer
        return extract_slots(ScriptedModel([call("SlotSubmission", {
            "tasks": [], "task_controls": [control or self.control]}, "control")]),
            [HumanMessage(content=text)], self.slots, 1, reviewer=self.reviewer,
            pending_tasks=[self.task])

    def test_invalid_current_quote_and_message_index_never_reach_reviewer(self):
        for evidence in ({"user_message": 0, "quote": "不存在的原话"},
                         {"user_message": 1, "quote": "取消查询"},
                         {"user_message": 0, "quote": " "}):
            with self.subTest(evidence=evidence):
                result = self.extract(control={**self.control, "evidence": evidence})
                self.assertEqual(result["status"], "error")
                self.reviewer.invoke.assert_not_called()

    def test_negated_quote_requires_semantic_review_and_cannot_publish(self):
        original = deepcopy(self.slots)
        result = self.extract(text="不要取消查询", verdict=review_response(
            "revise", ["current user explicitly negates cancellation"]))
        self.assertEqual(result["status"], "error")
        self.assertEqual(self.slots, original)
        self.assertNotIn("task_controls", result)
        self.reviewer.invoke.assert_called_once()

    def test_ambiguous_target_clarifies_without_state_change(self):
        result = self.extract(verdict=review_response("clarify", question="要取消哪一个查询任务？"))
        self.assertEqual(result, {"status": "clarify", "question": "要取消哪一个查询任务？"})

    def test_combined_task_and_control_limit(self):
        with self.assertRaisesRegex(SlotError, "too_many_tasks"):
            submission_tasks(SlotSubmission.model_validate({
                "tasks": [{"operation": "new_task", "intent": "other"}], "task_controls": [self.control]}), 1)

    def test_reviewer_receives_control_schema_and_pre_turn_reference(self):
        result = self.extract()
        self.assertEqual(result["status"], "ok")
        payload = json.loads(self.reviewer.invoke.call_args.args[0][-1].content)
        self.assertIn("task_control_schema", payload)
        semantic_reference = {key: value for key, value in self.task.items()
                              if key not in {"status", "attempt", "revision", "result_ref"}}
        self.assertEqual(payload["pending_tasks"], [semantic_reference])
        self.assertEqual(payload["mechanical_validation"], "passed")
        self.assertEqual(payload["proposal"]["task_controls"], [self.control])


if __name__ == "__main__":
    unittest.main()

"""Task control tests use synthetic model submissions, never production answer rules."""

import json
import unittest
from pathlib import Path
from unittest.mock import Mock

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from business_travel.contracts import SearchResult
from business_travel.slots import SlotSubmission, SlotError, submission_tasks
from business_travel.tasks import reconcile_tasks, dispatch_task
from tests import test_travel_graph as fixtures
from tests.test_travel_graph import call, slot_response


class TaskLifecycleTests(unittest.TestCase):
    setUp = fixtures.GraphTests.setUp
    tearDown = fixtures.GraphTests.tearDown
    build = fixtures.GraphTests.build

    def hotel(self):
        return slot_response({"destination": "B"}, "plan_trip", scope="hotel")

    def test_clarification_survives_smalltalk_and_updates_same_task(self):
        provider = Mock()
        provider.search.return_value = SearchResult(status="empty")
        graph = self.build([AIMessage(content="您好")], provider=provider, slots=[
            self.hotel(), slot_response(),
            slot_response({"check_in": "2099-10-03", "check_out": "2099-10-05"},
                          "plan_trip", update=True)])
        first = graph.invoke({"messages": [HumanMessage(content="查B酒店")]}, self.config)
        task = next(iter(first["tasks"].values()))
        self.assertEqual(task["status"], "needs_clarification")
        self.assertEqual(set(task["missing_fields"]), {"check_in", "check_out"})
        second = graph.invoke({"messages": [HumanMessage(content="你好")]}, self.config)
        self.assertEqual(second["tasks"][task["id"]]["status"], "needs_clarification")
        third = graph.invoke({"messages": [
            HumanMessage(content="2099-10-03入住2099-10-05退房")]}, self.config)
        updated = third["tasks"][task["id"]]
        self.assertGreater(updated["revision"], task["revision"])
        self.assertEqual(updated["status"], "partial")
        self.assertEqual(updated["missing_fields"], [])
        self.assertNotIn("values", updated)
        provider.search.assert_called_once()

    def test_pending_task_targets_reach_extractor(self):
        def inspect(messages):
            payload = json.loads(messages[-1].content)
            self.assertEqual(payload["pending_tasks"][0]["missing_fields"], ["check_in", "check_out"])
            self.assertEqual(payload["pending_tasks"][0]["subject_id"],
                             payload["current_slots"]["trip"]["id"])
            return slot_response()(messages)
        graph = self.build([AIMessage(content="您好")], slots=[self.hotel(), inspect])
        graph.invoke({"messages": [HumanMessage(content="查B酒店")]}, self.config)
        result = graph.invoke({"messages": [HumanMessage(content="你好")]}, self.config)
        self.assertIsNone(result["slot_error"])

    def test_duplicate_subject_updates_reject_entire_batch(self):
        def batch(messages):
            payload = json.loads(messages[-1].content)
            current = payload["current_slots"]["trip"]
            index = payload["current_user_index"]
            def task(day, revision):
                return {"intent": "plan_trip", "action": "update", "question": day,
                        "target_id": current["id"], "base_revision": revision,
                        "trip": {"check_out": day},
                        "evidence": {"check_out": {"user_message": index, "quote": day}}}
            return call("SlotSubmission", {"tasks": [
                task("2099-10-06", current["revision"]),
                task("2099-10-07", current["revision"] + 1)]}, "batch")
        provider = Mock()
        provider.search.return_value = SearchResult(status="empty")
        graph = self.build([], provider=provider, slots=[
            slot_response({"destination": "B", "check_in": "2099-10-03", "check_out": "2099-10-05"},
                          "plan_trip", scope="hotel"), batch])
        first = graph.invoke({"messages": [HumanMessage(content="B酒店2099-10-03入住2099-10-05退房")]}, self.config)
        result = graph.invoke({"messages": [HumanMessage(
            content="先改2099-10-06，最终改2099-10-07退房")]}, self.config)
        self.assertEqual(provider.search.call_count, 1)
        self.assertIsNotNone(result["slot_error"])
        self.assertEqual(result["slots"], first["slots"])
        self.assertEqual(result["tasks"], first["tasks"])
        # Retain the existing save-safety boundary after an unaccepted correction.
        self.assertIsNone(result["draft"])

    def test_independent_failure_does_not_block_other_task(self):
        submission = call("SlotSubmission", {"tasks": [
            {"intent": "plan_trip", "question": "查询酒店", "trip": {
                "query_scope": "hotel", "destination": "B", "check_in": "2099-10-03", "check_out": "2099-10-05"},
             "evidence": {key: {"user_message": 0, "quote": value} for key, value in {
                 "query_scope": "酒店", "destination": "B", "check_in": "2099-10-03",
                 "check_out": "2099-10-05"}.items()}},
            {"intent": "other", "question": "你好"}]}, "batch")
        provider = Mock()
        provider.search.return_value = SearchResult(status="error")
        graph = self.build([AIMessage(content="您好")], provider=provider, slots=[submission])
        result = graph.invoke({"messages": [HumanMessage(
            content="B酒店2099-10-03入住2099-10-05退房，另外你好")]}, self.config)
        self.assertEqual([t["status"] for t in result["tasks"].values()], ["failed", "completed"])
        self.assertIn("您好", result["messages"][-1].content)

    def test_unmet_dependency_does_not_invoke_assistant(self):
        first = {"intent": "plan_trip", "question": "查B酒店", "trip": {
            "query_scope": "hotel", "destination": "B"}, "evidence": {
                "query_scope": {"user_message": 0, "quote": "酒店"},
                "destination": {"user_message": 0, "quote": "B"}}}
        dependent = {"intent": "other", "question": "查询成功后再总结", "depends_on": [0]}
        graph = self.build([], slots=[call("SlotSubmission", {"tasks": [first, dependent]}, "batch")])
        result = graph.invoke({"messages": [HumanMessage(
            content="查B酒店，查询成功后再总结")]}, self.config)
        self.assertEqual([t["status"] for t in result["tasks"].values()],
                         ["needs_clarification", "blocked"])
        self.assertIn("前置任务", result["messages"][-1].content)

    def test_completed_dependency_can_execute(self):
        submission = call("SlotSubmission", {"tasks": [
            {"intent": "other", "question": "你好"},
            {"intent": "other", "question": "成功后继续", "depends_on": [0]}]}, "batch")
        graph = self.build([AIMessage(content="您好"), AIMessage(content="已继续")], slots=[submission])
        result = graph.invoke({"messages": [HumanMessage(content="先问好，成功后继续")]}, self.config)
        tasks = list(result["tasks"].values())
        self.assertEqual([task["status"] for task in tasks], ["completed", "completed"])
        self.assertEqual(tasks[1]["dependencies"], [tasks[0]["id"]])

    def test_assistant_failure_is_redacted_and_independent_task_continues(self):
        def unavailable(messages):
            raise RuntimeError("synthetic-secret-must-not-leak")
        submission = call("SlotSubmission", {"tasks": [
            {"intent": "other", "question": "先问好"},
            {"intent": "other", "question": "另外再聊"}]}, "batch")
        graph = self.build([unavailable, AIMessage(content="可以继续")], slots=[submission])
        result = graph.invoke({"messages": [HumanMessage(content="先问好，另外再聊")]}, self.config)
        self.assertEqual([task["status"] for task in result["tasks"].values()], ["failed", "completed"])
        self.assertIn("可以继续", result["messages"][-1].content)
        self.assertNotIn("synthetic-secret", str(result))

    def test_task_state_survives_checkpoint_reopen(self):
        path = str(Path(self.directory.name) / "checkpoints.sqlite")
        with SqliteSaver.from_conn_string(path) as saver:
            graph = self.build([], checkpointer=saver, slots=[self.hotel()])
            first = graph.invoke({"messages": [HumanMessage(content="查B酒店")]}, self.config)
        with SqliteSaver.from_conn_string(path) as saver:
            graph = self.build([AIMessage(content="您好")], checkpointer=saver, slots=[slot_response()])
            self.assertEqual(graph.get_state(self.config).values["tasks"], first["tasks"])
            result = graph.invoke({"messages": [HumanMessage(content="你好")]}, self.config)
            ident = next(iter(first["tasks"]))
            self.assertEqual(result["tasks"][ident]["status"], "needs_clarification")

    def test_confirmation_status_and_cancel_resume(self):
        # Use a real planning draft, then exercise the existing interrupt/resume path.
        graph = self.build([call("save_trip", {}, "save"), AIMessage(content="取消了")], slots=[
            slot_response({"origin": "Beijing", "destination": "Shanghai",
                           "departure_date": "2099-09-08"}, "plan_trip"), slot_response()])
        graph.invoke({"messages": [HumanMessage(content=fixtures.PLAN_TEXT)]}, self.config)
        waiting = graph.invoke({"messages": [HumanMessage(content="保存草案")]}, self.config)
        self.assertTrue(waiting["__interrupt__"])
        task = list(waiting["tasks"].values())[-1]
        self.assertEqual(task["status"], "awaiting_confirmation")
        resumed = graph.invoke(Command(resume=False), self.config)
        self.assertEqual(resumed["tasks"][task["id"]]["status"], "cancelled")
        self.assertEqual(self.memory.read()["trip_history"], [])

    def test_invalid_dependencies_rejected_before_review(self):
        for dependencies in ([1], [2], [-1], [0, 0]):
            with self.subTest(dependencies=dependencies), self.assertRaisesRegex(
                    SlotError, "dependencies_must_reference_unique_earlier_tasks"):
                submission_tasks(SlotSubmission.model_validate({"tasks": [
                    {"operation": "new_task", "intent": "other", "question": "first"},
                    {"operation": "new_task", "intent": "other", "depends_on": dependencies,
                                          "question": "dependent"}]}), 8)

    def test_other_subject_update_does_not_consume_pending_target(self):
        graph = self.build([], slots=[self.hotel(), slot_response(
            {"destination": "C"}, "plan_trip", scope="hotel"), slot_response(
                {"check_in": "2099-10-03"}, "plan_trip", update=True)])
        first = graph.invoke({"messages": [HumanMessage(content="查B酒店")]}, self.config)
        original = next(iter(first["tasks"]))
        graph.invoke({"messages": [HumanMessage(content="另查C酒店")]}, self.config)
        result = graph.invoke({"messages": [HumanMessage(content="C在2099-10-03入住")]}, self.config)
        self.assertEqual(result["tasks"][original], first["tasks"][original])
        other = [task for key, task in result["tasks"].items() if key != original][0]
        self.assertEqual(other["missing_fields"], ["check_out"])


class TaskVersionTests(unittest.TestCase):
    def test_changes_invalidate_downstream_result_and_dispatch_rejects_old_version(self):
        subject = {"id": "subject", "revision": 1}
        proposals = [{"intent": "plan_trip", "question": "查酒店", "subject": subject},
                     {"intent": "other", "question": "总结", "subject": None, "depends_on": [0]}]
        tasks, queue = reconcile_tasks({}, proposals, {"subjects": {"subject": subject}})
        for task in tasks.values():
            task["status"] = "completed"
            task["result_ref"] = {"subject_id": "subject", "revision": 1}
        newer = {"id": "subject", "revision": 2}
        rejected, _ = dispatch_task(tasks, queue[0], {"subjects": {"subject": newer}})
        self.assertEqual(rejected["status"], "blocked")
        self.assertIsNone(rejected["result_ref"])
        updated, _ = reconcile_tasks(tasks, [
            {"intent": "plan_trip", "question": "改日期", "subject": newer}],
            {"subjects": {"subject": newer}})
        self.assertEqual(updated[queue[1]]["status"], "blocked")
        self.assertIsNone(updated[queue[1]]["result_ref"])
        self.assertEqual(tasks[queue[1]]["status"], "completed")


if __name__ == "__main__":
    unittest.main()

"""Failure injection at the real graph boundary; no real model or supplier."""

import json
import unittest
from dataclasses import replace
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver

from business_travel.graph import build_assistant
from business_travel.pending_turns import pending_context
from tests import test_travel_graph as fixtures
from tests.test_travel_graph import ScriptedModel, call, review_response, slot_response


def unavailable(_):
    raise ConnectionError("synthetic outage")


class FailedTurnRecoveryTests(unittest.TestCase):
    setUp = fixtures.GraphTests.setUp
    tearDown = fixtures.GraphTests.tearDown
    initial_text = "核验2099-12-03天津住宿137元"

    def build(self, responses, reviews=None, checkpointer=None):
        self.checked = []
        policy = Mock()
        def check(facts):
            self.checked.append(facts.model_dump(mode="json"))
            return {"status": "within_checked_rules", "amount": str(facts.amount), "approved": False}
        policy.check.side_effect = check
        models = {
            "slots": ScriptedModel(responses),
            "reviewer": ScriptedModel(reviews if reviews is not None else [review_response()] * 20),
            "supervisor": ScriptedModel([AIMessage(content="您好")] * 20),
        }
        return build_assistant(lambda role: models[role], replace(self.settings, slot_max_attempts=1),
                               self.memory, policy, checkpointer=checkpointer or InMemorySaver(),
                               provider=Mock())

    def initial(self):
        return slot_response({"kind": "hotel", "amount": 137, "expense_date": "2099-12-03",
                              "destination": "天津"},
                             "policy_check")

    def recover(self, messages):
        payload = json.loads(messages[-1].content)
        subject = payload["current_slots"]["expense"]
        return call("SlotSubmission", {
            "tasks": [{
                "operation": "update_facts", "target_id": subject["id"],
                "base_revision": subject["revision"], "expense": {"amount": 181},
                "question": "核验更正后的住宿费用",
                "evidence": {"amount": {"user_message": 1, "quote": "181"}},
            }],
            "recovery": {"action": "retry", "user_messages": [1],
                         "evidence": {"user_message": payload["current_user_index"],
                                      "quote": payload["current_message"]}},
        }, "recover")

    def test_outage_then_authorized_retry_commits_original_correction_once(self):
        graph = self.build([self.initial(), unavailable, self.recover])
        first = graph.invoke({"messages": [HumanMessage("核验2099-12-03天津住宿137元")]}, self.config)
        failed = graph.invoke({"messages": [HumanMessage("金额改为181元")]}, self.config)
        self.assertEqual(failed["slot_error"]["reason"], "slot_model_unavailable")
        self.assertEqual(failed["slots"], first["slots"])
        self.assertEqual(len(self.checked), 1)
        recovered = graph.invoke({"messages": [HumanMessage("重试刚才失败的更正")]}, self.config)
        self.assertIsNone(recovered["slot_error"])
        self.assertEqual([str(row["amount"]) for row in self.checked], ["137", "181"])
        self.assertEqual(recovered["slots"]["expense"]["id"], first["slots"]["expense"]["id"])
        self.assertEqual(recovered["slots"]["expense"]["revision"], 2)
        self.assertEqual(recovered["slots"]["expense"]["evidence"]["amount"]["user_message"], 1)
        self.assertEqual(recovered.get("pending_turns"), [])

    def fail_correction(self, graph):
        first = graph.invoke({"messages": [HumanMessage(self.initial_text)]}, self.config)
        failed = graph.invoke({"messages": [HumanMessage("金额改为181元")]}, self.config)
        self.assertIsNotNone(failed["slot_error"])
        self.assertEqual(failed["slots"], first["slots"])
        self.assertEqual(failed["tasks"], first["tasks"])
        self.assertEqual(failed["pending_turns"][0]["user_message"], 1)
        return first, failed

    def changed_recovery(self, change):
        def respond(messages):
            response = self.recover(messages)
            change(response.tool_calls[0]["args"])
            return response
        return respond

    def test_reviewer_outage_also_retains_only_raw_turn_not_untrusted_patch(self):
        proposal = slot_response({"amount": 181}, "policy_check", update=True)
        graph = self.build([self.initial(), proposal, self.recover],
                           [review_response(), unavailable, review_response()])
        first, failed = self.fail_correction(graph)
        self.assertEqual(failed["slot_error"]["reason"], "semantic_review_unavailable")
        self.assertNotIn("values", failed["pending_turns"][0])
        recovered = graph.invoke({"messages": [HumanMessage("重试刚才更正")]}, self.config)
        self.assertIsNone(recovered["slot_error"])
        self.assertEqual(recovered["slots"]["expense"]["values"]["amount"], "181")
        self.assertEqual(len(self.checked), 2)

    def test_smalltalk_does_not_apply_or_discard_pending_correction(self):
        graph = self.build([self.initial(), unavailable, slot_response(), self.recover])
        _, failed = self.fail_correction(graph)
        chatted = graph.invoke({"messages": [HumanMessage("你好")]}, self.config)
        self.assertEqual(chatted["slots"], failed["slots"])
        self.assertEqual(chatted["pending_turns"], failed["pending_turns"])
        self.assertEqual(len(self.checked), 1)
        recovered = graph.invoke({"messages": [HumanMessage("重试之前那次金额更正")]}, self.config)
        self.assertIsNone(recovered["slot_error"])
        self.assertEqual(len(self.checked), 2)

    def test_historical_fact_without_recovery_is_still_rejected(self):
        graph = self.build([self.initial(), unavailable,
                           self.changed_recovery(lambda args: args.pop("recovery"))])
        first, _ = self.fail_correction(graph)
        result = graph.invoke({"messages": [HumanMessage("继续")]}, self.config)
        self.assertIn("cite_exact_current_message", str(result["slot_error"]))
        self.assertEqual(result["slots"], first["slots"])
        self.assertEqual(len(self.checked), 1)

    def test_recovery_requires_current_exact_quote_and_pending_indices(self):
        mutations = [
            lambda args: args["recovery"]["evidence"].update(user_message=1, quote="181"),
            lambda args: args["recovery"]["evidence"].update(quote="并不存在的授权"),
            lambda args: args["recovery"].update(user_messages=[0]),
            lambda args: args["recovery"].update(user_messages=[1, 1]),
            lambda args: args["recovery"].update(user_messages=[2]),
            lambda args: args["tasks"][0]["evidence"]["amount"].update(user_message=0, quote="137"),
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.config = {"configurable": {"thread_id": str(id(mutation))}}
                graph = self.build([self.initial(), unavailable, self.changed_recovery(mutation)])
                first, _ = self.fail_correction(graph)
                result = graph.invoke({"messages": [HumanMessage("重试更正")]}, self.config)
                self.assertIsNotNone(result["slot_error"])
                self.assertEqual(result["slots"], first["slots"])
                self.assertEqual(len(self.checked), 1)

    def test_negative_authorization_requires_review_not_substring_matching(self):
        graph = self.build([self.initial(), unavailable, self.recover],
                           [review_response(), review_response("revise", ["用户明确禁止重试"])])
        first, _ = self.fail_correction(graph)
        result = graph.invoke({"messages": [HumanMessage("不要重试刚才失败的更正")]}, self.config)
        self.assertIsNotNone(result["slot_error"])
        self.assertEqual(result["slots"], first["slots"])
        self.assertEqual(len(self.checked), 1)

    def test_latest_current_correction_wins_over_selected_older_text(self):
        def newer(args):
            args["tasks"][0]["expense"]["amount"] = 229
            args["tasks"][0]["evidence"]["amount"] = {"user_message": 2, "quote": "229"}
        graph = self.build([self.initial(), unavailable, self.changed_recovery(newer)])
        self.fail_correction(graph)
        result = graph.invoke({"messages": [HumanMessage("重试，但金额以229元为准")]}, self.config)
        self.assertIsNone(result["slot_error"])
        self.assertEqual(result["slots"]["expense"]["values"]["amount"], "229")
        self.assertEqual(result["slots"]["expense"]["evidence"]["amount"]["user_message"], 2)

    def test_current_retraction_can_override_pending_value(self):
        def retract(args):
            args["tasks"][0]["expense"]["amount"] = None
            args["tasks"][0]["evidence"]["amount"] = {"user_message": 2, "quote": "金额不确定"}
        graph = self.build([self.initial(), unavailable, self.changed_recovery(retract)])
        self.fail_correction(graph)
        result = graph.invoke({"messages": [HumanMessage("继续那次更正，但金额不确定")]}, self.config)
        self.assertIsNone(result["slot_error"])
        self.assertIsNone(result["slots"]["expense"]["values"]["amount"])

    def test_stale_pending_cannot_overwrite_subsequently_committed_facts(self):
        graph = self.build([self.initial(), unavailable,
                           slot_response({"amount": 229}, "policy_check", update=True), self.recover])
        self.fail_correction(graph)
        latest = graph.invoke({"messages": [HumanMessage("这笔金额现在改为229元")]}, self.config)
        result = graph.invoke({"messages": [HumanMessage("再重试原来失败的请求")]}, self.config)
        self.assertEqual(result["slot_error"]["status"], "clarify")
        self.assertEqual(result["slots"], latest["slots"])
        self.assertEqual(len(self.checked), 2)

    def test_discard_only_clears_marker_without_dispatching_or_deleting_history(self):
        def discard(messages):
            payload = json.loads(messages[-1].content)
            return call("SlotSubmission", {"tasks": [], "recovery": {
                "action": "discard", "user_messages": [1],
                "evidence": {"user_message": payload["current_user_index"], "quote": "放弃失败的更正"}}}, "d")
        graph = self.build([self.initial(), unavailable, discard])
        first, _ = self.fail_correction(graph)
        result = graph.invoke({"messages": [HumanMessage("放弃失败的更正")]}, self.config)
        self.assertIsNone(result["slot_error"])
        self.assertEqual(result["pending_turns"], [])
        self.assertEqual(result["slots"], first["slots"])
        self.assertEqual(result["tasks"], first["tasks"])
        self.assertEqual(len(self.checked), 1)
        self.assertIn("金额改为181元", [m.content for m in result["messages"]])
        self.assertIn("已放弃", result["messages"][-1].content)

    def test_recovery_never_replays_delegated_write(self):
        def delegated(args):
            args["tasks"] = [{"operation": "new_task", "intent": "other", "question": "记住我的偏好"}]
        graph = self.build([self.initial(), unavailable, self.changed_recovery(delegated)])
        first, _ = self.fail_correction(graph)
        result = graph.invoke({"messages": [HumanMessage("重试")]}, self.config)
        self.assertIn("recovery_business_facts_only", str(result["slot_error"]))
        self.assertEqual(result["slots"], first["slots"])
        self.assertEqual(self.memory.read()["preferences"], [])

    def test_second_outage_keeps_original_pending_turn_available(self):
        graph = self.build([self.initial(), unavailable, unavailable, self.recover])
        self.fail_correction(graph)
        again = graph.invoke({"messages": [HumanMessage("重试金额更正")]}, self.config)
        self.assertEqual([entry["user_message"] for entry in again["pending_turns"]], [1, 2])
        recovered = graph.invoke({"messages": [HumanMessage("重试最初金额更正")]}, self.config)
        self.assertIsNone(recovered["slot_error"])
        self.assertEqual(len(self.checked), 2)
        self.assertNotIn(1, [entry["user_message"] for entry in recovered["pending_turns"]])

    def test_pending_context_survives_short_conversation_window(self):
        seen = []
        def inspect_review(messages):
            payload = json.loads(messages[-1].content)
            seen.append(payload)
            return review_response()
        self.settings = replace(self.settings, slot_context_limit=1)
        graph = self.build([self.initial(), unavailable, self.recover],
                           [review_response(), inspect_review])
        self.fail_correction(graph)
        result = graph.invoke({"messages": [HumanMessage("重试之前金额更正")]}, self.config)
        self.assertIsNone(result["slot_error"])
        self.assertEqual(seen[0]["pending_turns"][0]["content"], "金额改为181元")
        self.assertEqual(len(seen[0]["conversation"]), 1)

    def test_session_isolation_blocks_pending_reference_from_another_thread(self):
        graph = self.build([self.initial(), unavailable, self.recover])
        self.fail_correction(graph)
        # A separate graph shares the saver, but the other thread has no pending turn.
        def other_submission(messages):
            return call("SlotSubmission", {"tasks": [{
                "operation": "new_task", "intent": "policy_check", "expense": {"kind": "hotel"}}],
                "recovery": {"action": "retry", "user_messages": [1],
                             "evidence": {"user_message": 0, "quote": "重试"}}}, "foreign")
        other = self.build([other_submission], checkpointer=graph.checkpointer)
        result = other.invoke({"messages": [HumanMessage("重试")]},
                              {"configurable": {"thread_id": "other-session"}})
        self.assertIsNotNone(result["slot_error"])
        self.assertFalse(result.get("slots"))

    def test_checkpoint_reopen_preserves_pending_original_evidence(self):
        path = str(Path(self.directory.name) / "recovery.sqlite")
        with SqliteSaver.from_conn_string(path) as saver:
            graph = self.build([self.initial(), unavailable], checkpointer=saver)
            self.fail_correction(graph)
        with SqliteSaver.from_conn_string(path) as saver:
            graph = self.build([self.recover], checkpointer=saver)
            result = graph.invoke({"messages": [HumanMessage("重试刚才金额更正")]}, self.config)
        self.assertIsNone(result["slot_error"])
        self.assertEqual(result["slots"]["expense"]["values"]["amount"], "181")
        self.assertEqual(len(self.checked), 1)

    def test_replaced_message_and_changed_task_status_disable_recovery(self):
        graph = self.build([self.initial(), unavailable])
        _, failed = self.fail_correction(graph)
        messages = [*failed["messages"], HumanMessage("重试")]
        original = pending_context(failed["pending_turns"], messages, failed["slots"], 16, failed["tasks"])
        self.assertTrue(original[0]["retryable"])
        modified = deepcopy(messages)
        next(m for m in modified if isinstance(m, HumanMessage) and m.content == "金额改为181元").content = "改为999"
        self.assertEqual(pending_context(failed["pending_turns"], modified, failed["slots"], 16, failed["tasks"]), [])
        tasks = deepcopy(failed["tasks"])
        next(iter(tasks.values()))["status"] = "cancelled"
        stale = pending_context(failed["pending_turns"], messages, failed["slots"], 16, tasks)
        self.assertFalse(stale[0]["retryable"])

    def test_initial_request_outage_can_create_subject_from_authorized_pending_text(self):
        def retry_initial(messages):
            payload = json.loads(messages[-1].content)
            return call("SlotSubmission", {"tasks": [{
                "operation": "new_task", "intent": "policy_check",
                "expense": {"kind": "hotel", "amount": 137, "destination": "天津",
                            "expense_date": "2099-12-03"},
                "evidence": {field: {"user_message": 0, "quote": value} for field, value in
                             {"amount": "137", "destination": "天津", "expense_date": "2099-12-03"}.items()},
            }], "recovery": {"action": "retry", "user_messages": [0],
                "evidence": {"user_message": payload["current_user_index"], "quote": "重试第一次请求"}}}, "r")
        graph = self.build([unavailable, retry_initial])
        failed = graph.invoke({"messages": [HumanMessage(self.initial_text)]}, self.config)
        self.assertFalse(failed.get("slots"))
        self.assertFalse(self.checked)
        result = graph.invoke({"messages": [HumanMessage("重试第一次请求")]}, self.config)
        self.assertIsNone(result["slot_error"])
        self.assertEqual(result["slots"]["expense"]["revision"], 1)
        self.assertEqual(result["slots"]["expense"]["evidence"]["amount"]["user_message"], 0)
        self.assertEqual(len(result["tasks"]), 1)
        self.assertEqual(len(self.checked), 1)
        self.assertEqual(result["pending_turns"], [])

    def test_multiple_subjects_recover_only_explicitly_selected_failed_turn(self):
        def recover_first(messages):
            payload = json.loads(messages[-1].content)
            subject = next(s for s in payload["current_slots"]["subjects"].values()
                           if s["values"]["destination"] == "天津")
            return call("SlotSubmission", {"tasks": [{
                "operation": "update_facts", "target_id": subject["id"],
                "base_revision": subject["revision"], "expense": {"amount": 181},
                "evidence": {"amount": {"user_message": 2, "quote": "181"}}}],
                "recovery": {"action": "retry", "user_messages": [2],
                    "evidence": {"user_message": 4, "quote": "只重试天津那笔"}}}, "r")
        graph = self.build([self.initial(),
            slot_response({"kind": "hotel", "amount": 229, "destination": "苏州",
                           "expense_date": "2099-12-04"}, "policy_check"),
            unavailable, unavailable, recover_first])
        graph.invoke({"messages": [HumanMessage(self.initial_text)]}, self.config)
        second = graph.invoke({"messages": [HumanMessage("另核验2099-12-04苏州住宿229元")]}, self.config)
        untouched_subject = deepcopy(second["slots"]["expense"])
        untouched_task = deepcopy(next(t for t in second["tasks"].values()
                                       if t["subject_id"] == untouched_subject["id"]))
        graph.invoke({"messages": [HumanMessage("天津那笔改为181元")]}, self.config)
        graph.invoke({"messages": [HumanMessage("苏州那笔改为257元")]}, self.config)
        result = graph.invoke({"messages": [HumanMessage("只重试天津那笔")]}, self.config)
        self.assertIsNone(result["slot_error"])
        self.assertEqual(result["slots"]["expense"]["values"]["amount"], "181")
        self.assertEqual(result["slots"]["subjects"][untouched_subject["id"]], untouched_subject)
        self.assertEqual(result["tasks"][untouched_task["id"]], untouched_task)
        self.assertEqual([entry["user_message"] for entry in result["pending_turns"]], [3])
        self.assertEqual([row["destination"] for row in self.checked], ["天津", "苏州", "天津"])

    def test_ambiguous_recovery_review_requests_clarification_without_committing(self):
        graph = self.build([self.initial(), unavailable, self.recover],
            [review_response(), review_response("clarify", question="请确认要重试哪次失败请求？")])
        first, failed = self.fail_correction(graph)
        result = graph.invoke({"messages": [HumanMessage("继续那个")]}, self.config)
        self.assertEqual(result["slot_error"]["status"], "clarify")
        self.assertEqual(result["slots"], first["slots"])
        self.assertEqual(result["tasks"], first["tasks"])
        self.assertEqual(result["pending_turns"][0], failed["pending_turns"][0])
        self.assertEqual(len(self.checked), 1)


if __name__ == "__main__":
    unittest.main()

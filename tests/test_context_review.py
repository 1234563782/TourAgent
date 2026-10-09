"""Protocol tests, not evidence that a real model understands unseen language."""

import json
import unittest
from copy import deepcopy
from unittest.mock import Mock

from langchain_core.messages import AIMessage, HumanMessage

from business_travel.slots import extract_slots
from tests.test_travel_graph import ScriptedModel, call, slot_response
from tests import test_travel_graph as fixtures
from business_travel.graph import build_assistant
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command


def review(verdict="accept", issues=None, question=""):
    return call("SemanticReview", {"verdict": verdict, "issues": issues or [],
                                  "question": question}, "review")


class ContextReviewTests(unittest.TestCase):
    def test_reviewer_rejects_negation_cut_from_full_message(self):
        proposal = {"intent": "policy_check", "expense": {"special_approval": True},
                    "evidence": {"special_approval": {"user_message": 0, "quote": "有专项审批"}}}
        seen = []
        def reject(messages):
            seen.append(json.loads(messages[-1].content))
            return review("revise", ["expense.special_approval contradicts the full user message"])
        result = extract_slots(ScriptedModel([call("SlotSubmission", proposal, "s")]),
                               [HumanMessage(content="没有专项审批")], {}, 1,
                               reviewer=ScriptedModel([reject]))
        self.assertEqual(result["status"], "error")
        self.assertNotIn("slots", result)
        self.assertIn("没有专项审批", json.dumps(seen, ensure_ascii=False))

    def test_reviewer_rejects_wrong_meal_attribution(self):
        text = "早餐午餐自理，晚餐免费提供"
        proposal = {"intent": "policy_check", "expense": {"covered_meals": ["breakfast", "lunch"]},
                    "evidence": {"covered_meals": {"user_message": 0, "quote": text}}}
        result = extract_slots(ScriptedModel([call("SlotSubmission", proposal, "s")]),
                               [HumanMessage(content=text)], {}, 1,
                               reviewer=ScriptedModel([review("revise", ["free meals assigned to wrong meals"])]))
        self.assertEqual(result["status"], "error")

    def test_pronoun_resolution_receives_assistant_question(self):
        seen = []
        def propose(messages):
            seen.append(json.loads(messages[-1].content))
            return slot_response({"special_approval": False}, "policy_check",
                                 {"special_approval": "没有"})(messages)
        messages = [HumanMessage(content="帮我核对费用"), AIMessage(content="有专项审批吗？"),
                    HumanMessage(content="没有")]
        result = extract_slots(ScriptedModel([propose]), messages, {}, 1,
                               reviewer=ScriptedModel([review()]))
        self.assertEqual(result["slots"]["expense"]["values"]["special_approval"], False)
        self.assertIn("有专项审批吗", json.dumps(seen, ensure_ascii=False))

    def test_unavailable_reviewer_cannot_commit(self):
        current = {}
        before = deepcopy(current)
        result = extract_slots(ScriptedModel([slot_response({}, "policy_check")]),
                               [HumanMessage(content="核对费用")], current, 1,
                               reviewer=ScriptedModel([]))
        self.assertEqual(result["status"], "error")
        self.assertEqual(current, before)

    def test_semantic_rejection_can_be_repaired_with_bounded_retry(self):
        result = extract_slots(
            ScriptedModel([slot_response({"special_approval": True}, "policy_check",
                                        {"special_approval": "获批"}),
                           slot_response({"special_approval": False}, "policy_check",
                                         {"special_approval": "还未获批"})]),
            [HumanMessage(content="专项审批还未获批")], {}, 2,
            reviewer=ScriptedModel([review("revise", ["negation omitted"]), review()]))
        self.assertFalse(result["slots"]["expense"]["values"]["special_approval"])

    def test_same_name_tool_results_are_not_overwritten(self):
        from langchain_core.messages import ToolMessage
        from business_travel.graph import finalize_business_reply
        messages = [HumanMessage(content="核对两笔费用")]
        for ident, amount in [("first", "137"), ("second", "269")]:
            messages.append(ToolMessage(name="consult_policy", tool_call_id=ident,
                content=json.dumps({"rule_checks": [{"facts": {"kind": "hotel"},
                    "result": {"status": "within_checked_rules", "amount": amount, "currency": "CNY"}}]})))
        answer = finalize_business_reply(messages, AIMessage(content="")).content
        self.assertIn("137", answer)
        self.assertIn("269", answer)

    def test_absent_reviewer_fails_closed(self):
        result = extract_slots(ScriptedModel([]), [HumanMessage(content="核对费用")], {}, 1)
        self.assertEqual(result["reason"], "semantic_reviewer_required")

    def test_review_clarification_preserves_state_and_question(self):
        current = {}
        result = extract_slots(ScriptedModel([slot_response({}, "policy_check")]),
            [HumanMessage(content="那一笔改一下")], current, 1,
            reviewer=ScriptedModel([review("clarify", question="您指的是哪一笔费用？")]))
        self.assertEqual(result, {"status": "clarify", "question": "您指的是哪一笔费用？"})
        self.assertEqual(current, {})

    def test_invalid_review_responses_cannot_commit(self):
        for response in [AIMessage(content='{"verdict":"accept"}'), review("accept", ["conflicting"]),
                         review("revise"), review("clarify")]:
            with self.subTest(response=response):
                result = extract_slots(ScriptedModel([slot_response({}, "policy_check")]),
                    [HumanMessage(content="核对费用")], {}, 1, reviewer=ScriptedModel([response]))
                self.assertEqual(result["reason"], "semantic_review_unavailable")

    def test_rejection_of_one_task_rejects_whole_batch(self):
        tasks = [{"intent": "policy_check", "question": str(i), "expense": {"kind": "hotel"}}
                 for i in range(2)]
        current = {}
        result = extract_slots(ScriptedModel([call("SlotSubmission", {"tasks": tasks}, "s")]),
            [HumanMessage(content="两笔费用")], current, 1,
            reviewer=ScriptedModel([review("revise", ["second task unsupported"])]))
        self.assertEqual(result["status"], "error")
        self.assertEqual(current, {})
        self.assertNotIn("slots", result)

    def test_task_count_limit_is_enforced(self):
        tasks = [{"intent": "other", "question": str(i)} for i in range(3)]
        result = extract_slots(ScriptedModel([call("SlotSubmission", {"tasks": tasks}, "s")]),
            [HumanMessage(content="组合问题")], {}, 1, reviewer=ScriptedModel([]), task_limit=2)
        self.assertIn("too_many_tasks", str(result))


class MultiTaskGraphTests(unittest.TestCase):
    setUp = fixtures.GraphTests.setUp
    tearDown = fixtures.GraphTests.tearDown

    def make_graph(self, slots, supervisor=(), reviews=None):
        self.checked = []
        policy = Mock()
        def check(facts):
            self.checked.append(facts.model_dump(mode="json"))
            return {"status": "within_checked_rules", "amount": str(facts.amount), "approved": False}
        policy.check.side_effect = check
        models = {"slots": ScriptedModel(slots), "supervisor": ScriptedModel(supervisor),
                  "reviewer": ScriptedModel(reviews if reviews is not None else [review()] * 10)}
        return build_assistant(lambda role: models[role], self.settings, self.memory, policy,
                               checkpointer=InMemorySaver(), provider=fixtures.FixtureProvider())

    @staticmethod
    def expense(amount, city, question):
        facts = {"kind": "hotel", "expense_date": "2099-12-03", "destination": city, "amount": amount}
        return {"intent": "policy_check", "question": question, "expense": facts,
                "evidence": {key: {"user_message": 0, "quote": str(value)}
                             for key, value in facts.items() if key != "kind"}}

    def test_two_expenses_same_date_preserved_and_archived_subject_updated(self):
        tasks = [self.expense(137, "天津", "天津住宿"), self.expense(269, "苏州", "苏州住宿")]
        def update_first(messages):
            context = json.loads(messages[-1].content)
            old = next(s for s in context["current_slots"]["subjects"].values()
                       if s["values"]["destination"] == "天津")
            return call("SlotSubmission", {"intent": "policy_check", "action": "update",
                "target_id": old["id"], "base_revision": old["revision"], "expense": {"amount": 181},
                "evidence": {"amount": {"user_message": 1, "quote": "181"}}}, "u")
        graph = self.make_graph([call("SlotSubmission", {"tasks": tasks}, "s"), update_first])
        result = graph.invoke({"messages": [HumanMessage(content="2099-12-03天津137，苏州269")]}, self.config)
        self.assertEqual([f["amount"] for f in self.checked], ["137", "269"])
        self.assertIn("137", result["messages"][-1].content)
        self.assertIn("269", result["messages"][-1].content)
        self.assertEqual(len(result["slots"]["subjects"]), 2)
        for subject in result["slots"]["subjects"].values():
            self.assertIsNotNone(subject["result"])
        result = graph.invoke({"messages": [HumanMessage(content="天津那一笔改为181")]}, self.config)
        self.assertEqual(self.checked[-1]["destination"], "天津")
        self.assertEqual(self.checked[-1]["amount"], "181")
        suzhou = next(s for s in result["slots"]["subjects"].values() if s["values"]["destination"] == "苏州")
        self.assertEqual(suzhou["values"]["amount"], "269")

    def test_business_then_ordinary_question_both_answered(self):
        tasks = [self.expense(137, "天津", "核对天津住宿"),
                 {"intent": "other", "question": "打个招呼"}]
        graph = self.make_graph([call("SlotSubmission", {"tasks": tasks}, "s")],
                                [AIMessage(content="您好，很高兴为您服务。")])
        result = graph.invoke({"messages": [HumanMessage(content="2099-12-03天津137，打个招呼")]}, self.config)
        self.assertEqual(len(self.checked), 1)
        self.assertIn("137", result["messages"][-1].content)
        self.assertIn("您好", result["messages"][-1].content)

    def test_automatic_preference_save_does_not_repeat_earlier_policy(self):
        preferences = [{"type": "hotel", "value": "安静"}]
        tasks = [self.expense(137, "天津", "核对天津住宿"),
                 {"intent": "other", "question": "记住我喜欢安静酒店"}]
        graph = self.make_graph([call("SlotSubmission", {"tasks": tasks}, "s")],
            [call("save_preferences", {"preferences": preferences, "context": {
                "subject": "self", "scope": "long_term", "assertion": "actual",
                "quote": "记住我喜欢安静酒店"}}, "save"), AIMessage(content="已保存")],
            reviews=[review(), call("PreferenceReview",
                {"decision": "accept", "quote": "记住我喜欢安静酒店"}, "review-pref")])
        result = graph.invoke({"messages": [HumanMessage(
            content="2099-12-03天津137，记住我喜欢安静酒店")]}, self.config)
        self.assertNotIn("__interrupt__", result)
        self.assertEqual(len(self.checked), 1)
        self.assertEqual(self.memory.read()["preferences"], preferences)
        self.assertIn("137", result["messages"][-1].content)
        self.assertIn("已记住", result["messages"][-1].content)

    def test_clarification_makes_no_policy_call(self):
        graph = self.make_graph([slot_response({}, "policy_check")],
            reviews=[review("clarify", question="您要调整天津还是苏州的住宿？")])
        result = graph.invoke({"messages": [HumanMessage(content="那笔住宿改一下")]}, self.config)
        self.assertEqual(result["messages"][-1].content, "您要调整天津还是苏州的住宿？")
        self.assertEqual(self.checked, [])

"""Jev adapter tests use synthetic HTTP responses and never call the real service."""

import unittest
import json
from itertools import combinations
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import httpx
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from business_travel.jev import INTENT_QUESTIONS, TASK_SCOPES, JevClient, JevDecision
from business_travel.slots import TaskClarification, build_clarification_route, extract_slots, propose_slots
from business_travel.settings import Settings
from tests.test_slot_protocol import RawModel, reply
from tests import test_travel_graph as graph_fixtures


def settings(**changes):
    with TemporaryDirectory() as directory:
        base = Settings(api_key="", model="", base_url="", node_path="", flyai_cli_path="",
                        storage_dir=Path(directory) / "new", legacy_memory_dir=Path(directory) / "old",
                        jev_enabled=True, jev_api_key="synthetic-key")
        return replace(base, **changes)


def multi_decision(*intents, overrides=None, threshold=Settings.jev_intent_threshold):
    """Scripted decisions test routing contracts, not real model understanding."""
    answers = {key: {"type": "noul", "noul": 0.99 if intent in intents else 0.01}
               for intent, key in INTENT_QUESTIONS.items()}
    answers["requires_task_routing"] = {"type": "noul", "noul": 0.01}
    answers.update(overrides or {})
    return JevDecision("ok", answers, {}, intent_threshold=threshold)


def empty_state(text="查南京酒店，并解释住宿标准"):
    return {"user_message": text, "current_slots": {"subjects": {}},
            "task_context": [], "pending_tasks": [], "pending_turns": []}


def extraction_payload(text, decision):
    return {"today": "2099-01-01", "current_user_index": 0,
            "current_message": text, "current_slots": {"subjects": {}},
            "conversation": [{"role": "human", "content": text, "user_message": 0}],
            "task_limit": 8, "pending_tasks": [], "pending_turns": [], "task_context": [],
            "jev_hint": decision.hint(), "jev_route": decision.primary_route(empty_state(text))}


class JevTests(unittest.TestCase):
    def test_hint_accepts_only_known_values_and_nested_answers(self):
        client = JevClient(settings())
        decision = client.classify
        self.assertTrue(callable(decision))

        result = JevDecision("ok", {
            "intent": {"value": "plan_trip"},
            "query_scope": "hotel",
            "transport": "flight",
            "journey": "round_trip",
        }, {})
        self.assertEqual(result.hint(), {"intent": "plan_trip"})

    def test_primary_route_only_applies_to_an_empty_first_turn(self):
        result = JevDecision("ok", {"intent": "plan_trip"}, {})
        route = result.primary_route({
            "user_message": "查明天去上海的机票",
            "current_slots": {"subjects": {}},
            "task_context": [],
            "pending_tasks": [],
        })
        self.assertEqual(route["tasks"][0]["operation"], "new_task")
        self.assertEqual(route["tasks"][0]["intent"], "plan_trip")

        self.assertIsNone(result.primary_route({
            "user_message": "改成后天去上海",
            "current_slots": {"trip": {"id": "subject-1"}},
            "task_context": [],
            "pending_tasks": [],
        }))
        self.assertIsNone(result.primary_route({
            "user_message": "继续查",
            "current_slots": {"subjects": {}},
            "task_context": [{"id": "task-1"}],
            "pending_tasks": [],
        }))

    def test_primary_route_preserves_jev_policy_intent(self):
        result = JevDecision("ok", {"intent": "policy_check"}, {})
        route = result.primary_route({
            "user_message": "我是院士去北京出差，住宿标准是多少",
            "current_slots": {"subjects": {}},
            "task_context": [],
            "pending_tasks": [],
        })
        self.assertEqual(route["tasks"][0]["intent"], "policy_check")

    def test_explicit_reimbursement_check_stays_policy_check(self):
        result = JevDecision("ok", {"intent": "policy_check"}, {})
        route = result.primary_route({
            "user_message": "我已经花了500元住宿费，这笔费用能不能报销",
            "current_slots": {"subjects": {}},
            "task_context": [],
            "pending_tasks": [],
        })
        self.assertEqual(route["tasks"][0]["intent"], "policy_check")

    def test_primary_route_does_not_rewrite_general_policy_question(self):
        result = JevDecision("ok", {"intent": "policy_check"}, {})
        route = result.primary_route({
            "user_message": "学校报销标准是怎么规定的",
            "current_slots": {"subjects": {}},
            "task_context": [],
            "pending_tasks": [],
        })
        self.assertEqual(route["tasks"][0]["intent"], "policy_check")

    def test_valid_jev_route_skips_turn_route_model_call(self):
        class ModelMustNotBeCalled:
            def bind_tools(self, *args, **kwargs):
                raise AssertionError("TurnRoute model call should be skipped")

        route = {
            "tasks": [{
                "operation": "new_task",
                "intent": "other",
                "question": "你好",
                "depends_on": [],
            }],
            "task_controls": [],
            "recovery": None,
            "clarification": "",
        }
        payload = {
            "today": "2099-01-01",
            "current_user_index": 0,
            "current_message": "你好",
            "current_slots": {"subjects": {}},
            "conversation": [{"role": "human", "content": "你好", "user_message": 0}],
            "task_limit": 8,
            "pending_tasks": [],
            "pending_turns": [],
            "task_context": [],
            "jev_hint": {"intent": "other"},
            "jev_route": route,
        }
        submission, route_payload, schemas, _ = propose_slots(
            ModelMustNotBeCalled(), [HumanMessage(content="你好")], {}, payload)
        self.assertEqual(submission.tasks[0].intent, "other")
        self.assertIs(route_payload, payload)
        self.assertEqual(schemas, {})

    @patch("business_travel.jev.httpx.post")
    def test_classify_sends_bearer_request_and_preserves_usage(self, post):
        post.return_value = httpx.Response(200, request=httpx.Request("POST", "https://jev.test"),
                                            json={
            "answers": {"intent": "policy_check"},
            "model": "jev-test",
            "model_version": "v1",
            "usage": {"input_tokens": 12, "output_tokens": 3, "charged_tokens": 15},
        })
        result = JevClient(settings()).classify({"user_message": "核对报销"}, request_id="request-1")
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.usage["total_tokens"], 15)
        self.assertEqual(result.hint(), {"intent": "policy_check"})
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer synthetic-key")
        self.assertEqual(post.call_args.kwargs["headers"]["Idempotency-Key"], "request-1")
        self.assertEqual(post.call_args.kwargs["json"]["state"]["user_message"], "核对报销")

    @patch("business_travel.jev.httpx.post")
    def test_classify_sends_disambiguated_policy_intent_definitions(self, post):
        post.return_value = httpx.Response(200, request=httpx.Request("POST", "https://jev.test"),
                                            json={"answers": {"intent": "policy_explain"}})

        JevClient(settings()).classify({"user_message": "北京住宿标准是多少"})

        questions = post.call_args.kwargs["json"]["questions"]
        self.assertEqual(list(questions), [*INTENT_QUESTIONS.values(), "requires_task_routing"])
        self.assertNotIn("intent", questions)
        for dropped in ("query_scope", "transport", "journey"):
            self.assertNotIn(dropped, questions)
        check = questions["has_policy_check"]
        explain = questions["has_policy_explain"]
        self.assertIn("具体费用", check["criteria"]["true"])
        self.assertIn("能否报销", check["criteria"]["true"])
        self.assertIn("事实不完整", check["criteria"]["true"])
        self.assertIn("明确假设", check["criteria"]["true"])
        self.assertIn("标准、限额、适用条件", explain["criteria"]["true"])
        self.assertIn("不能仅凭关键词", explain["criteria"]["true"])
        self.assertIn("不构成独立解释目标", explain["criteria"]["true"])
        self.assertIn("不能只选主要目标", check["instructions"])
        self.assertIn("否定、引用", explain["instructions"])
        for key in (*INTENT_QUESTIONS.values(), "requires_task_routing"):
            self.assertEqual(questions[key]["type"], "noul")
            self.assertEqual(set(questions[key]["criteria"]), {"true", "false"})
        for intent, key in INTENT_QUESTIONS.items():
            # The API does not send question IDs or sibling answers to the model.
            self.assertIn(TASK_SCOPES[intent], questions[key]["instructions"])
            self.assertIn("`user_message`", questions[key]["instructions"])
            self.assertIn("不自动产生 other", questions[key]["instructions"])
        other = questions["has_other_request"]
        self.assertIn("可以与业务目标同时存在", other["criteria"]["true"])
        self.assertIn("包括但不限于", other["criteria"]["true"])
        self.assertIn("查机票并查天气", other["criteria"]["true"])
        self.assertIn("保存长期偏好", other["criteria"]["true"])
        self.assertIn("保存当前出差", other["criteria"]["true"])
        self.assertIn("取消、恢复、重试或丢弃未提交回合不是 other", other["criteria"]["true"])
        self.assertNotIn("取消、恢复或重试当前出差和行程事项", other["criteria"]["true"])
        self.assertIn("已有结果解读仅指理解该结果", other["criteria"]["true"])
        self.assertIn("单独寒暄", other["criteria"]["true"])
        self.assertIn("查机票，另外解释学校住宿报销标准", other["criteria"]["false"])
        self.assertIn("即使同时存在多个业务目标，也应为假", other["criteria"]["false"])
        self.assertIn("只取消、只恢复、只重试或只丢弃失败回合也不是通用请求", other["criteria"]["false"])
        self.assertIn("不能因为与出行主题相关就判为假", other["criteria"]["false"])
        self.assertIn("否定、引用、历史提及", other["criteria"]["false"])
        self.assertIn("多笔独立费用", questions["requires_task_routing"]["criteria"]["true"])

    def test_control_operations_are_not_other_and_save_read_remain_other(self):
        from business_travel.jev import _intent_questions
        from business_travel.slot_protocol import ROUTE_PROMPT, NewRequest

        questions = _intent_questions()
        other = questions["has_other_request"]
        self.assertIn("阅读或解释已返回", other["criteria"]["true"])
        self.assertIn("确认事实", other["criteria"]["true"])
        self.assertIn("只修改已有事项的查询条件也不是 other", other["criteria"]["true"])
        routing = questions["requires_task_routing"]["criteria"]
        self.assertIn("取消这个查询", routing["true"])
        self.assertIn("目标无法对应到唯一事项", routing["true"])
        self.assertIn("仍应作为 other 任务保留", routing["false"])
        clarification = _intent_questions(clarification=True)["requires_task_routing"]["criteria"]
        self.assertIn("可由一个 other 新任务表达", clarification["false"])
        self.assertIn("不能用新任务代替", clarification["false"])
        self.assertIn("never other tasks", ROUTE_PROMPT)
        self.assertIn("still require an other task", ROUTE_PROMPT)
        self.assertIn("needs both plan_trip and other", ROUTE_PROMPT)
        description = NewRequest.model_fields["intent"].description
        self.assertIn("saving the current trip", description)
        self.assertIn("not other", description)
        saved = multi_decision("other").primary_route(empty_state("保存这个行程"))
        self.assertEqual([task["intent"] for task in saved["tasks"]], ["other"])
        combined = multi_decision("plan_trip", "other").primary_route(empty_state("查机票并保存行程"))
        self.assertEqual([task["intent"] for task in combined["tasks"]], ["plan_trip", "other"])
        cancelled = multi_decision(overrides={"requires_task_routing": {"type": "noul", "noul": 0.99}})
        self.assertIsNone(cancelled.primary_route(empty_state("取消这个查询")))

    def test_surface_words_do_not_override_confident_decisions(self):
        read = multi_decision("plan_trip", "other")
        read_text = "查看我刚才保存的行程"
        route = read.primary_route(empty_state(read_text))
        self.assertEqual([task["intent"] for task in route["tasks"]], ["plan_trip", "other"])
        self.assertEqual(read.hint()["intents"], ["plan_trip", "other"])

        seats = multi_decision("plan_trip")
        self.assertEqual(
            [task["intent"] for task in seats.primary_route(
                empty_state("普通人员坐高铁有哪些席别可以选择"))["tasks"]],
            ["plan_trip"])

        saved = multi_decision("plan_trip", "other")
        both = saved.primary_route(empty_state("帮我找下周一入住杭州的酒店并保存行程"))
        self.assertEqual([task["intent"] for task in both["tasks"]], ["plan_trip", "other"])

        control = multi_decision("other")
        self.assertEqual(
            [task["intent"] for task in control.primary_route(empty_state("取消这个查询"))["tasks"]],
            ["other"])
        self.assertEqual(control.hint()["requires_task_routing"], "no")

        answer = "2099-09-08入住，2099-09-09退房，另外取消这个查询"
        state = {"user_message": answer, "current_slots": {}, "task_context": [],
                 "pending_tasks": [], "pending_turns": [],
                 "clarification": {"question": "请补充入住和退房日期。",
                                   "items": [{"intent": "plan_trip", "subject_id": "s1",
                                              "draft_task_id": "d1"}]}}
        mixed = multi_decision(overrides={"is_clarification_answer": {"type": "noul", "noul": 0.99}})
        self.assertEqual(mixed.clarification_followup(state),
                         {"answers_clarification": True, "new_intents": []})

    @patch("business_travel.jev.httpx.post")
    def test_missing_key_is_local_and_does_not_call_network(self, post):
        result = JevClient(settings(jev_api_key="")).classify({"user_message": "你好"})
        self.assertEqual(result.status, "missing_key")
        post.assert_not_called()

    @patch("business_travel.jev.httpx.post")
    def test_clarification_state_asks_answer_relation_and_new_goals(self, post):
        post.return_value = httpx.Response(200, request=httpx.Request("POST", "https://jev.test"),
                                            json={"answers": {
                                                "is_clarification_answer": {"type": "noul", "noul": 0.97}}})
        state = {"user_message": "2099-09-08入住，2099-09-09退房", "current_slots": {},
                 "task_context": [], "pending_tasks": [], "pending_turns": [],
                 "clarification": {"question": "请补充入住和退房日期。",
                                    "missing_fields": ["check_in", "check_out"],
                                    "items": [{"intent": "plan_trip", "subject_id": "s1",
                                               "draft_task_id": "d1", "question": "查酒店"}]}}
        decision = JevClient(settings()).classify(state)
        questions = post.call_args.kwargs["json"]["questions"]
        self.assertEqual(list(questions), ["is_clarification_answer", *INTENT_QUESTIONS.values(),
                                            "requires_task_routing"])
        self.assertNotIn("query_scope", questions)
        answer = questions["is_clarification_answer"]
        self.assertIn("即使同一句还提出了其他独立请求", answer["instructions"])
        self.assertNotIn("已经转向", answer["criteria"]["false"])
        for key in INTENT_QUESTIONS.values():
            self.assertIn("不是新的独立业务目标", questions[key]["instructions"])
            self.assertIn("直接回答待补字段也不构成该独立目标", questions[key]["criteria"]["false"])
        self.assertIn("直接补充待补字段不算复杂路由",
                      questions["requires_task_routing"]["criteria"]["false"])
        self.assertEqual(decision.clarification_answer(), 0.97)
        self.assertIsNone(decision.primary_route(state))
        self.assertIsNone(decision.clarification_followup(state))

    def test_clarification_followup_keeps_answer_and_extra_goal_separate(self):
        text = "2099-09-08入住，2099-09-09退房，另外查一下南京天气"
        state = {"user_message": text, "current_slots": {}, "task_context": [],
                 "pending_tasks": [], "pending_turns": [],
                 "clarification": {"question": "请补充入住和退房日期。",
                                    "items": [{"intent": "plan_trip", "subject_id": "s1",
                                               "draft_task_id": "d1", "question": "查酒店"}]}}
        answered = multi_decision(overrides={"is_clarification_answer": {"type": "noul", "noul": 0.99}})
        self.assertEqual(answered.clarification_followup(state),
                         {"answers_clarification": True, "new_intents": []})
        mixed = multi_decision("other", overrides={
            "is_clarification_answer": {"type": "noul", "noul": 0.99}})
        self.assertEqual(mixed.clarification_followup(state)["new_intents"], ["other"])
        route = build_clarification_route(state["clarification"], ["other"], text)
        self.assertEqual([task["operation"] for task in route["tasks"]], ["update_facts", "new_task"])
        self.assertEqual(route["tasks"][1]["intent"], "other")
        self.assertIn("普通对话、天气", route["tasks"][1]["question"])
        self.assertIn(text, route["tasks"][1]["question"])
        self.assertNotIn("查酒店", route["tasks"][1]["question"])
        ignored = multi_decision("other", overrides={
            "is_clarification_answer": {"type": "noul", "noul": 0.05}})
        self.assertIsNone(ignored.clarification_followup(state))
        uncertain = multi_decision(overrides={
            "is_clarification_answer": {"type": "noul", "noul": 0.99},
            "has_policy_explain": {"type": "noul", "noul": 0.5}})
        self.assertIsNone(uncertain.clarification_followup(state))

    def test_mixed_clarification_route_preserves_draft_and_extra_goal(self):
        first = extract_slots(RawModel([
            reply("TripExtraction", {"patches": [{"task_index": 0,
                "trip": {"query_scope": "hotel", "destination": "南京"},
                "evidence": {"query_scope": {"quote": "酒店"},
                             "destination": {"quote": "南京"}}}]})
        ]), [HumanMessage("查南京酒店")], {}, 1,
            reviewer=RawModel([reply("SemanticReview", {"verdict": "accept"})]),
            jev_route=multi_decision("plan_trip").primary_route(empty_state("查南京酒店")))
        self.assertEqual(first["status"], "clarify", first)
        text = "2099-09-08入住，2099-09-09退房，另外查一下南京天气"
        followup = multi_decision("other", overrides={
            "is_clarification_answer": {"type": "noul", "noul": 0.99}}).clarification_followup(
                {"user_message": text, "clarification": first["clarification"]})
        second = extract_slots(RawModel([
            reply("TripExtraction", {"patches": [{"task_index": 0,
                "trip": {"check_in": "2099-09-08", "check_out": "2099-09-09"},
                "evidence": {"check_in": {"quote": "2099-09-08"},
                             "check_out": {"quote": "2099-09-09"}}}]})
        ]), [HumanMessage("查南京酒店"), HumanMessage(text)], {}, 1,
            reviewer=RawModel([reply("SemanticReview", {"verdict": "accept"})]),
            clarification_context=first["clarification"],
            clarification_route=build_clarification_route(
                first["clarification"], followup["new_intents"], text))
        self.assertEqual(second["status"], "ok", second)
        self.assertEqual([task["intent"] for task in second["tasks"]], ["plan_trip", "other"])
        self.assertEqual(second["tasks"][0]["subject"]["values"]["check_in"], "2099-09-08")
        self.assertIsNone(second["tasks"][1]["subject"])
        self.assertIn("南京天气", second["tasks"][1]["question"])

    def test_clarification_answer_requires_a_valid_noul_probability(self):
        self.assertEqual(JevDecision("ok", {
            "is_clarification_answer": {"type": "noul", "noul": 0.91}}, {}).clarification_answer(), 0.91)
        self.assertIsNone(JevDecision("ok", {
            "is_clarification_answer": {"type": "choice", "value": "yes"}}, {}).clarification_answer())

    def test_each_single_intent_still_generates_one_task(self):
        for intent in INTENT_QUESTIONS:
            with self.subTest(intent=intent):
                text = "帮我查机票" if intent == "plan_trip" else "用户原话"
                decision = multi_decision(intent)
                route = decision.primary_route(empty_state(text))
                self.assertEqual(decision.hint()["intents"], [intent])
                self.assertEqual(decision.hint()["intent"], intent)
                self.assertEqual(route["tasks"], [{"operation": "new_task", "intent": intent,
                    "question": text, "depends_on": []}])

    def test_every_intent_combination_preserves_all_goals_including_other(self):
        text = "帮我查机票。用户的完整原话，含否定和假设"
        for count in range(2, 5):
            for selected in combinations(INTENT_QUESTIONS, count):
                with self.subTest(intents=selected):
                    decision = multi_decision(*selected)
                    self.assertEqual(decision.hint()["intents"], list(selected))
                    self.assertNotIn("intent", decision.hint())
                    route = decision.primary_route(empty_state(text))
                    self.assertEqual([t["intent"] for t in route["tasks"]], list(selected))
                    self.assertEqual(len({t["question"] for t in route["tasks"]}), count)
                    for task in route["tasks"]:
                        self.assertIn(TASK_SCOPES[task["intent"]], task["question"])
                        self.assertTrue(task["question"].endswith(text))
                        self.assertIn("不得重复执行", task["question"])
                        self.assertEqual(task["depends_on"], [])

    def test_no_match_is_not_silently_converted_to_other(self):
        decision = multi_decision()
        self.assertEqual(decision.hint()["intents"], [])
        self.assertIsNone(decision.primary_route(empty_state()))
        self.assertEqual(decision.hint()["rejected_intents"], list(INTENT_QUESTIONS))
        self.assertEqual(decision.hint()["undecided_intents"], [])
        self.assertEqual(decision.hint()["requires_task_routing"], "no")

    def test_uncertain_intent_stays_with_turn_route_and_is_not_a_rejection(self):
        from business_travel.slot_protocol import ROUTE_PROMPT

        decision = multi_decision("plan_trip", overrides={
            "has_policy_explain": {"type": "noul", "noul": 0.55},
            "has_other_request": {"type": "noul", "noul": 0.05},
        })
        hint = decision.hint()
        self.assertEqual(hint["intents"], ["plan_trip"])
        self.assertEqual(hint["undecided_intents"], ["policy_explain"])
        self.assertEqual(hint["rejected_intents"], ["policy_check", "other"])
        self.assertEqual(hint["requires_task_routing"], "no")
        self.assertNotIn("intent", hint)
        self.assertIsNone(decision.primary_route(empty_state("查机票，这条规定怎么理解")))

        missing = multi_decision("plan_trip")
        answers = dict(missing.answers)
        answers["has_policy_check"] = {"type": "noul", "noul": "bad"}
        answers.pop("requires_task_routing")
        incomplete = type(missing)("ok", answers, {}, intent_threshold=missing.intent_threshold)
        self.assertIn("policy_check", incomplete.hint()["undecided_intents"])
        self.assertNotIn("policy_check", incomplete.hint()["rejected_intents"])
        self.assertEqual(incomplete.hint()["requires_task_routing"], "undecided")
        self.assertIsNone(incomplete.primary_route(empty_state()))

        complex = multi_decision("policy_check", overrides={
            "requires_task_routing": {"type": "noul", "noul": 0.5}})
        self.assertEqual(complex.hint()["requires_task_routing"], "undecided")
        self.assertEqual(complex.hint()["intents"], ["policy_check"])
        self.assertIsNone(complex.primary_route(empty_state()))
        self.assertIn("Absence from intents is not a rejection", ROUTE_PROMPT)
        self.assertIn("a rejection score cannot forbid it", ROUTE_PROMPT)
        self.assertNotIn("Do not create those intents", ROUTE_PROMPT)
        self.assertIn("Cancel, resume, retry, and discard stay controls", ROUTE_PROMPT)

        from business_travel.pending_turns import RECOVERY_PROMPT
        from business_travel.slot_protocol import route_prompt
        self.assertNotIn("pending_turns contains USER TEXT", route_prompt({}))
        self.assertNotIn("pending_turns contains USER TEXT", route_prompt({"pending_turns": []}))
        with_recovery = route_prompt({"pending_turns": [{"user_message": 0}]})
        self.assertIn("pending_turns contains USER TEXT", with_recovery)
        self.assertTrue(with_recovery.endswith(RECOVERY_PROMPT))

    def test_missing_malformed_and_uncertain_answers_require_turn_route(self):
        valid = multi_decision("plan_trip")
        for key in (*INTENT_QUESTIONS.values(), "requires_task_routing"):
            for invalid in (None, True, 0.99, {"noul": 0.99},
                            {"type": "choice", "noul": 0.99},
                            *({"type": "noul", "noul": value}
                              for value in (True, "0.99", -1, 2, float("nan"),
                                            float("inf"), 0.5, 0.4, 0.6))):
                with self.subTest(key=key, invalid=invalid):
                    decision = multi_decision("plan_trip", overrides={key: invalid})
                    self.assertIsNone(decision.primary_route(empty_state()))
            answers = dict(valid.answers)
            answers.pop(key)
            self.assertIsNone(JevDecision("ok", answers, {}).primary_route(empty_state()))

    def test_legacy_answer_cannot_override_incomplete_new_decisions(self):
        decision = JevDecision("ok", {"intent": "plan_trip", "has_other_request": None}, {})
        self.assertIsNone(decision.primary_route(empty_state()))
        self.assertNotIn("intent", decision.hint())

    def test_complex_task_decomposition_requires_turn_route(self):
        decision = multi_decision("policy_check", overrides={
            "requires_task_routing": {"type": "noul", "noul": 0.99}})
        self.assertEqual(decision.hint()["intents"], ["policy_check"])
        self.assertIsNone(decision.primary_route(empty_state("核对两笔不同的住宿费")))

    def test_existing_state_and_pending_recovery_never_create_new_tasks(self):
        decision = multi_decision("plan_trip", "other")
        for key, value in (
            ("current_slots", {"trip": {"id": "subject-1"}}),
            ("task_context", [{"id": "task-1"}]),
            ("pending_tasks", [{"id": "task-1"}]),
            ("pending_turns", [{"user_message": 0, "content": "失败请求"}]),
        ):
            with self.subTest(key=key):
                self.assertIsNone(decision.primary_route({**empty_state(), key: value}))

    def test_threshold_is_configurable_and_symmetric(self):
        overrides = {"has_plan_trip": {"type": "noul", "noul": 0.8},
                     "has_other_request": {"type": "noul", "noul": 0.2}}
        self.assertIsNotNone(multi_decision("plan_trip", overrides=overrides).primary_route(empty_state()))
        self.assertIsNone(multi_decision("plan_trip", overrides=overrides,
                                        threshold=0.9).primary_route(empty_state()))

    @patch("business_travel.jev.httpx.post")
    def test_official_noul_response_preserves_usage_and_uses_configured_threshold(self, post):
        answers = multi_decision("plan_trip", "other").answers
        post.return_value = httpx.Response(200, request=httpx.Request("POST", "https://jev.test"),
            json={"answers": answers, "usage": {"input_tokens": 120, "output_tokens": 20}})
        decision = JevClient(settings(jev_intent_threshold=0.9)).classify(empty_state())
        self.assertEqual(post.call_count, 1)
        self.assertEqual(decision.intent_threshold, 0.9)
        self.assertEqual(decision.hint()["intents"], ["plan_trip", "other"])
        self.assertEqual(decision.usage["total_tokens"], 140)

    def test_multi_intent_route_runs_domain_extractors_without_turn_route(self):
        text = "查南京酒店，2099-09-08入住，2099-09-09退房；解释住宿标准；核验2099-09-08住宿金额319元；另外打个招呼"
        decision = multi_decision(*INTENT_QUESTIONS)
        model = RawModel([
            reply("TripExtraction", {"patches": [{"task_index": 0,
                "trip": {"query_scope": "hotel", "destination": "南京", "check_in": "2099-09-08", "check_out": "2099-09-09"},
                "evidence": {"query_scope": {"quote": "酒店"},
                             "destination": {"quote": "南京"},
                             "check_in": {"quote": "2099-09-08"},
                             "check_out": {"quote": "2099-09-09"}}}]}),
            reply("ExpenseExtraction", {"patches": [
                {"task_index": 1, "expense": {"kind": "hotel", "amount": 319,
                                                 "expense_date": "2099-09-08", "destination": "南京"},
                 "evidence": {"amount": {"quote": "319元"},
                              "expense_date": {"quote": "2099-09-08"},
                              "destination": {"quote": "南京"}}},
                {"task_index": 2, "expense": {"kind": "hotel", "expense_date": "2099-09-08",
                                                 "destination": "南京"},
                 "evidence": {"expense_date": {"quote": "2099-09-08"},
                              "destination": {"quote": "南京"}},
                 "policy_query": {"category": "hotel", "mode": "general"}},
            ]}),
        ])
        reviewer = RawModel([reply("SemanticReview", {"verdict": "accept"})])
        current = {}
        result = extract_slots(model, [HumanMessage(text)], current, 1, reviewer=reviewer,
            jev_hint=decision.hint(), jev_route=decision.primary_route(empty_state(text)))
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(current, {})
        self.assertEqual([task["intent"] for task in result["tasks"]], list(INTENT_QUESTIONS))
        self.assertEqual(len(result["slots"]["subjects"]), 3)
        self.assertEqual(result["tasks"][1]["subject"]["values"]["amount"], "319")
        self.assertNotIn("amount", result["tasks"][2]["subject"]["values"])
        self.assertIsNone(result["tasks"][3]["subject"])
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(len(reviewer.calls), 1)
        for call, intents in zip(model.calls, (["plan_trip"], ["policy_check", "policy_explain"])):
            assigned = json.loads(call[-1].content)["extraction_tasks"]
            self.assertEqual([task["intent"] for task in assigned], intents)

    def test_uncertain_decision_uses_turn_route_instead_of_losing_a_goal(self):
        decision = multi_decision("plan_trip", overrides={
            "has_policy_explain": {"type": "noul", "noul": 0.5}})
        model = RawModel([reply("TurnRoute", {"tasks": [
            {"operation": "new_task", "intent": "other", "question": "请澄清请求"}]})])
        text = "帮我看看"
        submission, _, _, _ = propose_slots(model, [HumanMessage(text)], {},
                                         extraction_payload(text, decision))
        self.assertEqual(submission.tasks[0].intent, "other")
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(json.loads(model.calls[0][-1].content)["jev_hint"]["intents"], ["plan_trip"])

    def test_review_revision_falls_back_to_turn_route_to_repair_task_structure(self):
        text = "核对两笔不同住宿费，费用日期2099-09-08，目的地南京"
        decision = multi_decision("policy_check")
        model = RawModel([
            reply("ExpenseExtraction", {"patches": [
                {"task_index": 0, "expense": {"kind": "hotel", "expense_date": "2099-09-08",
                                                   "destination": "南京"},
                 "evidence": {"expense_date": {"quote": "2099-09-08"},
                              "destination": {"quote": "南京"}}}]}),
            reply("TurnRoute", {"tasks": [
                {"operation": "new_task", "intent": "policy_check", "question": "第一笔住宿"},
                {"operation": "new_task", "intent": "policy_check", "question": "第二笔住宿"}]}),
            reply("ExpenseExtraction", {"patches": [
                {"task_index": index, "expense": {"kind": "hotel", "expense_date": "2099-09-08",
                                                     "destination": "南京"},
                 "evidence": {"expense_date": {"quote": "2099-09-08"},
                              "destination": {"quote": "南京"}}}
                for index in (0, 1)]}),
        ])
        reviewer = RawModel([
            reply("SemanticReview", {"verdict": "revise", "issues": ["遗漏第二笔住宿任务"]}),
            reply("SemanticReview", {"verdict": "accept"}),
        ])
        result = extract_slots(model, [HumanMessage(text)], {}, 2, reviewer=reviewer,
            jev_hint=decision.hint(), jev_route=decision.primary_route(empty_state(text)))
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(len(result["tasks"]), 2)
        self.assertEqual(json.loads(model.calls[1][-1].content)["jev_route"], {})
        self.assertIn("遗漏第二笔住宿任务", model.calls[1][-1].content)

    def test_task_limit_rejection_uses_full_route_instead_of_truncating_intents(self):
        decision = multi_decision("plan_trip", "other")
        text = "查酒店并打招呼"
        payload = {**extraction_payload(text, decision), "task_limit": 1}
        model = RawModel([reply("TurnRoute", {"clarification": "请分两次发送这些请求。"})])
        with self.assertRaisesRegex(TaskClarification, "请分两次"):
            propose_slots(model, [HumanMessage(text)], {}, payload)
        self.assertEqual(len(model.calls), 1)


class JevGraphTests(unittest.TestCase):
    setUp = graph_fixtures.GraphTests.setUp
    tearDown = graph_fixtures.GraphTests.tearDown

    def run_graph(self, decision, *, mode="primary"):
        from business_travel.graph import build_assistant
        from langgraph.checkpoint.memory import InMemorySaver

        text = "查南京酒店，2099-09-08入住，2099-09-09退房；另外打个招呼"
        model = RawModel([
            *([] if mode == "primary" and decision.primary_route(empty_state(text)) else [
                reply("TurnRoute", {"tasks": [
                    {"operation": "new_task", "intent": "plan_trip", "question": "查南京酒店"},
                    {"operation": "new_task", "intent": "other", "question": "打个招呼"}]}),
            ]),
            reply("TripExtraction", {"patches": [{"task_index": 0,
                "trip": {"query_scope": "hotel", "destination": "南京", "check_in": "2099-09-08", "check_out": "2099-09-09"},
                "evidence": {"query_scope": {"quote": "酒店"},
                             "destination": {"quote": "南京"},
                             "check_in": {"quote": "2099-09-08"},
                             "check_out": {"quote": "2099-09-09"}}}]}),
        ])
        supervisor = RawModel([AIMessage(content="您好。")])
        models = {"slots": model, "supervisor": supervisor,
                  "reviewer": RawModel([reply("SemanticReview", {"verdict": "accept"})])}
        provider = graph_fixtures.FixtureProvider()
        graph = build_assistant(lambda role: models[role], replace(self.settings, jev_mode=mode),
            self.memory, self.policy, checkpointer=InMemorySaver(), provider=provider)
        with patch("business_travel.graph.JevClient.classify", return_value=decision) as classify:
            result = graph.invoke({"messages": [HumanMessage(text)]}, self.config)
        self.assertEqual(classify.call_count, 1)
        self.assertIn("pending_turns", classify.call_args.args[0])
        self.assertIsNone(result.get("slot_error"), result.get("slot_error"))
        self.assertEqual([t["intent"] for t in result["tasks"].values()], ["plan_trip", "other"])
        self.assertEqual(result["task_queue"], [])
        self.assertEqual(len(result["task_answers"]), 2)
        self.assertEqual(len(provider.requests), 0)
        self.assertEqual(len(supervisor.calls), 1)
        self.assertIn("您好", result["messages"][-1].content)
        return result, model, supervisor

    def test_primary_mode_dispatches_both_goals_without_turn_route(self):
        result, model, supervisor = self.run_graph(multi_decision("plan_trip", "other"))
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(result["jev"]["hint"]["intents"], ["plan_trip", "other"])
        task_scope = next(message.content for message in supervisor.calls[0]
                          if isinstance(message, SystemMessage)
                          and "Execute ONLY the current reviewed task" in message.content)
        self.assertIn(TASK_SCOPES["other"], task_scope)
        self.assertIn("不得重复执行", task_scope)

    def test_shadow_mode_keeps_original_routing_and_does_not_inject_hints(self):
        _, model, _ = self.run_graph(multi_decision("plan_trip", "other"), mode="shadow")
        self.assertEqual(len(model.calls), 2)
        payload = json.loads(model.calls[0][-1].content)
        self.assertEqual(payload["jev_hint"], {})
        self.assertEqual(payload["jev_route"], {})

    def test_jev_failure_and_unknown_intents_fall_back_to_original_route(self):
        for decision in (JevDecision("error", {}, {}, error_code="synthetic_failure"),
                         multi_decision()):
            with self.subTest(status=decision.status):
                _, model, _ = self.run_graph(decision)
                self.assertEqual(len(model.calls), 2)

    def test_followup_answer_uses_saved_clarification_without_turn_route(self):
        from business_travel.graph import build_assistant
        from langgraph.checkpoint.memory import InMemorySaver

        slot_model = RawModel([
            reply("ExpenseExtraction", {"patches": [{"task_index": 0,
                "expense": {"kind": "hotel"}, "evidence": {}}]}),
            reply("ExpenseExtraction", {"patches": [{"task_index": 0,
                "expense": {"expense_date": "2099-09-08", "destination": "南京"},
                "evidence": {"expense_date": {"quote": "2099-09-08"},
                             "destination": {"quote": "南京"}}}]})
        ])
        reviewer = RawModel([reply("SemanticReview", {"verdict": "accept"})])
        supervisor = RawModel([])
        policy = Mock()
        policy.check.return_value = {"status": "within_checked_rules", "approved": True, "sources": []}
        models = {"slots": slot_model, "reviewer": reviewer, "supervisor": supervisor}
        config = replace(self.settings, jev_enabled=True, jev_api_key="synthetic-key", jev_mode="primary")
        graph = build_assistant(lambda role: models[role], config, self.memory, policy,
                                checkpointer=InMemorySaver(), provider=graph_fixtures.FixtureProvider())
        first_decision = multi_decision("policy_check")
        followup_decision = multi_decision(overrides={
            "is_clarification_answer": {"type": "noul", "noul": 0.99}})
        with patch("business_travel.graph.JevClient.classify",
                   side_effect=[first_decision, followup_decision]) as classify:
            first = graph.invoke({"messages": [HumanMessage("核验南京住宿")]} , self.config)
            second = graph.invoke({"messages": [HumanMessage("费用日期2099-09-08，目的地南京")]}, self.config)

        self.assertEqual(classify.call_count, 2)
        self.assertEqual(first["slot_error"]["status"], "clarify")
        self.assertIsNotNone(first["clarification"])
        self.assertIsNone(second.get("slot_error"), second.get("slot_error"))
        self.assertIsNone(second.get("clarification"))
        self.assertEqual(len(second["tasks"]), 1)
        self.assertEqual(second["tasks"][next(iter(second["tasks"]))]["status"], "completed")
        self.assertEqual(len(slot_model.calls), 2)
        self.assertEqual([json.loads(call[-1].content).get("extraction_tasks")
                          for call in slot_model.calls], [[{"task_index": 0, "intent": "policy_check",
                                                            "question": "核验南京住宿", "operation": "new_task",
                                                            "target_id": None}],
                                                           [{"task_index": 0, "intent": "policy_check",
                                                             "question": "核验南京住宿", "operation": "update_facts",
                                                             "target_id": first["clarification"]["items"][0]["subject_id"]}]])


if __name__ == "__main__":
    unittest.main()

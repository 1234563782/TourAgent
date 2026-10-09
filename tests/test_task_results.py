"""Read-only result references must never replay work or promote quotes to expenses."""

import json
import unittest
from copy import deepcopy
from dataclasses import replace
from unittest.mock import Mock

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from business_travel.task_results import read_task_result, render_task_result, task_result_context
from tests import test_travel_graph as fixtures
from tests.test_travel_graph import FixtureProvider, PLAN_TEXT, call, slot_response


class ResultReferenceTests(unittest.TestCase):
    def setUp(self):
        self.result = {"subject_id": "s", "revision": 2, "status": "checked",
                       "purpose": "check", "rule_checks": [], "sources": []}
        self.subject = {"id": "s", "revision": 2, "domain": "expense",
                        "intent": "policy_check", "validation": "context_reviewed",
                        "result": self.result}
        self.task = {"id": "t", "intent": "policy_check", "subject_id": "s", "revision": 2,
                     "status": "completed", "result_ref": {"subject_id": "s", "revision": 2}}
        self.state = {"slots": {"subjects": {"s": self.subject}}, "tasks": {"t": self.task}}

    def test_valid_read_is_detached_and_does_not_modify_state(self):
        original = deepcopy(self.state)
        answer = read_task_result(self.state, "t", 2)
        self.assertEqual(answer["status"], "ok")
        self.assertEqual(answer["result"], self.result)
        answer["result"]["status"] = "changed"
        self.assertEqual(self.state, original)

    def test_rejects_stale_missing_unfinished_and_wrong_domain_results(self):
        cases = [
            ("unknown", 2, {}, {}, {}),
            ("t", 1, {}, {}, {}),
            ("t", 2, {"status": "running"}, {}, {}),
            ("t", 2, {"status": "cancelled"}, {}, {}),
            ("t", 2, {"result_ref": None}, {}, {}),
            ("t", 2, {}, {"revision": 3}, {}),
            ("t", 2, {}, {"validation": "pending_context_review"}, {}),
            ("t", 2, {}, {}, {"revision": 1}),
            ("t", 2, {}, {}, {"subject_id": "another"}),
            ("t", 2, {"intent": "other"}, {}, {}),
            ("t", 2, {"intent": "plan_trip"}, {}, {}),
            ("t", 2, {}, {"intent": "policy_explain"}, {}),
        ]
        for ident, revision, task, subject, result in cases:
            with self.subTest(case=(ident, revision, task, subject, result)):
                state = deepcopy(self.state)
                state["tasks"]["t"].update(task)
                state["slots"]["subjects"]["s"].update(subject)
                state["slots"]["subjects"]["s"]["result"].update(result)
                answer = read_task_result(state, ident, revision)
                self.assertEqual(answer["status"], "error")
                self.assertNotIn("result", answer)

    def test_incomplete_but_recorded_result_can_be_explained(self):
        self.state["tasks"]["t"]["status"] = "needs_clarification"
        self.assertEqual(read_task_result(self.state, "t", 2)["status"], "ok")

    def test_missing_result_and_other_session_cannot_leak(self):
        self.assertEqual(read_task_result({}, "t", 2)["status"], "error")
        self.subject["result"] = None
        self.assertEqual(read_task_result(self.state, "t", 2)["status"], "error")

    def test_renderer_does_not_claim_new_execution(self):
        text = render_task_result(read_task_result(self.state, "t", 2))
        self.assertIn("未重新查询或核验", text)
        self.assertIn("不代表", text)
        self.assertIn("重新确认", render_task_result({"status": "error"}))

    def test_routing_context_only_contains_current_readable_result_metadata(self):
        self.result["private_supplier_payload"] = "not routing context"
        context = task_result_context(self.state["slots"], list(self.state["tasks"].values()))
        self.assertEqual(context, [{
            "task_id": "t", "intent": "policy_check", "subject_id": "s", "revision": 2,
            "result_status": "checked", "selected_count": 0, "policy_check_statuses": [],
        }])
        self.subject["revision"] = 3
        self.assertEqual(task_result_context(self.state["slots"],
                                            list(self.state["tasks"].values())), [])

    def test_routing_context_is_shared_with_extractor_and_reviewer(self):
        from business_travel.slots import extract_slots
        from tests.test_travel_graph import ScriptedModel, review_response
        seen = []

        def capture(response):
            scripted = ScriptedModel([response])
            def respond(messages):
                seen.append(json.loads(messages[-1].content))
                return scripted.invoke(messages)
            model = Mock()
            def bind(tools, **kwargs):
                scripted.bind_tools(tools, **kwargs)
                return model
            model.bind_tools.side_effect = bind
            model.invoke.side_effect = respond
            return model

        original = deepcopy(self.state)
        answer = extract_slots(
            capture(slot_response()), [HumanMessage(content="这份结果是什么意思？")],
            self.state["slots"], 1, reviewer=capture(review_response()),
            task_context=list(self.state["tasks"].values()))
        self.assertEqual(answer["status"], "ok")
        self.assertEqual(len(seen[0]["available_results"]), 1)
        self.assertEqual(seen[0]["available_results"], seen[1]["available_results"])
        self.assertEqual(self.state, original)


class ResultGraphTests(unittest.TestCase):
    setUp = fixtures.GraphTests.setUp
    tearDown = fixtures.GraphTests.tearDown
    build = fixtures.GraphTests.build

    def test_one_followup_can_read_multiple_business_results(self):
        refs = []
        self.settings = replace(self.settings, context_window_tokens=200000)
        provider = FixtureProvider()
        graph = self.build([
            lambda _: call("read_task_result", refs[0], "read-first"),
            lambda _: call("read_task_result", refs[1], "read-second"),
            AIMessage(content="已读取两份结果")], provider,
            slots=[slot_response(self.trip, "plan_trip"),
                   slot_response({**self.trip, "destination": "Hangzhou"}, "plan_trip"),
                   slot_response()])
        first = graph.invoke({"messages": [HumanMessage(content=PLAN_TEXT)]}, self.config)
        second = graph.invoke({"messages": [HumanMessage(
            content="另外查Beijing到Hangzhou，2099-09-08出发，flight")]}, self.config)
        for state in (first, second):
            task = next(t for t in state["tasks"].values()
                        if t["subject_id"] == state["slots"]["trip"]["id"])
            refs.append({"task_id": task["id"], "expected_revision": task["revision"]})
        result = graph.invoke({"messages": [HumanMessage(content="分别查看两份已保存方案，不重新搜索")]},
                              self.config)
        reads = [json.loads(m.content) for m in result["messages"]
                 if isinstance(m, ToolMessage) and m.name == "read_task_result"]
        self.assertEqual([r["task_id"] for r in reads], [r["task_id"] for r in refs])
        self.assertEqual(result["messages"][-1].content.count("未重新查询或核验"), 2)
        self.assertEqual(len(provider.requests), 2)

    def test_large_verified_result_uses_complete_presentation_without_losing_raw_evidence(self):
        refs, calls = {}, []

        def inspect(messages):
            calls.append(messages)
            return call("read_task_result", refs, "large-result")

        def finish(messages):
            calls.append(messages)
            return AIMessage(content="完成")

        provider = FixtureProvider()
        graph = self.build([inspect, finish, AIMessage(content="你好")], provider,
                           slots=[slot_response(self.trip, "plan_trip"),
                                  slot_response(), slot_response()])
        first = graph.invoke({"messages": [HumanMessage(content=PLAN_TEXT)]}, self.config)
        task = next(iter(first["tasks"].values()))
        refs.update(task_id=task["id"], expected_revision=task["revision"])
        slots = deepcopy(first["slots"])
        subject = slots["subjects"][task["subject_id"]]
        evidence = "完整来源记录。" * self.settings.context_window_tokens
        subject["result"]["source_evidence"] = evidence
        slots["trip"] = subject
        graph.update_state(self.config, {"slots": slots})

        result = graph.invoke({"messages": [HumanMessage(content="查看上次方案")]}, self.config)
        reads = [m for m in result["messages"]
                 if isinstance(m, ToolMessage) and m.name == "read_task_result"]
        self.assertEqual(json.loads(reads[-1].content)["result"]["source_evidence"], evidence)
        self.assertEqual(result["slots"], slots)
        self.assertTrue(all(t["status"] == "completed" for t in result["tasks"].values()))
        self.assertIn("未重新查询或核验", result["messages"][-1].content)
        self.assertNotIn("上下文预算", result["messages"][-1].content)
        self.assertEqual(len(calls), 2)
        model_read = next(m for m in calls[-1]
                          if isinstance(m, ToolMessage) and m.name == "read_task_result")
        model_data = json.loads(model_read.content)
        self.assertEqual(model_data["rendered_result"], render_task_result(json.loads(reads[-1].content)))
        self.assertNotIn("source_evidence", model_read.content)
        self.assertEqual(model_data["task_id"], task["id"])
        original = result["messages"][model_data["source_message_index"]]
        self.assertEqual(original.content, reads[-1].content)
        self.assertEqual(len(provider.requests), 1)

        following = graph.invoke({"messages": [HumanMessage(content="你好")]}, self.config)
        self.assertIsNone(following.get("slot_error"))
        self.assertEqual(following["messages"][-1].content, "你好")
        self.assertEqual(len(provider.requests), 1)

    def test_result_followup_reads_checkpoint_without_search_or_new_expense(self):
        refs = {}
        def inspect(messages):
            return call("read_task_result", refs, "read-result")

        provider = FixtureProvider()
        graph = self.build([inspect, AIMessage(content="全部合规，已经批准")], provider,
                           slots=[slot_response(self.trip, "plan_trip"), slot_response()])
        first = graph.invoke({"messages": [HumanMessage(content=PLAN_TEXT)]}, self.config)
        task = next(iter(first["tasks"].values()))
        refs.update(task_id=task["id"], expected_revision=task["revision"])
        second = graph.invoke({"messages": [HumanMessage(content="这些候选符合报销规定吗？")]}, self.config)
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(second["slots"], first["slots"])
        self.assertEqual(second["tasks"][task["id"]], first["tasks"][task["id"]])
        self.assertNotIn("expense", second["slots"])
        answer = second["messages"][-1].content
        self.assertIn("未重新查询或核验", answer)
        self.assertNotIn("全部合规，已经批准", answer)
        reads = [json.loads(m.content) for m in second["messages"]
                 if isinstance(m, ToolMessage) and m.name == "read_task_result"]
        self.assertEqual(reads[-1]["status"], "ok")

    def test_other_session_cannot_read_task_by_known_id(self):
        refs = {}
        provider = FixtureProvider()
        graph = self.build([lambda _: call("read_task_result", refs, "read-other"),
                            AIMessage(content="伪造结果")], provider,
                           slots=[slot_response(self.trip, "plan_trip"), slot_response()])
        first = graph.invoke({"messages": [HumanMessage(content=PLAN_TEXT)]}, self.config)
        task = next(iter(first["tasks"].values()))
        refs.update(task_id=task["id"], expected_revision=task["revision"])
        result = graph.invoke({"messages": [HumanMessage(content="读取上个结果")]},
                              {"configurable": {"thread_id": "another:session"}})
        self.assertEqual(len(provider.requests), 1)
        self.assertIn("不存在", result["messages"][-1].content)
        self.assertNotIn("SYNTHETIC", result["messages"][-1].content)
        self.assertEqual(result.get("slots"), {})

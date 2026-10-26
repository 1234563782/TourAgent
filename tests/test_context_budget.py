"""Synthetic long conversations exercise real graph gates, not model language quality."""

from copy import deepcopy
from dataclasses import replace
from datetime import date
import json
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel, Field

from business_travel.context_budget import (
    BudgetedModel, ContextBudgetExceeded, assistant_context, encoded_size, history_summary,
    input_limit, model_task_snapshot, read_history, request_size, select_history,
    slot_history, validate_tool_sequence,
)
from business_travel.graph import build_assistant
from business_travel.observability import safe_metadata
from business_travel.policy_rag import explain_policy
from business_travel.settings import Settings
from tests import test_travel_graph as fixtures
from tests import test_failed_turn_recovery as recovery_fixtures
from tests.test_travel_graph import ScriptedModel, call, review_response, slot_response


def settings(**changes):
    return replace(Settings(api_key="", model="", base_url="", node_path="", flyai_cli_path="",
                            storage_dir=Path("."), legacy_memory_dir=Path(".")), **changes)


def turns(count, content="完整历史内容。"):
    result = []
    for index in range(count):
        result.extend([HumanMessage(content=f"{index}: {content}", id=f"u-{index}"),
                       AIMessage(content=f"第{index}轮回答。", id=f"a-{index}")])
    return result


class ContextBudgetTests(unittest.TestCase):
    def test_failed_or_invalid_result_read_is_not_replaced_by_presentation(self):
        for content in ('not json', '[]', '{"status":"error","reason":"stale"}',
                        '{"status":"ok","intent":"other","result":{}}'):
            with self.subTest(content=content):
                messages = [HumanMessage("结果"), call("read_task_result", {}, "read"),
                            ToolMessage(content=content, tool_call_id="read", name="read_task_result")]
                context, _ = assistant_context(messages, [], [], settings())
                self.assertEqual(context, messages)

    def test_oversized_rendered_result_is_rejected_not_truncated(self):
        data = {"status": "ok", "intent": "policy_explain", "result": {
            "purpose": "explain", "sources": [{"text": "不可省略的完整条款。" * 10000}]}}
        messages = [HumanMessage("结果"), call("read_task_result", {}, "read"),
                    ToolMessage(content=json.dumps(data, ensure_ascii=False),
                                tool_call_id="read", name="read_task_result")]
        before = deepcopy(messages)
        with self.assertRaises(ContextBudgetExceeded):
            assistant_context(messages, [], [], settings())
        self.assertEqual(messages, before)

    def test_long_history_is_bounded_without_mutating_source_or_indices(self):
        original = [*turns(200, "这里是历史对话" * 40), HumanMessage(content="当前问题", id="current")]
        before = deepcopy(original)
        config = settings(context_recent_tokens=1500)
        context, summary = assistant_context(original, [SystemMessage("rules")], [], config)
        self.assertEqual(original, before)
        self.assertEqual(context[-1], original[-1])
        self.assertLess(request_size(context), input_limit(config, "supervisor"))
        self.assertLess(request_size(context), request_size(original) // 10)
        self.assertEqual(summary["kind"], "extractive_history_navigation")
        self.assertLessEqual(encoded_size(summary), config.context_summary_tokens)
        for entry in summary["quotes"]:
            self.assertEqual(entry["quote"], original[entry["message_index"]].content)
            self.assertEqual(entry["user_message"], entry["message_index"] // 2)

    def test_current_tool_exchange_is_never_trimmed(self):
        exchange = [HumanMessage("当前查天气"), call("query_weather", {"city": "杭州"}, "weather"),
                    ToolMessage(content="unknown", tool_call_id="weather", name="query_weather")]
        view = select_history([*turns(30), *exchange], settings(context_recent_tokens=256), 12000,
                              max_messages=1)
        self.assertEqual(view.messages, exchange)
        validate_tool_sequence(view.messages)

    def test_old_parallel_tool_exchange_is_kept_whole_or_dropped(self):
        old = [HumanMessage("旧查询"),
               AIMessage("", tool_calls=[{"name": "q", "args": {}, "id": ident, "type": "tool_call"}
                                          for ident in ("a", "b")]),
               ToolMessage(content="A", tool_call_id="a"),
               ToolMessage(content="B", tool_call_id="b"), AIMessage("旧回答")]
        messages = [*old, HumanMessage("新问题")]
        large = select_history(messages, settings(), 20000)
        self.assertEqual(large.messages, messages)
        small = select_history(messages, settings(context_recent_tokens=256), 20000, max_messages=4)
        self.assertEqual(small.messages, messages[-1:])
        validate_tool_sequence(small.messages)

    def test_orphan_duplicate_or_incomplete_tools_are_rejected(self):
        cases = [
            [HumanMessage("x"), ToolMessage(content="x", tool_call_id="missing")],
            [HumanMessage("x"), call("q", {}, "a")],
            [HumanMessage("x"), call("q", {}, "a"), AIMessage("missing response")],
            [HumanMessage("x"), AIMessage("", tool_calls=[
                {"name": "q", "args": {}, "id": "same", "type": "tool_call"}] * 2)],
        ]
        for messages in cases:
            with self.subTest(messages=messages):
                with self.assertRaises(ValueError):
                    select_history(messages, settings(), 20000)

    def test_large_current_message_is_rejected_not_summarized(self):
        with self.assertRaises(ContextBudgetExceeded):
            select_history([HumanMessage("current-private-" * 1000)], settings(), 1000)

    def test_large_historical_tool_is_a_reference_current_result_is_exact(self):
        tool_result = ToolMessage(content=json.dumps({"status": "ok", "hotels": ["item"] * 1000}),
                                  tool_call_id="a", name="hotel")
        original = [HumanMessage("酒店"), call("hotel", {}, "a"), tool_result,
                    AIMessage("结果"), HumanMessage("第二家呢")]
        before = deepcopy(original)
        view = select_history(original, settings(context_tool_tokens=256), 20000)
        projected = next(m for m in view.messages if isinstance(m, ToolMessage))
        pointer = json.loads(projected.content)
        self.assertEqual(pointer["message_index"], 2)
        self.assertNotIn("status", pointer)
        self.assertTrue(pointer["archived_tool_result"])
        self.assertEqual(original, before)
        current = select_history(original[:3], settings(context_tool_tokens=256), 20000)
        self.assertEqual(current.messages[-1], tool_result)

    def test_summary_never_slices_negation_or_promotes_old_authorization(self):
        text = "金额不确定；不是没有批准，而是不知道是否批准。不要保存，也不要重试。"
        messages = [HumanMessage(text), AIMessage("请核实")]
        summary = history_summary(messages, 2, 2048)
        self.assertEqual(summary["quotes"][0]["quote"], text)
        self.assertIn("not current facts", summary["notice"])
        self.assertIn("Never replay", summary["notice"])
        digest = summary["source_sha256"]
        messages[0] = HumanMessage(text + "后续更正")
        self.assertNotEqual(history_summary(messages, 2, 2048)["source_sha256"], digest)
        huge = history_summary([HumanMessage(text * 200)], 1, 1024)
        self.assertEqual(huge["quotes"], [])
        self.assertEqual(huge["omitted_user_count"], 1)

    def test_summary_with_no_room_is_optional_not_an_unbounded_fallback(self):
        self.assertIsNone(history_summary([HumanMessage("x")], 1, 10))
        context, summary = assistant_context([HumanMessage("当前")], [SystemMessage("rule")], [], settings())
        self.assertIsNone(summary)
        self.assertEqual(len(context), 2)

    def test_utf8_estimate_includes_chinese_schema_and_tool_arguments(self):
        english = request_size([HumanMessage("abc")])
        chinese = request_size([HumanMessage("中文好")])
        self.assertGreater(chinese, english)
        self.assertGreater(request_size([call("q", {"value": "字" * 300}, "a")]), 900)
        self.assertGreater(request_size([HumanMessage("x")], [{"description": "word" * 100}]),
                           request_size([HumanMessage("x")]))

    def test_bound_model_blocks_before_network_and_preserves_valid_callbacks_config(self):
        raw = Mock()
        raw.bind_tools.return_value = raw
        model = BudgetedModel(raw, settings(context_role_input_tokens={"policy": 1024}), "policy")
        bound = model.bind_tools([])
        with self.assertRaises(ContextBudgetExceeded) as error:
            bound.invoke([HumanMessage("private" * 1000)])
        self.assertEqual(str(error.exception), "context_budget_exceeded")
        raw.invoke.assert_not_called()
        messages = [HumanMessage("safe")]
        config = {"callbacks": []}
        bound.invoke(messages, config=config)
        raw.invoke.assert_called_once_with(messages, config=config)

    def test_tool_schema_and_repair_growth_cannot_bypass_guard(self):
        class Submission(BaseModel):
            value: str = Field(description="synthetic description" * 80)
        raw = Mock()
        raw.bind_tools.return_value = raw
        config = settings(context_role_input_tokens={"slots": 1024})
        bound = BudgetedModel(raw, config, "slots").bind_tools([Submission])
        with self.assertRaises(ContextBudgetExceeded):
            bound.invoke([HumanMessage("x")])
        raw.invoke.assert_not_called()
        bound = BudgetedModel(raw, config, "slots").bind_tools([])
        bound.invoke([HumanMessage("short")])
        with self.assertRaises(ContextBudgetExceeded):
            bound.invoke([HumanMessage("short"), call("q", {"proposal": "x" * 1500}, "r"),
                          ToolMessage(content="repair", tool_call_id="r")])
        self.assertEqual(raw.invoke.call_count, 1)

    def test_slot_view_preserves_original_user_indices_and_required_state(self):
        messages = [*turns(80), HumanMessage("金额撤回，改为未知")]
        payload = {"current_message": messages[-1].content, "current_user_index": 80,
                   "current_slots": {"expense": {"revision": 3, "values": {"amount": None}}},
                   "pending_turns": [{"user_message": 5, "content": messages[10].content}]}
        before = deepcopy(payload)
        result = slot_history(payload, messages, "rule", [], settings(context_recent_tokens=300), 2)
        self.assertEqual(result["current_slots"], before["current_slots"])
        self.assertEqual(result["pending_turns"], before["pending_turns"])
        self.assertEqual(result["conversation"][-1]["user_message"], 80)
        self.assertEqual(result["conversation"][-1]["message_index"], 160)
        self.assertEqual(payload, before)
        self.assertGreater(result["omitted_message_count"], 0)

    def test_read_history_pages_are_exact_and_only_read_injected_conversation(self):
        source = [HumanMessage("不允许" * 1000, id="source")]
        config = settings(context_read_tokens=256)
        offset, fragments = 0, []
        while True:
            page = read_history(source, 0, offset, config)
            self.assertEqual(page["message_id"], "source")
            self.assertLessEqual(encoded_size(page["text"]), 256)
            self.assertFalse(page["complete"])
            fragments.append(page["text"])
            if page["next_offset"] is None:
                break
            self.assertGreater(page["next_offset"], offset)
            offset = page["next_offset"]
        self.assertEqual(json.loads("".join(fragments))["content"], source[0].content)
        other = read_history([HumanMessage("另一个会话")], 0, 0, config)
        self.assertNotIn("不允许", other["text"])
        for index, position in [(-1, 0), (1, 0), (True, 0), (0, -1), (0, True), (0, 10**9)]:
            self.assertEqual(read_history(source, index, position, config)["status"], "error")

    def test_completed_smalltalk_does_not_grow_task_prompt_but_business_state_is_retained(self):
        tasks = {str(i): {"id": str(i), "intent": "other", "status": "completed"} for i in range(100)}
        tasks["business"] = {"id": "business", "intent": "policy_check", "status": "completed",
                             "revision": 5, "question": "policy"}
        tasks["pending"] = {"id": "pending", "intent": "other", "status": "awaiting_confirmation"}
        before = deepcopy(tasks)
        self.assertEqual({x["id"] for x in model_task_snapshot(tasks)}, {"business", "pending"})
        self.assertEqual(tasks, before)

    def test_metadata_allowlist_exports_counts_not_history_or_summary(self):
        payload = {"context_role": "slots", "estimated_input_tokens": 1200, "context_input_limit": 2000,
                   "summary": "private", "messages": "private", "source_sha256": "a" * 64}
        self.assertEqual(safe_metadata(payload), {
            "context_role": "slots", "estimated_input_tokens": 1200, "context_input_limit": 2000})


class ContextConfigurationTests(unittest.TestCase):
    def test_env_defaults_and_role_override_without_reading_real_env(self):
        with patch.dict(os.environ, {"TRAVEL_LOAD_DOTENV": "0",
                                    "TRAVEL_CONTEXT_ROLE_INPUT_TOKENS": '{"slots":20000}'}, clear=True):
            config = Settings.from_env()
        self.assertEqual(input_limit(config, "slots"), 20000)
        self.assertEqual(input_limit(config, "policy"), 53248)

    def test_invalid_budgets_fail_closed(self):
        for changes in [
            {"context_window_tokens": 1000},
            {"context_output_tokens": -1},
            {"context_margin_tokens": 0},
            {"context_recent_tokens": 999999},
            {"context_role_input_tokens": []},
            {"context_role_input_tokens": {"unknown": 2000}},
            {"context_role_input_tokens": {"slots": True}},
            {"context_role_input_tokens": {"slots": 999999}},
        ]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                settings(**changes).validate_context()
        with patch.dict(os.environ, {"TRAVEL_LOAD_DOTENV": "0",
                                    "TRAVEL_CONTEXT_ROLE_INPUT_TOKENS": "not-json"}, clear=True):
            with self.assertRaises(ValueError):
                Settings.from_env()


class ContextGraphTests(unittest.TestCase):
    setUp = fixtures.GraphTests.setUp
    tearDown = fixtures.GraphTests.tearDown

    def build(self, responses, supervisors, *, config=None, reviews=None):
        self.seen = {"slots": [], "reviewer": [], "supervisor": []}
        def tracked(role, answers):
            source = ScriptedModel(answers)
            def invoke(messages):
                self.seen[role].append(deepcopy(messages))
                return source.invoke(messages)
            model = Mock()
            def bind(tools, **kwargs):
                source.bind_tools(tools, **kwargs)
                return model
            model.bind_tools.side_effect = bind
            model.invoke.side_effect = invoke
            return model
        models = {"slots": tracked("slots", responses),
                  "reviewer": tracked("reviewer", reviews or [review_response()] * 10),
                  "supervisor": tracked("supervisor", supervisors)}
        return build_assistant(lambda role: models[role], config or self.settings, self.memory, self.policy,
                               checkpointer=InMemorySaver(), provider=Mock())

    def test_real_graph_keeps_full_history_while_models_receive_bounded_views(self):
        graph = self.build([slot_response()], [AIMessage("回答")])
        original = [*turns(100, "历史内容" * 50), HumanMessage("你好")]
        result = graph.invoke({"messages": original}, self.config)
        self.assertIsNone(result["slot_error"])
        self.assertEqual(result["messages"][:len(original)], original)
        for role, calls in self.seen.items():
            self.assertEqual(len(calls), 1, role)
            self.assertLess(request_size(calls[0]), input_limit(self.settings, role))
        self.assertLess(len(self.seen["supervisor"][0]), len(original))
        self.assertIsNotNone(result["context_summary"])

    def test_current_fact_retraction_after_compression_reaches_reviewer_unchanged(self):
        initial = slot_response({"kind": "hotel", "amount": 317, "destination": "武汉"}, "policy_check")
        update = slot_response({"amount": None}, "policy_check", quotes={"amount": "金额不确定"}, update=True)
        self.policy = Mock()
        self.policy.check.return_value = {"status": "insufficient_information"}
        graph = self.build([initial, update], [])
        first = graph.invoke({"messages": [HumanMessage("武汉住宿317元")]}, self.config)
        old = [*turns(60, "历史备注" * 60), HumanMessage("撤回之前的金额，金额不确定")]
        result = graph.invoke({"messages": old}, self.config)
        self.assertIsNone(result["slot_error"])
        subject = result["slots"]["expense"]
        self.assertEqual(subject["id"], first["slots"]["expense"]["id"])
        self.assertIsNone(subject["values"]["amount"])
        self.assertEqual(subject["evidence"]["amount"]["user_message"], 61)
        reviewer = json.loads(self.seen["reviewer"][-1][-1].content)
        self.assertEqual(reviewer["current_message"], old[-1].content)
        self.assertIsNone(reviewer["proposed_slots"]["expense"]["values"]["amount"])

    def test_old_authorization_never_bypasses_current_content_review(self):
        config = replace(self.settings, context_recent_tokens=256)
        graph = self.build([slot_response()], [call("query_memory", {}, "mem"),
            AIMessage("未保存")], config=config)
        result = graph.invoke({"messages": [HumanMessage("以前我说过请保存"), AIMessage("历史回答"),
                                            HumanMessage("现在先别保存")]}, self.config)
        self.assertFalse(result.get("__interrupt__"))
        self.assertNotIn("preferences", self.memory.read())

    def test_oversized_current_turn_blocks_before_any_model_or_tool_and_keeps_original(self):
        config = replace(self.settings, context_role_input_tokens={"slots": 1024})
        graph = self.build([], [], config=config)
        message = HumanMessage("private-current" * 300)
        result = graph.invoke({"messages": [message]}, self.config)
        self.assertEqual(result["slot_error"]["reason"], "context_budget_exceeded")
        self.assertIn("预算", result["messages"][-1].content)
        self.assertEqual(result["messages"][0], message)
        self.assertEqual(result["pending_turns"][0]["user_message"], 0)
        self.assertEqual(self.seen, {"slots": [], "reviewer": [], "supervisor": []})

    def test_reviewer_budget_failure_does_not_commit_extractor_proposal(self):
        config = replace(self.settings, context_role_input_tokens={"reviewer": 1024})
        graph = self.build([slot_response({"kind": "hotel", "amount": 319}, "policy_check")],
                           [], config=config)
        result = graph.invoke({"messages": [HumanMessage("住宿319元")]}, self.config)
        self.assertEqual(result["slot_error"]["reason"], "context_budget_exceeded")
        self.assertFalse(result.get("slots"))
        self.assertFalse(self.seen["reviewer"])
        self.assertFalse(self.seen["supervisor"])

    @patch("business_travel.graph.InformationProvider")
    def test_supervisor_overflow_after_tool_keeps_result_and_visible_limit(self, information):
        information.return_value.weather.return_value = {
            "status": "unavailable", "reason": "test", "raw": "x" * 60000}
        graph = self.build([slot_response()], [
            call("query_weather", {"city": "杭州", "day": "2099-01-01"}, "weather")])
        result = graph.invoke({"messages": [HumanMessage("查天气")]}, self.config)
        task = next(iter(result["tasks"].values()))
        self.assertEqual(task["reason"], "context_budget_exceeded")
        self.assertEqual(task["status"], "failed")
        self.assertEqual(len(self.seen["supervisor"]), 1)
        self.assertIn("预算", result["messages"][-1].content)
        tool_result = next(m for m in result["messages"] if isinstance(m, ToolMessage))
        self.assertEqual(json.loads(tool_result.content)["raw"], "x" * 60000)
        information.return_value.weather.assert_called_once()

    def test_history_tool_reads_from_current_checkpoint_without_rerunning_supplier(self):
        graph = self.build([slot_response()], [
            call("read_conversation_history", {"message_index": 0}, "history"),
            AIMessage("这是历史记录，不是新授权")])
        result = graph.invoke({"messages": [HumanMessage("旧记录原话"), AIMessage("旧回答"),
                                            HumanMessage("读取第一条")]}, self.config)
        tool_result = next(m for m in result["messages"] if isinstance(m, ToolMessage)
                           and m.name == "read_conversation_history")
        data = json.loads(tool_result.content)
        self.assertEqual(json.loads(data["text"])["content"], "旧记录原话")
        self.assertTrue(data["complete"])
        self.assertEqual(self.memory.read()["trip_history"], [])

    def test_long_failed_turn_recovery_keeps_original_source_index_and_commits_once(self):
        fixture = recovery_fixtures.FailedTurnRecoveryTests()
        fixture.settings, fixture.memory = replace(self.settings, context_recent_tokens=256), self.memory
        fixture.config = self.config
        def outage(_):
            raise ConnectionError("synthetic")
        graph = fixture.build([fixture.initial(), outage, fixture.recover])
        graph.invoke({"messages": [HumanMessage(fixture.initial_text)]}, self.config)
        failed = graph.invoke({"messages": [HumanMessage("金额改为181元")]}, self.config)
        prior = deepcopy(failed["messages"])
        result = graph.invoke({"messages": [*turns(45, "无关背景" * 50),
                                            HumanMessage("重试最初那次金额更正")]}, self.config)
        self.assertIsNone(result["slot_error"])
        self.assertEqual(result["messages"][:len(prior)], prior)
        self.assertEqual(result["slots"]["expense"]["evidence"]["amount"]["user_message"], 1)
        self.assertEqual([str(row["amount"]) for row in fixture.checked], ["137", "181"])

    def test_policy_sources_over_budget_are_not_truncated_or_sent(self):
        source = {"source": "synthetic.docx", "version": "test", "document_id": 1,
                  "source_sha256": "a" * 64, "chunk": "one", "clause": "1",
                  "section": "规则", "text": "不允许省略本条例外。" * 100}
        store = Mock()
        store.search.return_value = {"status": "ok", "sources": [source]}
        raw = Mock()
        raw.bind_tools.return_value = raw
        config = replace(self.settings, context_role_input_tokens={"policy": 1024})
        result = explain_policy(store, lambda role: raw, config, "有哪些规则")
        self.assertEqual(result["reason"], "context_budget_exceeded")
        self.assertEqual(result["rag"]["status"], "incomplete")
        self.assertEqual(result["sources"][0]["text"], source["text"])
        raw.invoke.assert_not_called()


if __name__ == "__main__":
    unittest.main()

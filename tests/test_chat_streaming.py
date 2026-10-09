"""Exercise delivery ordering and disconnects without a buffering HTTP client."""

import asyncio
import json
import threading
import unittest
from unittest.mock import Mock

from fastapi import HTTPException
from langchain_core.messages import AIMessage, HumanMessage

from business_travel.contracts import SearchResult
from business_travel.execution_events import public_graph_events
from business_travel.streaming import ChatEventResponse
from tests import test_travel_graph as fixtures
from tests.test_travel_graph import call, slot_response


class EventResponseTests(unittest.IsolatedAsyncioTestCase):
    async def run_response(self, producer, on_body=None):
        sent = []
        disconnected = asyncio.Event()

        async def receive():
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(event):
            sent.append(event)
            if on_body and event["type"] == "http.response.body" and event.get("body"):
                on_body(event["body"], disconnected)

        await asyncio.wait_for(ChatEventResponse(producer)(
            {"type": "http", "asgi": {"spec_version": "2.0"}}, receive, send), 5)
        return sent

    async def test_progress_delivered_before_work_finishes(self):
        delivered = threading.Event()

        def producer(emit):
            emit("progress", {"stage": "understand", "text": "正在理解"})
            if not delivered.wait(2):
                raise AssertionError("Response buffered until execution completed")
            emit("delta", {"text": "已核验回答"})
            return {"messages": [{"role": "assistant", "text": "已核验回答"}]}

        events = await self.run_response(
            producer, lambda body, _: delivered.set() if b"event: progress" in body else None)
        text = b"".join(e.get("body", b"") for e in events).decode()
        self.assertIn("event: done", text)
        self.assertLess(text.index("event: progress"), text.index("event: delta"))
        self.assertLess(text.index("event: delta"), text.index("event: done"))

    async def test_preflight_error_has_http_status_and_no_stream(self):
        def producer(emit):
            raise HTTPException(409, "确认已失效")

        events = await self.run_response(producer)
        self.assertEqual(events[0]["status"], 409)
        body = b"".join(e.get("body", b"") for e in events)
        self.assertEqual(json.loads(body)["detail"], "确认已失效")

    async def test_failure_after_start_is_redacted_without_done(self):
        def producer(emit):
            emit("progress", {"stage": "understand", "text": "正在理解"})
            raise RuntimeError("private-token-provider-url")

        events = await self.run_response(producer)
        text = b"".join(e.get("body", b"") for e in events).decode()
        self.assertEqual(events[0]["status"], 200)
        self.assertIn("event: error", text)
        self.assertNotIn("event: done", text)
        self.assertNotIn("private-token", text)

    async def test_disconnect_finishes_worker_and_releases_resources(self):
        released = threading.Event()
        started = threading.Event()
        allow_finish = threading.Event()
        threads = []

        def producer(emit):
            threads.append(threading.get_ident())
            try:
                emit("progress", {"stage": "understand", "text": "正在理解"})
                started.set()
                if not allow_finish.wait(2):
                    raise AssertionError("Disconnect was not delivered")
                for _ in range(100):
                    emit("delta", {"text": "已核验回答"})
                return {"messages": []}
            finally:
                threads.append(threading.get_ident())
                released.set()

        def disconnect(body, signal):
            signal.set()
            allow_finish.set()

        await self.run_response(producer, disconnect)
        self.assertTrue(started.is_set())
        self.assertTrue(released.is_set())
        self.assertEqual(threads[0], threads[1])


class GraphEventTests(unittest.TestCase):
    setUp = fixtures.GraphTests.setUp
    tearDown = fixtures.GraphTests.tearDown
    build = fixtures.GraphTests.build

    def test_real_graph_emits_stage_before_supplier_and_only_reviewed_answer(self):
        provider = Mock()
        provider.search.return_value = SearchResult(status="empty")
        graph = self.build([], provider=provider, slots=[
            slot_response({"destination": "B", "check_in": "2099-10-03", "check_out": "2099-10-05"},
                          "plan_trip", scope="hotel")])
        chunks = graph.stream({"messages": [HumanMessage(
            content="B酒店2099-10-03入住2099-10-05退房")]}, self.config,
            stream_mode=["custom", "updates"])
        events = []
        for event in public_graph_events(chunks):
            events.append(event)
        stages = [data["stage"] for kind, data in events if kind == "progress"]
        self.assertIn("understand", stages)
        self.assertIn("query_hotel", stages)
        self.assertNotIn("query_train", stages)
        self.assertNotIn("query_flight", stages)
        self.assertEqual(provider.search.call_count, 1)
        answer = "".join(data["text"] for kind, data in events if kind == "delta")
        self.assertEqual(answer, graph.get_state(self.config).values["messages"][-1].content)
        self.assertIn("未找到候选", answer)

    def test_multiple_task_deltas_equal_committed_answer(self):
        submission = call("SlotSubmission", {"tasks": [
            {"intent": "other", "question": "问好"},
            {"intent": "other", "question": "再说一句"}]}, "batch")
        graph = self.build([AIMessage("您好"), AIMessage("很高兴交流")], slots=[submission])
        events = list(public_graph_events(graph.stream(
            {"messages": [HumanMessage("问好，再说一句")]}, self.config,
            stream_mode=["custom", "updates"])))
        deltas = [data["text"] for kind, data in events if kind == "delta"]
        self.assertEqual(deltas, ["您好", "\n\n很高兴交流"])
        self.assertEqual("".join(deltas), graph.get_state(self.config).values["messages"][-1].content)

    def test_long_reviewed_answer_is_delivered_as_multiple_deltas(self):
        answer = "这是已经完成政策核对的公开回答。" * 40
        events = list(public_graph_events([
            ("updates", {"advance": {"task_answers": [answer]}}),
        ]))
        deltas = [data["text"] for kind, data in events if kind == "delta"]
        self.assertGreater(len(deltas), 1)
        self.assertEqual("".join(deltas), answer)

    def test_unrecognized_custom_data_and_tool_outputs_never_leave_graph(self):
        events = list(public_graph_events([
            ("custom", {"stage": "private", "text": "secret"}),
            ("updates", {"tools": {"messages": ["secret"]}}),
            ("messages", ("secret", {})),
            ("custom", {"stage": "policy", "text": "secret", "arguments": "secret"}),
        ]))
        self.assertEqual(events, [("progress", {"stage": "policy", "text": "正在核对差旅政策"})])

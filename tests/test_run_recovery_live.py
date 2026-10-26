"""隔离 PostgreSQL 中验证执行失败后的核实、分叉和不重放。"""

import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from business_travel.chat_runs import ChatRunStore, request_digest, setup_chat_runs
from business_travel.graph import AgentState
from business_travel.memory_pg import (PostgresTravelMemory, ScopedPostgresSaver,
                                       memory_connection, open_checkpointer)
from business_travel.web import ChatBody, conversation_lock
from tests import test_chat_runs_live as fixtures


@unittest.skipUnless(os.environ.get("TRAVEL_TEST_POSTGRES") == "1", "PostgreSQL integration is opt-in")
class RunRecoveryLiveTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.ChatRunLiveTests.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.ChatRunLiveTests.tearDownClass.__func__)
    send = fixtures.ChatRunLiveTests.send
    payload = fixtures.ChatRunLiveTests.payload

    def setUp(self):
        fixtures.ChatRunLiveTests.setUp(self)
        self.effect = None
        self.fail_finish = False

        def build(*args, checkpointer, **kwargs):
            def respond(state):
                self.calls += 1
                if self.confirm:
                    accepted = interrupt({"action": "save_trip"})
                    if not accepted:
                        return {"messages": [AIMessage("已取消")]}
                if self.effect:
                    memory = PostgresTravelMemory(self.settings, self.identity)
                    memory.save_trip(self.effect)
                    self.writes += 1
                if self.fail:
                    raise RuntimeError("private-provider-token")
                text = next(m.content for m in reversed(state["messages"]) if isinstance(m, HumanMessage))
                return {"messages": [AIMessage("收到：" + text)]}

            def finish(state):
                if self.fail_finish:
                    raise RuntimeError("failure after checkpoint")
                return {}

            builder = StateGraph(AgentState)
            builder.add_node("respond", respond)
            builder.add_node("finish", finish)
            builder.add_edge(START, "respond")
            builder.add_edge("respond", "finish")
            builder.add_edge("finish", END)
            return builder.compile(checkpointer=checkpointer)

        self.builder = build
        item = patch("business_travel.web.build_assistant", side_effect=build)
        item.start()
        self.addCleanup(item.stop)

    def fail_run(self, text="执行失败"):
        self.fail = True
        payload = self.payload(text)
        self.assertEqual(self.send(payload).status_code, 503)
        self.fail = False
        return payload["request_id"]

    def review(self):
        response = self.client.get(self.route + "/recovery")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def recover(self, review):
        return self.client.post(self.route + "/recovery", json={
            "run_id": review["run"]["id"], "review_revision": review["review_revision"],
            "confirmed": True,
        }, headers=self.origin)

    def test_review_fork_preserves_completed_history_without_replay(self):
        good = self.send(self.payload("完整的一轮")).json()
        failed_id = self.fail_run()
        self.assertEqual(self.send(self.payload()).status_code, 409)
        review = self.review()
        self.assertEqual(review["source_checkpoint_id"], good["revision"])
        self.assertEqual(review["run"]["id"], failed_id)
        response = self.recover(review)
        self.assertEqual(response.status_code, 200, response.text)
        target = response.json()["conversation_id"]
        self.assertNotEqual(target, self.conversation)
        history = self.client.get(f"/api/conversations/{target}/chat").json()
        self.assertEqual(history["messages"][:len(good["messages"])], good["messages"])
        self.assertFalse(history["interrupt"])
        self.assertNotIn("执行失败", str(history["messages"]))
        self.assertEqual(self.calls, 2)
        self.assertEqual(self.runs.get(failed_id)["status"], "failed")
        self.assertEqual(self.send(self.payload()).status_code, 409)
        continued = self.client.post(f"/api/conversations/{target}/chat",
                                     json=self.payload("继续新问题"), headers=self.origin)
        self.assertEqual(continued.status_code, 200, continued.text)
        self.assertEqual(self.calls, 3)

    def test_saved_effect_review_and_repeated_confirmation_never_write_again(self):
        self.effect = {"id": uuid4().hex, "destination": "合成测试地点"}
        failed_id = self.fail_run()
        review = self.review()
        self.assertEqual(review["saved_trips"][0]["data"], self.effect)
        first = self.recover(review)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(self.recover(review).json(), first.json())
        self.assertEqual(self.review()["recovery"], first.json())
        self.assertEqual(self.writes, 1)
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.runs.get(failed_id)["status"], "failed")
        # Existing schema setup is additive and does not destroy recovery receipts.
        setup_chat_runs(self.settings)
        self.assertEqual(self.recover(review).json(), first.json())

    def test_new_trip_invalidates_review(self):
        self.fail_run()
        review = self.review()
        self.assertNotIn("preferences", review)
        memory = PostgresTravelMemory(self.settings, self.identity)
        memory.save_trip({"id": uuid4().hex, "note": "another conversation"})
        self.assertEqual(self.recover(review).status_code, 409)
        self.assertEqual(self.recover(self.review()).status_code, 200)
        self.assertEqual(self.calls, 1)

    def test_interrupted_run_can_fork_without_any_execution(self):
        payload = self.payload()
        self.runs.start(payload["request_id"], request_digest(ChatBody(**payload)), None)
        review = self.review()
        self.assertEqual(review["run"]["status"], "interrupted")
        self.assertIsNone(review["source_checkpoint_id"])
        self.assertEqual(self.recover(review).status_code, 200)
        self.assertEqual(self.calls, 0)

    def test_completed_checkpoint_without_ledger_commit_is_not_promoted(self):
        with patch.object(ChatRunStore, "finish", side_effect=RuntimeError("ledger outage")):
            self.assertEqual(self.send(self.payload()).status_code, 503)
        review = self.review()
        self.assertIsNone(review["source_checkpoint_id"])
        recovered = self.recover(review).json()
        history = self.client.get(f"/api/conversations/{recovered['conversation_id']}/chat").json()
        self.assertEqual(history["messages"], [])
        self.assertEqual(self.calls, 1)

    def test_pending_confirmation_is_not_inherited_after_write_then_failure(self):
        good = self.send(self.payload("完整背景")).json()
        self.confirm = True
        pending = self.send(self.payload("请求保存")).json()
        self.assertTrue(pending["interrupt"])
        self.effect = {"id": uuid4().hex, "note": "persisted before failure"}
        self.fail_finish = True
        result = self.send({"request_id": uuid4().hex, "resume": True, "revision": pending["revision"]})
        self.assertEqual(result.status_code, 503)
        review = self.review()
        self.assertEqual(review["source_checkpoint_id"], good["revision"])
        self.assertEqual(len(review["saved_trips"]), 1)
        recovered = self.recover(review).json()
        history = self.client.get(f"/api/conversations/{recovered['conversation_id']}/chat").json()
        self.assertFalse(history["interrupt"])
        self.assertEqual(history["messages"], good["messages"])
        self.assertEqual(self.writes, 1)
        stale = self.client.post(f"/api/conversations/{recovered['conversation_id']}/chat",
            json={"request_id": uuid4().hex, "resume": True, "revision": pending["revision"]},
            headers=self.origin)
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(self.writes, 1)

    def test_checkpoint_write_failure_rolls_back_registry_checkpoint_and_receipt(self):
        self.fail_run()
        review = self.review()
        def counts():
            with memory_connection(self.settings) as connection:
                return tuple(connection.execute(query, params).fetchone()["n"] for query, params in (
                    ("SELECT count(*) AS n FROM conversations WHERE user_id=%s", (self.identity.user_id,)),
                    ("SELECT count(*) AS n FROM checkpoints WHERE thread_id LIKE %s",
                     (self.identity.thread_id(self.conversation).rsplit(":", 1)[0] + ":%",)),
                    ("SELECT count(*) AS n FROM chat_recoveries WHERE user_id=%s", (self.identity.user_id,))))
        before = counts()
        original = ScopedPostgresSaver.put
        def fail_after_write(saver, *args, **kwargs):
            original(saver, *args, **kwargs)
            raise RuntimeError("private-database-details")
        with patch.object(ScopedPostgresSaver, "put", new=fail_after_write):
            result = self.recover(review)
        self.assertEqual(result.status_code, 503)
        self.assertNotIn("private-database-details", result.text)
        self.assertEqual(counts(), before)
        self.assertEqual(self.recover(review).status_code, 200)

    def test_concurrent_confirmation_creates_only_one_child(self):
        self.fail_run()
        review = self.review()
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: self.recover(review), range(2)))
        self.assertTrue(all(item.status_code in {200, 409} for item in responses))
        target = self.recover(review).json()["conversation_id"]
        for response in responses:
            if response.status_code == 200:
                self.assertEqual(response.json()["conversation_id"], target)
        with memory_connection(self.settings) as connection:
            count = connection.execute(
                "SELECT count(*) AS n FROM chat_recoveries WHERE user_id=%s",
                (self.identity.user_id,)).fetchone()["n"]
        self.assertEqual(count, 1)

    def test_locked_source_rejects_review_and_fork(self):
        self.fail_run()
        review = self.review()
        with conversation_lock(self.settings, self.identity, self.conversation):
            self.assertEqual(self.client.get(self.route + "/recovery").status_code, 409)
            self.assertEqual(self.recover(review).status_code, 409)

    def test_fork_failing_before_first_success_keeps_inherited_history(self):
        good = self.send(self.payload("历史上下文")).json()
        self.fail_run()
        child = self.recover(self.review()).json()["conversation_id"]
        self.route = f"/api/conversations/{child}"
        self.fail_run("子会话失败")
        review = self.review()
        self.assertIsNotNone(review["source_checkpoint_id"])
        second = self.recover(review).json()["conversation_id"]
        history = self.client.get(f"/api/conversations/{second}/chat").json()
        self.assertEqual(history["messages"], good["messages"])

    def test_missing_committed_checkpoint_fails_closed(self):
        self.send(self.payload())
        self.fail_run()
        with open_checkpointer(self.settings, self.identity) as saver:
            saver.delete_thread(self.identity.thread_id(self.conversation))
        response = self.client.get(self.route + "/recovery")
        self.assertEqual(response.status_code, 503)

    def test_validation_identity_csrf_and_stale_run(self):
        self.assertEqual(self.client.get(self.route + "/recovery").status_code, 409)
        self.fail_run()
        review = self.review()
        body = {"run_id": review["run"]["id"], "review_revision": review["review_revision"],
                "confirmed": True}
        for change in ({"confirmed": False}, {"confirmed": "true"}, {"user_id": "foreign"},
                       {"review_revision": "x"}, {"run_id": "x"}):
            response = self.client.post(self.route + "/recovery", json={**body, **change}, headers=self.origin)
            self.assertEqual(response.status_code, 422)
        self.assertEqual(self.client.post(self.route + "/recovery", json=body).status_code, 403)
        self.assertEqual(self.client.post(self.route + "/recovery?user_id=x", json=body,
                                         headers=self.origin).status_code, 400)
        stale = {**review, "run": {**review["run"], "id": uuid4().hex}}
        self.assertEqual(self.recover(stale).status_code, 409)
        self.auth.current_user.return_value = {**self.auth.current_user.return_value, "user_id": uuid4().hex}
        self.assertEqual(self.client.get(self.route + "/recovery").status_code, 404)
        self.assertEqual(self.recover(review).status_code, 404)
        self.auth.current_user.return_value = None
        self.assertEqual(self.client.get(self.route + "/recovery").status_code, 401)
        self.assertEqual(self.recover(review).status_code, 401)

    def test_real_assistant_graph_keeps_fact_indices_but_discards_runtime_and_consent(self):
        from unittest.mock import Mock
        from business_travel.graph import build_assistant
        from business_travel.run_recovery import recover_run
        from business_travel.conversations import ConversationStore
        model = Mock()
        model.bind_tools.return_value = model
        memory = PostgresTravelMemory(self.settings, self.identity)
        provider = Mock()
        def build(saver):
            return build_assistant(lambda role: model, self.settings, memory, Mock(),
                                   checkpointer=saver, provider=provider, information=Mock())
        config = {"configurable": {"thread_id": self.identity.thread_id(self.conversation)}}
        slots = {"subjects": {"s1": {"id": "s1", "revision": 1,
                                    "evidence": {"city": {"user_message": 0, "quote": "保留原文"}}}}}
        with open_checkpointer(self.settings, self.identity) as saver:
            graph = build(saver)
            saved = graph.update_state(config, {
                "messages": [HumanMessage("保留原文"), AIMessage("完成")], "slots": slots,
                "tasks": {}, "draft": {"id": "old"},
                "pending_turns": [{"user_message": 0}], "task_queue": ["old"],
            }, as_node="finish")
        run_id = uuid4().hex
        self.runs.start(run_id, "synthetic", None)
        self.runs.finish(run_id, "completed", saved["configurable"]["checkpoint_id"])
        self.runs.start(uuid4().hex, "synthetic", saved["configurable"]["checkpoint_id"])
        self.runs.interrupt_orphans()
        from business_travel.run_recovery import RecoveryBody
        with conversation_lock(self.settings, self.identity, self.conversation):
            review = recover_run(self.settings, self.identity, self.conversation, build)
            recovered = recover_run(self.settings, self.identity, self.conversation, build,
                                    RecoveryBody(run_id=review["run"]["id"],
                                                 review_revision=review["review_revision"], confirmed=True))
        ConversationStore(self.settings, self.identity).require(recovered["conversation_id"])
        with open_checkpointer(self.settings, self.identity) as saver:
            state = build(saver).get_state({"configurable": {
                "thread_id": self.identity.thread_id(recovered["conversation_id"])}})
        self.assertEqual(state.values["slots"], slots)
        self.assertEqual(state.values["messages"][0].content, "保留原文")
        for key in ("draft", "preference_pending", "pending_turns", "task_queue"):
            self.assertFalse(state.values.get(key))
        self.assertFalse(state.next)
        self.assertFalse(state.interrupts)
        model.invoke.assert_not_called()
        provider.search.assert_not_called()

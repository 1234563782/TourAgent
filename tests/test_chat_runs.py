"""Request identity and fail-closed HTTP contracts without network dependencies."""

from uuid import uuid4
import unittest

from langchain_core.messages import AIMessage

from business_travel.chat_runs import public_run, request_digest
from business_travel.web import ChatBody
from tests import test_web_chat as fixtures
from tests.test_web_chat import state


class RunHttpTests(unittest.TestCase):
    setUp = fixtures.ChatTests.setUp
    post = fixtures.ChatTests.post

    def test_request_id_required_on_every_write_route(self):
        for path in ("/api/chat", "/api/chat/stream",
                     "/api/conversations/" + "a" * 32 + "/chat",
                     "/api/conversations/" + "a" * 32 + "/chat/stream"):
            for key in (None, "invalid", "a" * 33):
                payload = {"message": "hello"}
                if key is not None:
                    payload["request_id"] = key
                with self.subTest(path=path, key=key):
                    self.assertEqual(self.client.post(path, json=payload,
                        headers={"Origin": "http://127.0.0.1:8000"}).status_code, 422)
        self.graph.stream.assert_not_called()
        self.runs.start.assert_not_called()

    def completed(self, body):
        return {"id": body.request_id, "request_hash": request_digest(body),
                "status": "completed", "stage": "assistant", "checkpoint_id": "old",
                "base_checkpoint_id": None}

    def test_replay_uses_pinned_checkpoint_not_latest_and_never_executes(self):
        body = ChatBody(request_id=uuid4().hex, message="hello")
        self.runs.get.return_value = self.completed(body)
        self.graph.get_state.return_value = state([AIMessage("old result")], revision="old")
        result = self.post(body.model_dump(exclude_none=True))
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["messages"][-1]["text"], "old result")
        self.assertEqual(self.graph.get_state.call_args.args[0]["configurable"]["checkpoint_id"], "old")
        self.graph.stream.assert_not_called()
        self.runs.start.assert_not_called()

    def test_request_content_conflict_never_executes(self):
        body = ChatBody(request_id=uuid4().hex, message="original")
        self.runs.get.return_value = self.completed(body)
        self.assertEqual(self.post({"request_id": body.request_id, "message": "different"}).status_code, 409)
        self.graph.stream.assert_not_called()

    def test_missing_pinned_result_fails_closed(self):
        body = ChatBody(request_id=uuid4().hex, message="original")
        self.runs.get.return_value = self.completed(body)
        self.graph.get_state.return_value = state(revision=None)
        self.assertEqual(self.post(body.model_dump(exclude_none=True)).status_code, 503)
        self.graph.stream.assert_not_called()

    def test_failed_or_interrupted_blocks_new_write_and_hides_partial_answer(self):
        for status in ("failed", "interrupted"):
            self.runs.latest.return_value = {
                "id": uuid4().hex, "status": status, "stage": "save", "base_checkpoint_id": None}
            self.graph.get_state.return_value = state([AIMessage("uncommitted internal output")])
            self.assertEqual(self.post({"message": "continue"}).status_code, 409)
            result = self.client.get("/api/chat")
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()["messages"], [])
            self.assertTrue(result.json()["run"]["needs_review"])
        self.graph.stream.assert_not_called()

    def test_failure_is_recorded_without_raw_error(self):
        self.graph.stream.side_effect = RuntimeError("private-secret")
        response = self.post({"message": "hello"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.runs.finish.call_args.args[1], "failed")
        self.assertNotIn("private-secret", str(self.runs.finish.call_args))
        self.assertNotIn("private-secret", response.text)

    def test_digest_is_action_specific_but_ignores_request_id(self):
        a = ChatBody(request_id=uuid4().hex, message=" hello ")
        b = ChatBody(request_id=uuid4().hex, message="hello")
        self.assertEqual(request_digest(a), request_digest(b))
        approved = ChatBody(request_id=uuid4().hex, resume=True, revision="v1")
        cancelled = ChatBody(request_id=uuid4().hex, resume=False, revision="v1")
        self.assertNotEqual(request_digest(approved), request_digest(cancelled))
        self.assertNotEqual(request_digest(approved), request_digest(
            ChatBody(request_id=uuid4().hex, resume=True, revision="v2")))

    def test_public_run_is_a_whitelist(self):
        row = {"id": uuid4().hex, "status": "completed", "stage": "assistant",
               "request_hash": "private", "checkpoint_id": "private", "user_id": "private"}
        self.assertEqual(set(public_run(row)), {"id", "status", "progress", "needs_review"})
        self.assertNotIn("private", str(public_run(row)))

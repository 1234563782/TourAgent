"""Conversation routes preserve identity and confirmation boundaries."""

from unittest.mock import Mock, patch
from uuid import uuid4

from langchain_core.messages import HumanMessage

from tests import test_web_chat
from tests.test_web_chat import state


class ConversationTests(test_web_chat.ChatTests):
    def setUp(self):
        super().setUp()
        self.registry = Mock()
        self.registry.require.return_value = {"id": "a" * 32}
        self.registry.list.return_value = {"items": [], "next_offset": None}
        self.registry.create.return_value = {"id": "b" * 32, "title": "新对话"}
        self.factory = patch("business_travel.web.ConversationStore", return_value=self.registry)
        self.factory_mock = self.factory.start()
        self.addCleanup(self.factory.stop)
        self.ident = "a" * 32

    def route(self, suffix="/chat"):
        return "/api/conversations/" + self.ident + suffix

    def send(self, body, suffix="/chat"):
        return self.client.post(self.route(suffix), json={"request_id": uuid4().hex, **body},
                                headers={"Origin": "http://127.0.0.1:8000"})

    def test_conversation_routes_use_verified_identity(self):
        result = self.send({"message": "test"})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["conversation_id"], self.ident)
        self.assertEqual(self.graph.stream.call_args.args[1]["configurable"]["thread_id"],
                         "verified:" + self.ident)
        self.registry.require.assert_called_once_with(self.ident)
        self.assertEqual(self.factory_mock.call_args.args[1].user_id, "verified")
        self.assertEqual(self.mocks[0].call_args.args[2], self.ident)

    def test_foreign_missing_and_invalid_denied_before_graph(self):
        self.registry.require.side_effect = LookupError()
        for suffix in ("/chat", "/chat/stream"):
            self.assertEqual(self.send({"message": "test"}, suffix).status_code, 404)
        self.assertEqual(self.client.get(self.route()).status_code, 404)
        self.graph.stream.assert_not_called()
        self.graph.get_state.assert_not_called()

    def test_confirmation_revision_checked_in_selected_thread(self):
        self.graph.get_state.return_value = state(pending=[{"action": "save_trip"}], revision="v2")
        self.assertEqual(self.send({"resume": True, "revision": "v1"}).status_code, 409)
        self.assertEqual(self.send({"message": "new"}).status_code, 409)
        self.graph.stream.assert_not_called()

    def test_anonymous_all_new_routes(self):
        self.auth.current_user.return_value = None
        self.assertEqual(self.client.get("/api/conversations").status_code, 401)
        self.assertEqual(self.client.post("/api/conversations",
            headers={"Origin": "http://127.0.0.1:8000"}).status_code, 401)
        self.assertEqual(self.client.get(self.route()).status_code, 401)
        self.assertEqual(self.send({"message": "x"}, "/chat/stream").status_code, 401)

    def test_list_create_and_forbidden_query(self):
        self.assertEqual(self.client.get("/api/conversations?offset=50").status_code, 200)
        self.registry.list.assert_called_once_with(offset=50)
        self.assertEqual(self.client.get("/api/conversations?offset=-1").status_code, 422)
        self.assertEqual(self.client.get("/api/conversations?user_id=foreign").status_code, 400)
        self.assertEqual(self.client.post("/api/conversations",
            headers={"Origin": "http://127.0.0.1:8000"}).status_code, 201)

    def test_history_includes_owned_metadata_without_requiring_list(self):
        self.registry.require.return_value = {"id": self.ident, "title": "早期会话"}
        response = self.client.get(self.route())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["conversation"]["title"], "早期会话")
        self.registry.list.assert_not_called()

    def test_stream_chinese_limit_and_no_user_echo(self):
        self.graph.get_state.return_value = state([HumanMessage("private user text")],
                                                  pending=[{"action": "save_trip"}])
        # A valid resume may end at another interrupt with no assistant text.
        result = self.send({"resume": False, "revision": "v1"}, "/chat/stream")
        self.assertEqual(result.status_code, 200)
        self.assertNotIn("event: delta", result.text)
        self.assertIn("event: done", result.text)
        self.graph.get_state.return_value = state()
        self.assertEqual(self.send({"message": "中" * 3000}, "/chat/stream").status_code, 200)

    def test_query_override_cannot_switch_thread(self):
        self.assertEqual(self.client.get(self.route() + "?user_id=x").status_code, 400)
        self.assertEqual(self.send({"message": "x", "conversation_id": "other"}).status_code, 422)
        self.graph.stream.assert_not_called()

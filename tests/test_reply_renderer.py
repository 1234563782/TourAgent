import unittest

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from business_travel.graph import finalize_business_reply
from business_travel.reply_renderer import render_tool_result


class ReplyRendererTests(unittest.TestCase):
    def test_dispatches_trip_missing_fields_through_shared_renderer(self):
        text = render_tool_result("plan_trip", {
            "missing_fields": ["destination", "departure_date"],
        })
        self.assertEqual(text, "请补充或核实：目的地、出发日期。尚未查询行程。")

    def test_keeps_save_trip_status_separate_from_preference_status(self):
        text = render_tool_result("save_trip", {"status": "conflict"})
        self.assertIn("行程草案", text)
        self.assertNotIn("偏好", text)

    def test_unknown_tool_does_not_replace_model_response(self):
        fallback = AIMessage(content="模型原始回复")
        messages = [
            HumanMessage(content="普通聊天"),
            ToolMessage(name="unknown_tool", tool_call_id="unknown", content="{}"),
        ]
        self.assertIs(finalize_business_reply(messages, fallback), fallback)

    def test_malformed_tool_payload_does_not_raise(self):
        fallback = AIMessage(content="未取得业务结果。")
        messages = [
            HumanMessage(content="查询"),
            ToolMessage(name="plan_trip", tool_call_id="trip", content="{"),
        ]
        response = finalize_business_reply(messages, fallback)
        self.assertIn("未取得有效行程草案", response.content)


if __name__ == "__main__":
    unittest.main()

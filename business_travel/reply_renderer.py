"""Deterministic rendering of reviewed business results for chat clients."""

import json

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from .plan_checks import render_draft
from .policy_guard import render_policy
from .task_results import render_task_result
from .weather import render_weather


RENDERABLE_TOOL_NAMES = frozenset({
    "consult_policy",
    "plan_trip",
    "save_preferences",
    "edit_preferences",
    "save_trip",
    "confirm_facts",
    "query_weather",
    "read_task_result",
})

TRIP_FIELD_LABELS = {
    "origin": "出发地",
    "destination": "目的地",
    "departure_date": "出发日期",
    "check_in": "入住日期",
    "check_out": "退房日期",
    "return_date": "返程日期",
    "query_scope": "查询范围（酒店、交通或两者）",
    "journey": "单程或往返",
    "trip_dates": "行程日期先后关系及完整的入住、退房日期",
}

PREFERENCE_STATUS_TEXT = {
    "saved": "已记住你的偏好。",
    "cancelled": "已取消保存偏好，本次未写入。",
    "conflict": "偏好已变化或存在冲突，本次未写入。请查看当前记录后重新确认。",
    "rejected": "本次内容不适合保存为你的长期偏好，未写入。",
    "needs_clarification": "还不能确定这是否是你本人的长期偏好，本次未保存。请说明是长期习惯还是仅限这次行程。",
    "review_unavailable": "偏好信息核对暂时失败，本次未保存，请稍后重试。",
}


def _parse_tool_data(message):
    try:
        data = json.loads(message.content)
    except (ValueError, TypeError):
        data = {}
    return data if isinstance(data, dict) else {}


def _render_confirmation(data):
    return {
        "confirmed": "已记录您对该事项当前表述的确认；不代表业务系统核实或审批通过。",
        "cancelled": "未确认该事项，请说明需要更正的内容。",
    }.get(data.get("status"), "事项不存在或版本已变化，本次未记录确认，请重新核对。")


def _render_trip(data):
    missing = data.get("missing_fields")
    if not isinstance(missing, (list, tuple)):
        missing = []
    if missing:
        labels = "、".join(TRIP_FIELD_LABELS.get(field, field) for field in missing)
        return f"请补充或核实：{labels}。尚未查询行程。"
    if data.get("reason") == "departure_is_in_the_past":
        return "出发日期已过去，请确认新的出发日期。尚未查询行程。"
    if data.get("reason") == "check_in_is_in_the_past":
        return "入住日期已过去，请确认新的入住和退房日期。尚未查询酒店。"
    return render_draft(data.get("draft"))


def _render_preference(data):
    return PREFERENCE_STATUS_TEXT.get(
        data.get("status"),
        "偏好未保存，请检查输入或服务状态。",
    )


def render_tool_result(name, data):
    """Render one validated tool result without invoking a language model."""
    if not isinstance(data, dict):
        data = {}
    renderers = {
        "read_task_result": render_task_result,
        "confirm_facts": _render_confirmation,
        "query_weather": render_weather,
        "consult_policy": render_policy,
        "plan_trip": _render_trip,
        "save_preferences": _render_preference,
        "edit_preferences": _render_preference,
        "save_trip": lambda payload: {
            "saved": "行程草案已保存；不涉及预订或审批。",
            "cancelled": "已取消保存行程草案，本次未写入。",
            "conflict": "行程草案已变化或存在冲突，本次未写入。请重新核对。",
        }.get(payload.get("status"), "行程草案未保存，请检查输入或服务状态。"),
    }
    renderer = renderers.get(name)
    return renderer(data) if renderer else None


def finalize_business_reply(messages, response):
    """Render only this turn's business results, never an earlier turn's."""
    current = []
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            break
        if isinstance(message, ToolMessage):
            current.append(message)

    sections = []
    for message in reversed(current):
        if message.name not in RENDERABLE_TOOL_NAMES:
            continue
        rendered = render_tool_result(message.name, _parse_tool_data(message))
        if rendered:
            sections.append(rendered)
    return AIMessage(content="\n\n".join(sections)) if sections else response

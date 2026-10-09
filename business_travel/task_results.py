"""Version-bound reads of existing business results; never execute the original task."""

from copy import deepcopy

from .plan_checks import render_draft
from .policy_guard import render_policy


def read_task_result(state, task_id, expected_revision):
    unavailable = {"status": "error", "reason": "result_unavailable_or_stale"}
    task = state.get("tasks", {}).get(task_id)
    if (not task or task.get("intent") not in {"plan_trip", "policy_check", "policy_explain"}
            or task.get("revision") != expected_revision
            or task.get("status") not in {"completed", "partial", "needs_clarification", "failed"}):
        return unavailable
    subject = state.get("slots", {}).get("subjects", {}).get(task.get("subject_id"))
    reference = {"subject_id": task.get("subject_id"), "revision": expected_revision}
    if (not subject or subject.get("id") != task.get("subject_id")
            or subject.get("revision") != expected_revision
            or subject.get("intent") != task["intent"]
            or subject.get("validation") != "context_reviewed"
            or task.get("result_ref") != reference):
        return unavailable
    domain = "trip" if task["intent"] == "plan_trip" else "expense"
    result = subject.get("result")
    if (subject.get("domain") != domain or not isinstance(result, dict)
            or any(result.get(key) != value for key, value in reference.items())):
        return unavailable
    return {"status": "ok", "task_id": task_id, "intent": task["intent"],
            **reference, "result": deepcopy(result)}


def task_result_context(slots, tasks):
    """Expose minimal result metadata using the same gate as the read-only tool."""
    state = {"slots": slots, "tasks": {task["id"]: task for task in tasks}}
    context = []
    for task in tasks:
        data = read_task_result(state, task["id"], task.get("revision"))
        if data["status"] != "ok":
            continue
        result = data["result"]
        context.append({
            "task_id": task["id"], "intent": data["intent"],
            "subject_id": data["subject_id"], "revision": data["revision"],
            "result_status": result.get("status"),
            "selected_count": len(result.get("selected") or []),
            "policy_check_statuses": [
                {"status": check.get("status"), "reason": check.get("reason")}
                for check in result.get("policy_checks") or [] if isinstance(check, dict)],
        })
    return context


def render_task_result(data):
    if data.get("status") != "ok":
        return "该任务结果不存在、尚未生成或版本已变化。请重新确认要查看哪个任务的结果；本次未执行查询或核验。"
    intro = "以下为该任务已保存的结果，未重新查询或核验，不代表当前库存、最终价格或审批通过。"
    if data.get("intent") == "plan_trip":
        result = data.get("result") or {}
        quote_note = ("酒店起价不等于指定日期每间每晚含税房价；缺失的报价口径仍需核实。"
                      if any(item.get("kind") == "hotel" for item in result.get("selected", []))
                      else "参考报价不等于最终成交价；缺失的报价口径仍需核实。")
        return (intro + "\n核验范围仅限下列已选候选，不能据此认定所有搜索结果都合规。"
                + quote_note + "\n\n" + render_draft(result))
    return intro + "\n\n" + render_policy(data.get("result") or {})

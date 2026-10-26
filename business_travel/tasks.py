"""Checkpointed task references and deterministic execution transitions."""

import json
import uuid
from copy import deepcopy

from langchain_core.messages import ToolMessage


PENDING_STATUSES = {
    "collecting", "ready", "running", "needs_clarification", "blocked",
    "awaiting_confirmation", "partial", "failed",
}
RESUMABLE_INTENTS = {"plan_trip", "policy_check", "policy_explain"}
CONTROLLABLE_STATUSES = PENDING_STATUSES - {"running", "awaiting_confirmation"}


class TaskControlError(ValueError):
    pass


def validate_task_controls(tasks, controls, slots):
    """Validate references against the pre-turn ledger, before any state is published."""
    seen = set()
    for control in controls:
        ident = control["target_id"]
        task = tasks.get(ident)
        if ident in seen:
            raise TaskControlError("one_control_per_task")
        seen.add(ident)
        if not task or any(task.get(field) != control["expected_" + field]
                           for field in ("revision", "attempt", "status")):
            raise TaskControlError("stale_or_unknown_task_use_current_reference")
        if task["status"] not in CONTROLLABLE_STATUSES:
            raise TaskControlError("task_not_controllable_use_existing_confirmation_if_pending")
        if control["action"] not in {"cancel", "resume"}:
            raise TaskControlError("unsupported_task_control")
        if control["action"] == "resume":
            if task["intent"] not in RESUMABLE_INTENTS:
                raise TaskControlError("delegated_task_requires_fresh_request_not_historical_replay")
            subject = slots.get("subjects", {}).get(task.get("subject_id"))
            if not subject or subject["revision"] != task["revision"]:
                raise TaskControlError("task_subject_changed_submit_reviewed_update_before_resuming")


def _execution_order(tasks, queue):
    remaining = list(dict.fromkeys(queue))
    ordered = []
    while remaining:
        ready = next((ident for ident in remaining
                      if not set(tasks[ident]["dependencies"]).intersection(remaining)), None)
        if ready is None:
            raise TaskControlError("cyclic_task_dependencies")
        remaining.remove(ready)
        ordered.append(ready)
    return ordered


def task_snapshot(tasks, *, pending_only=False):
    fields = ("id", "intent", "question", "canonical_question", "subject_id", "revision", "status",
              "missing_fields", "dependencies", "result_ref", "attempt", "reason", "operation")
    return [{key: task[key] for key in fields if key in task}
            for task in tasks.values()
            if not pending_only or task["status"] in PENDING_STATUSES]


def reconcile_tasks(existing, proposals, slots, controls=()):
    """Coalesce reviewed revisions, without overwriting the canonical subject store."""
    tasks = deepcopy(existing)
    validate_task_controls(tasks, controls, slots)
    controlled = {control["target_id"] for control in controls}
    for proposal in proposals:
        subject = proposal.get("subject")
        if subject and any(tasks[ident].get("subject_id") == subject["id"] for ident in controlled):
            raise TaskControlError("cannot_control_and_update_same_subject_in_one_submission")
    ids = []
    latest = {}
    for index, proposal in enumerate(proposals):
        subject = proposal.get("subject")
        subject_id = subject["id"] if subject else None
        requested_id = proposal.get("target_task_id")
        requested = tasks.get(requested_id) if requested_id else None
        if requested_id and (not requested or requested.get("status") == "cancelled"
                             or requested.get("subject_id") != subject_id):
            raise TaskControlError("stale_or_unknown_target_task")
        if requested and requested["intent"] != proposal["intent"] and proposal.get("operation") != "change_intent":
            raise TaskControlError("intent_change_requires_explicit_operation")
        if requested and proposal.get("operation") in {"update_facts", "change_intent"}:
            base_revision = proposal.get("base_revision")
            if (base_revision != requested["revision"]
                    or subject["revision"] not in {base_revision, base_revision + 1}):
                raise TaskControlError("stale_target_task_revision")
            if proposal["operation"] == "change_intent":
                if (subject["revision"] != base_revision + 1
                        or requested["intent"] == proposal["intent"]
                        or (requested["intent"] == "plan_trip") != (proposal["intent"] == "plan_trip")):
                    raise TaskControlError("invalid_goal_transition")
        prior = requested or next((task for task in tasks.values()
                                   if subject_id and task.get("subject_id") == subject_id
                                   and task["intent"] == proposal["intent"]
                                   and task["status"] != "cancelled"), None)
        ident = prior["id"] if prior else str(uuid.uuid4())
        ids.append(ident)
        latest[ident] = index
        question = proposal["question"]
        canonical_question = proposal.get("canonical_question") or question
        if prior and proposal.get("operation") == "update_facts" and not proposal.get("canonical_question"):
            canonical_question = prior.get("canonical_question") or prior.get("question") or question
        tasks[ident] = {
            "id": ident, "intent": proposal["intent"], "question": question,
            "canonical_question": canonical_question,
            "subject_id": subject_id, "revision": subject["revision"] if subject else None,
            "status": "ready", "missing_fields": [],
            "dependencies": list(prior.get("dependencies", [])) if prior else [], "result_ref": None,
            "attempt": prior.get("attempt", 0) if prior else 0,
        }
        if proposal.get("operation"):
            tasks[ident]["operation"] = proposal["operation"]
    for ident, index in latest.items():
        tasks[ident]["dependencies"] = list(dict.fromkeys([
            *tasks[ident]["dependencies"],
            *(ids[dependency] for dependency in proposals[index].get("depends_on", []))]))
    for control in controls:
        task = tasks[control["target_id"]]
        task.update(status="cancelled" if control["action"] == "cancel" else "ready",
                    result_ref=None, missing_fields=[])
        if control["action"] == "cancel":
            task["reason"] = "user_cancelled"
        else:
            task.pop("reason", None)
    # A changed subject also invalidates tasks of another intent referring to its old version.
    invalidated = set()
    for ident, task in tasks.items():
        if task["status"] == "cancelled":
            continue
        subject = slots.get("subjects", {}).get(task.get("subject_id"))
        if subject and task["revision"] != subject["revision"]:
            task.update(status="blocked", reason="subject_revision_changed",
                        result_ref=None, missing_fields=[])
            invalidated.add(ident)
        elif ident in latest:
            invalidated.add(ident)
    invalidated.update(controlled)
    # Invalidate downstream results transitively, even if those tasks are not reissued this turn.
    frontier = set(invalidated)
    while frontier:
        changed = set()
        for ident, task in tasks.items():
            if (task["status"] != "cancelled" and ident not in invalidated
                    and frontier.intersection(task.get("dependencies", []))):
                cancelled = any(tasks.get(key, {}).get("status") == "cancelled"
                                or tasks.get(key, {}).get("reason") == "dependency_cancelled"
                                for key in task.get("dependencies", []))
                task.update(status="blocked",
                            reason="dependency_cancelled" if cancelled else "dependency_changed",
                            result_ref=None)
                changed.add(ident)
        invalidated.update(changed)
        frontier = changed
    queue = [ident for index, ident in enumerate(ids) if latest[ident] == index]
    queue.extend(control["target_id"] for control in controls)
    return tasks, _execution_order(tasks, queue)


def dispatch_task(tasks, ident, slots):
    task = {**tasks[ident]}
    subject = slots.get("subjects", {}).get(task.get("subject_id"))
    if task["status"] == "cancelled":
        return task, subject
    if task.get("subject_id") and (not subject or subject["revision"] != task["revision"]):
        task.update(status="blocked", reason="subject_revision_changed", result_ref=None)
    elif any(tasks.get(key, {}).get("status") == "cancelled"
             or tasks.get(key, {}).get("reason") == "dependency_cancelled"
             for key in task["dependencies"]):
        task.update(status="blocked", reason="dependency_cancelled", result_ref=None)
    elif any(tasks.get(key, {}).get("status") != "completed"
             or (tasks[key].get("subject_id") and
                 slots.get("subjects", {}).get(tasks[key]["subject_id"], {}).get("revision")
                 != tasks[key]["revision"])
             for key in task["dependencies"]):
        task.update(status="blocked", reason="dependency_not_completed", result_ref=None)
    else:
        task.update(status="running", attempt=task["attempt"] + 1)
        task.pop("reason", None)
    return task, subject


def _business_outcome(name, data):
    if data.get("missing_fields"):
        return "needs_clarification", data["missing_fields"]
    if data.get("reason") in {"departure_is_in_the_past", "check_in_is_in_the_past"}:
        return "needs_clarification", (
            ["departure_date"] if data["reason"] == "departure_is_in_the_past" else ["check_in", "check_out"])
    if name == "plan_trip":
        draft = data.get("draft") or {}
        if draft.get("status") == "draft":
            return "completed", []
        queries = draft.get("query_status", [])
        if not draft or (queries and all(q["status"] not in {"ok", "empty"} for q in queries)):
            return "failed", []
        if draft.get("selection_status") in {"error", "invalid_selection"} and not draft.get("selected"):
            return "failed", []
        return "partial", []
    if name == "consult_policy":
        if data.get("purpose") == "explain" and data.get("rag", {}).get("status") == "clarify":
            return "needs_clarification", data.get("missing_fields", [])
        checks = [item.get("result", {}) for item in data.get("rule_checks", [])]
        missing = list(dict.fromkeys(field for item in checks for field in item.get("missing_fields", [])))
        if missing or any(item.get("status") == "insufficient_information" for item in checks):
            return "needs_clarification", missing
        if any(item.get("status") in {"error", "unavailable"} for item in checks):
            return "failed", []
    status = data.get("status")
    if status in {"error", "unavailable", "unconfigured", "conflict", "rejected"}:
        return "failed", []
    if status == "cancelled":
        return "cancelled", []
    if status in {"empty", "incomplete", "unverified", "insufficient_information"}:
        return "partial", []
    return "completed", []


def complete_task(task, messages, slots):
    outcomes = []
    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        try:
            data = json.loads(message.content)
        except (ValueError, TypeError):
            outcomes.append(("failed", []))
            continue
        if not isinstance(data, dict):
            outcomes.append(("failed", []))
        else:
            outcomes.append(_business_outcome(message.name, data))
    status = "completed"
    missing = []
    for candidate in ("needs_clarification", "failed", "cancelled", "partial"):
        if any(outcome == candidate for outcome, _ in outcomes):
            status = candidate
            break
    for _, fields in outcomes:
        missing.extend(fields)
    result_ref = None
    subject = slots.get("subjects", {}).get(task.get("subject_id"))
    if subject and subject["revision"] == task["revision"] and subject.get("result") is not None:
        result_ref = {"subject_id": subject["id"], "revision": subject["revision"]}
    return {**task, "status": status, "missing_fields": list(dict.fromkeys(missing)),
            "result_ref": result_ref}

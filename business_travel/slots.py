"""Stage structured proposals, review their meaning, then atomically publish state."""

import json
import uuid
from copy import deepcopy
from datetime import date
from typing import Annotated, Literal

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from pydantic import Field, ValidationError

from .contracts import Contract
from .models import model_error_details, safe_model_error_message
from .observability import observed, observation, record_event
from .policy_guard import FactEvidence
from .semantic_review import review_payload
from .task_results import task_result_context
from .tasks import TaskControlError, validate_task_controls
from .context_budget import (slot_history, budgeted_model, ContextBudgetExceeded, CONTEXT_LIMIT_MESSAGE)
from .slot_protocol import (
    ExpensePatch, TripPatch, TurnRoute, TripExtraction, ExpenseExtraction,
    ROUTE_PROMPT, TRIP_PROMPT, EXPENSE_PROMPT, protocol_tool,
    validation_guidance,
    PolicyQuery,
)


class SlotTask(Contract):
    """Internal merge command, never exposed as the model's tool schema."""

    operation: Literal["new_task", "update_facts", "change_intent"] = "new_task"
    intent: Literal["policy_check", "policy_explain", "plan_trip", "other"] = Field(
        description="Route by the user's goal, not completeness. Expense checking uses policy_check "
                    "even with missing facts; explaining applicable limits or conditions uses policy_explain "
                    "even when the user's city and grade are known; "
                    "itinerary planning uses plan_trip. First distinguish reading an existing result "
                    "from requesting new execution: questions about returned candidates, their existing "
                    "compliance checks or uncertainty use other, unless new facts or a fresh check "
                    "are requested. Other also delegates preference CRUD and ordinary conversation "
                    "to the assistant and its tools. "
                    "Delegation does not change expense/trip slots.")
    question: str = Field(default="", description=
        "Faithful task request preserving user intent, attribution and negation, not an answer "
        "or a clarification question. For a follow-up, restate the original goal with updated "
        "conditions as a standalone request. For other, carries the request to the assistant.")
    intent_change_evidence: FactEvidence | None = Field(default=None, description=
        "Required when an update changes an existing subject's task intent in task_context. "
        "Cite the exact CURRENT user words requesting a different operation, not merely new facts. "
        "Without an authorized goal change, keep the existing intent. Independently reviewed.")
    action: Literal["new", "update"] = "new"
    target_id: str | None = None
    target_task_id: str | None = Field(default=None,
        description="update_facts或change_intent要修改的现有任务ID。多任务并存时必须提供。")
    prior_intent: str | None = None
    base_revision: int = Field(default=0, ge=0)
    depends_on: list[int] = Field(default_factory=list,
        description="Zero-based indices of earlier tasks in THIS submission that must complete "
                    "successfully first. Only use dependencies explicitly supported by the request.")
    expense: ExpensePatch | None = Field(default=None,
        description="Required patch for policy_check/policy_explain, including incomplete facts. "
                    "Never set for plan_trip or other.")
    trip: TripPatch | None = Field(default=None,
        description="Required patch for plan_trip only. Policy checking uses expense, not trip.")
    evidence: dict[str, FactEvidence] = Field(default_factory=dict,
        description="Map bare field names to {user_message, quote: exact substring}. Use the current "
                    "user index, or a selected retryable pending-turn index ONLY with explicitly "
                    "authorized recovery.action=retry. Ordinary history is not admissible.")
    policy_query: PolicyQuery | None = None


class TaskFacts(Contract):
    question: str = SlotTask.model_fields["question"]
    depends_on: list[int] = SlotTask.model_fields["depends_on"]
    expense: ExpensePatch | None = SlotTask.model_fields["expense"]
    trip: TripPatch | None = SlotTask.model_fields["trip"]
    evidence: dict[str, FactEvidence] = SlotTask.model_fields["evidence"]
    policy_query: PolicyQuery | None = None


class NewTask(TaskFacts):
    operation: Literal["new_task"]
    intent: Literal["policy_check", "policy_explain", "plan_trip", "other"] = SlotTask.model_fields["intent"]


class UpdateFacts(TaskFacts):
    operation: Literal["update_facts"]
    target_id: str = Field(min_length=1, description="Existing subject ID from current_slots.")
    base_revision: int = Field(ge=1, description="Exact current subject revision.")
    target_task_id: str | None = Field(default=None,
        description="Exact task ID from task_context. Required if multiple goals share the subject.")


class ChangeIntent(UpdateFacts):
    operation: Literal["change_intent"]
    intent: Literal["policy_check", "policy_explain", "plan_trip"] = Field(
        description="New goal explicitly requested by the current user. Cross-domain goals need new_task.")
    intent_change_evidence: FactEvidence


TaskOperation = Annotated[NewTask | UpdateFacts | ChangeIntent, Field(discriminator="operation")]


class TaskControl(Contract):
    action: Literal["resume", "cancel"] = Field(
        description="resume explicitly requests another execution attempt; cancel ends future "
                    "scheduling. Maintaining the current state authorizes neither action.")
    target_id: str = Field(min_length=1)
    expected_revision: int | None = Field(ge=1)
    expected_attempt: int = Field(ge=0)
    expected_status: str = Field(min_length=1)
    evidence: FactEvidence = Field(
        description="Exact current user quote authorizing this control on this specific task.")


class TurnRecovery(Contract):
    action: Literal["retry", "discard"]
    user_messages: list[int] = Field(min_length=1,
        description="Exact pending_turns user_message indices, never arbitrary history.")
    evidence: FactEvidence = Field(description="Exact CURRENT authorization to retry/discard selected failed turns.")


class SlotSubmission(Contract):
    """Server-assembled atomic batch, never sent as a model tool."""

    tasks: list[TaskOperation] = Field(default_factory=list)
    task_controls: list[TaskControl] = Field(default_factory=list,
        description="Explicit resume/cancel requests for unfinished tasks in pending_tasks. "
                    "Never invent task IDs or replay a historical delegated write.")
    recovery: TurnRecovery | None = None


class SlotError(ValueError):
    pass


class TaskClarification(SlotError):
    pass


def submission_tasks(submission, limit):
    if not submission.tasks and not submission.task_controls and not (
            submission.recovery and submission.recovery.action == "discard"):
        raise SlotError("at_least_one_task_or_control_required")
    if len(submission.tasks) + len(submission.task_controls) > limit:
        raise SlotError("too_many_tasks_request_smaller_batch")
    return validate_task_batch(submission.tasks, limit)


def validate_task_batch(tasks, limit):
    """Shared task-only validation, also used by isolated extraction experiments."""
    if len(tasks) > limit:
        raise SlotError("too_many_tasks_request_smaller_batch")
    if len(tasks) > 1 and any(not task.question.strip() for task in tasks):
        raise SlotError("each_task_requires_its_own_question")
    for index, task in enumerate(tasks):
        if len(set(task.depends_on)) != len(task.depends_on) or any(
                dependency < 0 or dependency >= index for dependency in task.depends_on):
            raise SlotError("dependencies_must_reference_unique_earlier_tasks")
    return tasks


def validate_trip_occupancy(changes):
    """Reject incomplete redundant single-room facts; never invent allocation or evidence."""
    if (changes.get("query_scope") == "transport" or changes.get("hotel_rooms") != 1
            or changes.get("hotel_adults") is None or changes.get("hotel_child_ages") is None):
        return
    # Explicit withdrawal remains unknown; semantic review checks whether it was authorized.
    if "room_occupants" in changes and changes["room_occupants"] is None:
        return
    if changes.get("room_occupants") != changes["hotel_adults"] + len(changes["hotel_child_ages"]):
        raise SlotError("single_room_occupancy_requires_reconciliation")


def stage_submission(submission, current, user_messages, *, recovered_indices=()):
    """Mechanical validation ONLY. The returned copy is untrusted until semantic review."""
    task = submission.intent
    if task == "other":
        if submission.expense is not None or submission.trip is not None or submission.evidence:
            raise SlotError("non_business_intent_must_not_change_slots")
        return deepcopy(current)
    domain = "trip" if task == "plan_trip" else "expense"
    patch = getattr(submission, domain)
    if getattr(submission, "expense" if domain == "trip" else "trip") is not None:
        raise SlotError(f"intent_{task}_requires_{domain}_patch_not_"
                        + ("expense" if domain == "trip" else "trip"))
    if patch is None:
        raise SlotError("missing_patch")
    if domain == "trip" and patch.query_scope is None and (
            submission.action == "new" or "query_scope" in patch.model_fields_set):
        raise SlotError("query_scope_required_use_hotel_transport_or_combined_or_request_clarification")
    old = current.get(domain)
    if submission.action == "update":
        if not old or old["id"] != submission.target_id:
            old = current.get("subjects", {}).get(submission.target_id)
        if (not old or old.get("domain", domain) != domain
                or submission.target_id != old["id"] or submission.base_revision != old["revision"]):
            raise SlotError("stale_or_unknown_subject_use_current_id_and_revision")
        merged = deepcopy(old)
    else:
        if submission.target_id is not None or submission.base_revision != 0:
            raise SlotError("new_subject_requires_null_id_and_zero_revision")
        merged = {"id": str(uuid.uuid4()), "revision": 0, "values": {}, "evidence": {},
                  "history": [], "result": None}
    merged["domain"] = domain
    changes = patch.model_dump(mode="json", exclude_unset=True)
    if not set(submission.evidence) <= changes.keys():
        raise SlotError("evidence_must_only_describe_submitted_fields")
    # Repeating an existing value without evidence is a no-op, not a new assertion.
    # Explicit citations still undergo validation, even for unchanged values.
    if submission.action == "update":
        changes = {field: value for field, value in changes.items()
                   if field in submission.evidence or field not in merged["values"]
                   or value != merged["values"][field]}
    latest = len(user_messages) - 1
    citations = {}
    for field in changes:
        # Classification is reviewed against the whole message along with the intent.
        if field == "kind" and changes[field] is not None:
            continue
        evidence = submission.evidence.get(field)
        if (evidence is None or evidence.user_message not in {latest, *recovered_indices}
                or not 0 <= evidence.user_message < len(user_messages)
                or not evidence.quote.strip() or evidence.quote not in user_messages[evidence.user_message]):
            raise SlotError(field + ":cite_exact_current_message")
        citations[field] = {"user_message": evidence.user_message, "quote": evidence.quote,
                            "status": "pending_context_review"}
    if domain == "trip":
        validate_trip_occupancy(changes)
    for field, value in changes.items():
        merged["history"].append({"field": field, "before": merged["values"].get(field),
                                  "after": value, "user_message": citations.get(field, {}).get("user_message", latest)})
        merged["values"][field] = value
        merged.setdefault("provenance", {})[field] = {
            "source": "model_inference" if field == "kind" else "user_statement",
            "review_status": "pending_context_review",
        }
        if field in citations:
            merged["evidence"][field] = citations[field]
    query = (submission.policy_query.model_dump(mode="json", exclude_none=True)
             if submission.policy_query is not None else None)
    scope_changed = query is not None and query != merged.get("policy_query")
    if changes or scope_changed or submission.action == "new" or submission.operation == "change_intent":
        merged["revision"] += 1
        merged.pop("fact_confirmation", None)
    merged["intent"] = task
    merged["last_user_message_index"] = latest
    if submission.action == "new" or submission.operation == "change_intent":
        merged["request_origin"] = min(recovered_indices, default=latest)
    if submission.policy_query is not None:
        merged["policy_query"] = submission.policy_query.model_dump(mode="json", exclude_none=True)
    elif task != "policy_explain":
        merged.pop("policy_query", None)
    if submission.operation == "change_intent":
        merged.setdefault("goal_history", []).append({
            "task_id": submission.target_task_id, "before": submission.prior_intent or old.get("intent"),
            "after": task, "revision": merged["revision"],
            "evidence": submission.intent_change_evidence.model_dump(mode="json"),
        })
    merged.pop("policy_date", None)
    merged["result"] = None
    merged["validation"] = "pending_context_review"
    updated = deepcopy(current)
    subjects = updated.setdefault("subjects", {})
    if current.get(domain):
        prior = deepcopy(current[domain])
        prior["domain"] = domain
        subjects[prior["id"]] = prior
    subjects[merged["id"]] = merged
    updated[domain] = merged
    return updated


def validate_intent_transition(task, users):
    evidence = task.intent_change_evidence
    if evidence is not None and (
            evidence.user_message != len(users) - 1 or not evidence.quote.strip()
            or evidence.quote not in users[-1]):
        raise SlotError("intent_change_requires_current_user_evidence")
    if task.operation == "change_intent" and evidence is None:
        raise SlotError("intent_change_requires_current_user_evidence")


def normalize_task_operation(task, task_context, current):
    """Resolve references without letting fact updates choose a replacement intent."""
    args = task.model_dump(mode="json", exclude_unset=True)
    if task.operation == "new_task":
        return SlotTask.model_validate(args)
    subject = current.get("subjects", {}).get(task.target_id)
    if subject is None:
        subject = next((current[key] for key in ("expense", "trip")
                        if (current.get(key) or {}).get("id") == task.target_id), None)
    if not subject or subject["revision"] != task.base_revision:
        raise SlotError("stale_or_unknown_subject_use_current_id_and_revision")
    candidates = [item for item in task_context
                  if item.get("subject_id") == task.target_id
                  and (task.target_task_id is None or item.get("id") == task.target_task_id)]
    matches = [item for item in candidates
               if item.get("revision") == task.base_revision and item.get("status") != "cancelled"]
    if candidates and not matches:
        raise SlotError("stale_or_cancelled_target_task")
    # Standalone extraction experiments have no task ledger. Only recorded intent
    # on their own subject can supply the goal; never infer it from the new patch.
    if not matches and not task_context and task.target_task_id is None and subject.get("intent"):
        matches = [{"intent": subject["intent"]}]
    if len(matches) != 1:
        if task.target_task_id is not None:
            raise SlotError("unknown_or_cancelled_target_task")
        raise TaskClarification("本次要修改哪一个事项？请说明对应的查询或核验任务。")
    target = matches[0]
    if target.get("status") in {"running", "awaiting_confirmation"}:
        raise SlotError("target_task_requires_existing_confirmation_or_completion")
    if target["intent"] == "other":
        raise SlotError("delegated_task_requires_fresh_request_not_historical_replay")
    if task.operation == "update_facts":
        args["intent"] = target["intent"]
    elif task.intent == target["intent"]:
        raise SlotError("change_intent_requires_different_intent")
    elif (task.intent == "plan_trip") != (target["intent"] == "plan_trip"):
        raise SlotError("cross_domain_goal_requires_independent_new_task")
    args.update(action="update", target_task_id=target.get("id"), prior_intent=target["intent"])
    return SlotTask.model_validate(args)


def conversation_context(messages, limit):
    entries = []
    user_index = -1
    for message in messages:
        if isinstance(message, HumanMessage):
            user_index += 1
        entry = {"role": message.type, "content": message.content}
        if isinstance(message, HumanMessage):
            entry["user_message"] = user_index
        if getattr(message, "tool_calls", None):
            entry["tool_calls"] = message.tool_calls
        if isinstance(message, ToolMessage):
            entry.update(name=message.name, tool_call_id=message.tool_call_id)
        entries.append(entry)
    return entries[-limit:]


def _snapshot(current):
    fields = {"id", "domain", "revision", "values", "evidence", "intent", "policy_query", "request_origin"}
    snapshot = {key: {name: value for name, value in current[key].items() if name in fields}
                for key in ("expense", "trip") if current.get(key)}
    snapshot["subjects"] = {ident: {name: value for name, value in subject.items() if name in fields}
                            for ident, subject in current.get("subjects", {}).items()}
    # Active aliases already contain the full version and evidence. Omit only exact
    # duplicates from the historical map; conflicting versions must remain visible.
    for key in ("expense", "trip"):
        active = snapshot.get(key)
        if active and snapshot["subjects"].get(active.get("id")) == active:
            snapshot["subjects"].pop(active["id"])
    return snapshot


def _current_evidence(evidence, users):
    if not evidence.quote.strip() or evidence.quote not in users[-1]:
        raise SlotError("authorization:cite_exact_current_message")
    return {"user_message": len(users) - 1, "quote": evidence.quote}


def _recovery_selection(submission, pending_turns, users):
    recovery = submission.recovery
    if recovery is None:
        return set(), []
    selected = recovery.user_messages
    entries = {item["user_message"]: item for item in pending_turns}
    evidence = recovery.evidence
    if (len(set(selected)) != len(selected) or not set(selected) <= entries.keys()
            or any(index >= len(users) - 1 or index < 0 for index in selected)):
        raise SlotError("recovery_requires_exact_pending_turns")
    if (evidence.user_message != len(users) - 1 or not evidence.quote.strip()
            or evidence.quote not in users[-1]):
        raise SlotError("recovery_requires_current_authorization")
    if recovery.action == "retry":
        if any(not entries[index]["retryable"] for index in selected):
            raise TaskClarification("失败请求之后事项已经变化，请重新说明现在要采用的条件；未套用旧更正。")
        if any(entries[index]["content"] != users[index] for index in selected):
            raise SlotError("recovery_message_mismatch")
        if (not submission.tasks or submission.task_controls
                or any(isinstance(task, NewTask) and task.intent == "other" for task in submission.tasks)):
            raise SlotError("recovery_business_facts_only_use_fresh_request_for_other_actions")
        return set(selected), selected
    return set(), selected


def _bind_route(route, current, payload, users):
    if route.clarification.strip():
        if route.tasks or route.task_controls or route.recovery:
            raise SlotError("clarification_must_not_include_changes")
        raise TaskClarification(route.clarification)
    subjects = {**current.get("subjects", {})}
    for domain in ("expense", "trip"):
        if current.get(domain):
            subject = current[domain]
            if subject["id"] in subjects and subjects[subject["id"]] != subject:
                raise SlotError("conflicting_subject_snapshot")
            subjects[subject["id"]] = subject
    tasks = []
    for request in route.tasks:
        args = request.model_dump(mode="json", exclude_unset=True)
        if request.operation != "new_task":
            if not request.target_id:
                raise TaskClarification("本次要修改哪一个事项？请说明对应的查询或核验任务。")
            subject = subjects.get(request.target_id)
            if subject is None:
                raise SlotError("stale_or_unknown_subject_use_current_id_and_revision")
            args["base_revision"] = subject["revision"]
            if request.operation == "change_intent":
                args["intent_change_evidence"] = _current_evidence(request.intent_change_evidence, users)
        task = SlotSubmission.model_validate({"tasks": [args]}).tasks[0]
        resolved = normalize_task_operation(task, payload["task_context"], current)
        if resolved.intent != "other":
            args["trip" if resolved.intent == "plan_trip" else "expense"] = {}
        tasks.append(args)
    ledger = {task["id"]: task for task in payload["pending_tasks"]}
    controls = []
    for request in route.task_controls:
        target = ledger.get(request.target_id)
        if target is None:
            raise SlotError("unknown_control_target")
        controls.append({"action": request.action, "target_id": target["id"],
                         "expected_revision": target.get("revision"),
                         "expected_attempt": target["attempt"], "expected_status": target["status"],
                         "evidence": _current_evidence(request.evidence, users)})
    recovery = None
    if route.recovery:
        recovery = {**route.recovery.model_dump(mode="json"),
                    "evidence": _current_evidence(route.recovery.evidence, users)}
    submission = SlotSubmission.model_validate({"tasks": tasks, "task_controls": controls, "recovery": recovery})
    submission_tasks(submission, payload["task_limit"])
    _recovery_selection(submission, payload["pending_turns"], users)
    validate_task_controls(ledger, controls, current)
    targets = [task.target_id for task in submission.tasks if task.operation != "new_task"]
    if len(targets) != len(set(targets)):
        raise SlotError("one_update_per_subject_per_submission")
    if any(ledger[control["target_id"]].get("subject_id") in targets for control in controls):
        raise SlotError("cannot_control_and_update_same_subject_in_one_submission")
    return submission


def _invoke_protocol(model, contract, prompt, payload, messages, settings, context_limit):
    definition = protocol_tool(contract)
    bound = model.bind_tools([definition], tool_choice=contract.__name__, parallel_tool_calls=False)
    if settings is not None:
        payload = slot_history(payload, messages, prompt, [definition], settings, context_limit)
    with observation("slot_protocol", slot_phase=contract.__name__):
        response = bound.invoke([SystemMessage(content=prompt),
            HumanMessage(content=json.dumps(payload, ensure_ascii=False, separators=(",", ":")))])
    calls = response.tool_calls
    if response.invalid_tool_calls or len(calls) != 1 or calls[0]["name"] != contract.__name__:
        raise SlotError(f"submit_exactly_one_{contract.__name__}_call")
    return contract.model_validate(calls[0]["args"]), payload


def propose_slots(model, messages, current, payload, *, settings=None, context_limit=16):
    """Read-only staged extraction shared by production and isolated comparison scripts."""
    users = [message.content for message in messages if isinstance(message, HumanMessage)]
    route, route_payload = _invoke_protocol(
        model, TurnRoute, ROUTE_PROMPT, payload, messages, settings, context_limit)
    submission = _bind_route(route, current, payload, users)
    tasks = [normalize_task_operation(task, payload["task_context"], current) for task in submission.tasks]
    raw = submission.model_dump(mode="json", exclude_unset=True)
    schemas = {}
    for domain, contract, prompt in (
            ("trip", TripExtraction, TRIP_PROMPT), ("expense", ExpenseExtraction, EXPENSE_PROMPT)):
        assigned = [{"task_index": index, "intent": task.intent, "question": task.question,
                     "operation": task.operation, "target_id": task.target_id}
                    for index, task in enumerate(tasks)
                    if task.intent != "other" and (task.intent == "plan_trip") == (domain == "trip")]
        if not assigned:
            continue
        extraction_input = {key: payload[key] for key in (
            "today", "current_user_index", "current_message", "current_slots",
            "conversation", "task_context", "pending_turns")}
        extraction_input.update({key: payload[key] for key in (
            "validation_errors", "validation_guidance") if key in payload})
        extraction, _ = _invoke_protocol(model, contract, prompt, {
            **extraction_input, "extraction_tasks": assigned},
            messages, settings, context_limit)
        if extraction.clarification.strip():
            raise TaskClarification(extraction.clarification.strip())
        expected = {item["task_index"] for item in assigned}
        indices = [patch.task_index for patch in extraction.patches]
        if len(indices) != len(set(indices)) or set(indices) != expected:
            raise SlotError("patches_must_cover_exactly_assigned_task_indices")
        for patch in extraction.patches:
            data = patch.model_dump(mode="json", exclude_unset=True)
            task = tasks[patch.task_index]
            scope_changed = False
            if domain == "expense":
                if task.intent == "policy_explain":
                    if patch.policy_query is None:
                        raise SlotError("policy_query_required")
                    old = current.get("subjects", {}).get(task.target_id, {})
                    if task.target_id and current.get("expense", {}).get("id") == task.target_id:
                        old = current["expense"]
                    merged_values = {**(old.get("values", {}) if task.action == "update" else {}),
                                     **data[domain]}
                    category = patch.policy_query.category
                    if category != "other" and merged_values.get("kind") != category:
                        raise SlotError("policy_category_requires_matching_kind")
                    scope_changed = bool(old) and data["policy_query"] != old.get("policy_query")
                    raw["tasks"][patch.task_index]["policy_query"] = data["policy_query"]
                elif patch.policy_query is not None:
                    raise SlotError("policy_query_only_for_explanations")
            if task.operation == "update_facts" and not data[domain] and not scope_changed:
                raise SlotError("update_facts_requires_submitted_fields")
            raw["tasks"][patch.task_index][domain] = data[domain]
            raw["tasks"][patch.task_index]["evidence"] = {
                key: {"user_message": len(users) - 1 if evidence.user_message is None else evidence.user_message,
                      "quote": evidence.quote}
                for key, evidence in patch.evidence.items()}
        schemas[domain] = contract.model_json_schema()
    return SlotSubmission.model_validate(raw), route_payload, schemas


@observed("slot_extraction")
def extract_slots(model, messages, current, attempts, *, reviewer=None, context_limit=16, task_limit=8,
                  pending_tasks=None, task_context=None, pending_turns=None, settings=None):
    if settings is not None:
        model = budgeted_model(model, settings, "slots")
        if reviewer is not None:
            reviewer = budgeted_model(reviewer, settings, "reviewer")
    users = [message.content for message in messages if isinstance(message, HumanMessage)]
    if not users or not isinstance(users[-1], str):
        return {"status": "error", "reason": "text_user_message_required"}
    if reviewer is None:
        return {"status": "error", "reason": "semantic_reviewer_required"}
    if attempts < 1 or context_limit < 1 or task_limit < 1:
        return {"status": "error", "reason": "invalid_extraction_limits"}
    # Freeze trusted state once for all stages and retries. Reconciliation checks it again.
    current = deepcopy(current)
    payload = {"today": date.today().isoformat(), "current_user_index": len(users) - 1,
               "current_message": users[-1], "current_slots": _snapshot(current),
               "conversation": conversation_context(messages, context_limit), "task_limit": task_limit,
               "pending_tasks": deepcopy(pending_tasks or []),
               "pending_turns": deepcopy(pending_turns or []),
               "task_context": deepcopy(task_context if task_context is not None else pending_tasks or [])}
    payload["available_results"] = task_result_context(current, payload["task_context"])
    errors = []
    last_failure = None
    for attempt in range(1, attempts + 1):
        record_event("slot_attempt", attempt=attempt)
        try:
            submission, review_context, schemas = propose_slots(model, messages, current,
                {**payload, "validation_errors": errors[-1:],
                 "validation_guidance": validation_guidance(errors[-1] if errors else None)},
                settings=settings, context_limit=context_limit)
            recovery = submission.recovery
            recovered, resolved_pending = _recovery_selection(submission, payload["pending_turns"], users)
            tasks = [normalize_task_operation(task, payload["task_context"], current)
                     for task in submission_tasks(submission, task_limit)]
            for task in tasks:
                record_event("slot_attempt", intent=task.intent, operation=task.operation,
                             attempt=attempt)
            targets = [task.target_id for task in tasks if task.action == "update"]
            if len(targets) != len(set(targets)):
                raise SlotError("one_update_per_subject_per_submission")
            controls = [control.model_dump(mode="json") for control in submission.task_controls]
            for control in controls:
                evidence = control["evidence"]
                if (evidence["user_message"] != len(users) - 1 or not evidence["quote"].strip()
                        or evidence["quote"] not in users[-1]):
                    raise SlotError("task_control:cite_exact_current_message")
            ledger = {task["id"]: task for task in payload["pending_tasks"]}
            validate_task_controls(ledger, controls, current)
            updated = deepcopy(current)
            records = []
            for task in tasks:
                validate_intent_transition(task, users)
                updated = stage_submission(task, updated, users, recovered_indices=recovered)
                domain = "trip" if task.intent == "plan_trip" else "expense"
                records.append({"intent": task.intent, "question": task.question or users[-1],
                                "depends_on": task.depends_on,
                                "subject": deepcopy(updated[domain]) if task.intent != "other" else None})
                if task.intent != "other":
                    records[-1].update(operation=task.operation, target_task_id=task.target_task_id,
                                       base_revision=task.base_revision)
            if any(record["subject"] and record["subject"]["id"] == ledger[control["target_id"]].get("subject_id")
                   for record in records for control in controls):
                raise SlotError("cannot_control_and_update_same_subject_in_one_submission")
            for record in records:
                for dependency in record["depends_on"]:
                    previous = records[dependency]
                    if (record["subject"] and previous["subject"]
                            and record["subject"]["id"] == previous["subject"]["id"]):
                        raise SlotError("cannot_depend_on_another_revision_of_same_subject")
            try:
                with observation("slot_review") as span:
                    review = review_payload(reviewer, {**review_context,
                        "task_schema": schemas,
                        "task_control_schema": TaskControl.model_json_schema(),
                        "proposal": submission.model_dump(mode="json", exclude_unset=True),
                        "resolved_tasks": [task.model_dump(mode="json", exclude_unset=True) for task in tasks],
                        "proposed_slots": _snapshot(updated)})
                    span.update(verdict=review.verdict, issue_count=len(review.issues))
                    span.content(output=lambda: review.model_dump(mode="json"))
            except Exception as error:
                details = model_error_details(error, stage="semantic_review")
                return {"status": "error", "reason": "context_budget_exceeded" if isinstance(
                            error, ContextBudgetExceeded) else "semantic_review_unavailable",
                        **({"question": CONTEXT_LIMIT_MESSAGE} if isinstance(error, ContextBudgetExceeded)
                           else {"question": safe_model_error_message(details)}),
                        **details}
            if review.verdict == "clarify":
                return {"status": "clarify", "question": review.question}
            if review.verdict == "revise":
                raise SlotError("semantic_review:" + json.dumps(review.issues, ensure_ascii=False))
            for subject in [*updated.get("subjects", {}).values(),
                            *(updated[key] for key in ("expense", "trip") if updated.get(key)),
                            *(record["subject"] for record in records if record["subject"])]:
                if subject.get("validation") == "pending_context_review":
                    subject["validation"] = "context_reviewed"
                    for origin in subject.get("provenance", {}).values():
                        if origin.get("review_status") == "pending_context_review":
                            origin["review_status"] = "model_reviewed"
                    for evidence in subject["evidence"].values():
                        if evidence.get("status") == "pending_context_review":
                            evidence["status"] = "context_reviewed"
            if recovery:
                for record in records:
                    if record["subject"]:
                        record["subject"]["recovery_authorization"] = recovery.model_dump(mode="json")
                        ident = record["subject"]["id"]
                        updated["subjects"][ident]["recovery_authorization"] = recovery.model_dump(mode="json")
                        domain = record["subject"]["domain"]
                        if updated[domain]["id"] == ident:
                            updated[domain]["recovery_authorization"] = recovery.model_dump(mode="json")
            return {"status": "ok", "intent": records[0]["intent"] if records else "task_control",
                    "slots": updated, "tasks": records, "task_controls": controls,
                    "context_summary": review_context.get("history_summary"),
                    "resolved_pending_turns": resolved_pending}
        except TaskClarification as error:
            return {"status": "clarify", "question": str(error)}
        except ValidationError as error:
            last_failure = model_error_details(error, stage="slot_extraction")
            feedback = [{"field": ".".join(map(str, entry["loc"])), "error": entry["type"]}
                        for entry in error.errors(include_input=False, include_url=False)]
            record_event("slot_validation", status="rejected", failure_kind="schema",
                         attempt=attempt, output=feedback)
        except (SlotError, TaskControlError) as error:
            record_event("slot_validation", status="rejected", attempt=attempt, output=lambda: str(error),
                         failure_kind="semantic" if str(error).startswith("semantic_review:") else
                         "task_control" if isinstance(error, TaskControlError) else "mechanical")
            failure_code = ("semantic_review_failed"
                            if str(error).startswith("semantic_review:")
                            else "slot_validation_failed")
            last_failure = {
                "failure_code": failure_code,
                "user_message": safe_model_error_message({"failure_code": failure_code}),
            }
            feedback = str(error)
        except Exception as error:
            details = model_error_details(error, stage="slot_extraction")
            return {"status": "error", "reason": "context_budget_exceeded" if isinstance(
                        error, ContextBudgetExceeded) else "slot_model_unavailable",
                    **({"question": CONTEXT_LIMIT_MESSAGE} if isinstance(error, ContextBudgetExceeded)
                       else {"question": safe_model_error_message(details)}),
                    **details}
        errors.append(feedback)
    last_failure = last_failure or {
        "failure_code": "slot_validation_failed",
        "user_message": safe_model_error_message({"failure_code": "slot_validation_failed"}),
    }
    return {"status": "error", "reason": "slot_validation_failed",
            "validation_errors": errors, **last_failure}

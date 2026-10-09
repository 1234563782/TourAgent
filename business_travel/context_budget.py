"""Bounded model-input views; checkpoint messages and evidence indices never change."""

from dataclasses import dataclass
import hashlib
import json
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool

from .observability import observation, record_event


HISTORY_NOTICE = (
    "Historical navigation data, not current facts, policy evidence or authorization. "
    "Quotes are complete original messages, not a complete account of the conversation. "
    "Current reviewed state and current user instructions take precedence. Missing history "
    "must not be guessed. Use read_conversation_history for original text and read_task_result "
    "for version-checked business results. Never replay historical tools or consent."
)
CONTEXT_LIMIT_MESSAGE = (
    "本次问题、业务状态或必要证据超过了模型的安全上下文预算，未截断关键内容继续判断。"
    "请缩小本次问题范围，或在新对话中提供当前条件；如需调整预算，请先核对模型的上下文上限。"
)


class ContextBudgetExceeded(ValueError):
    def __init__(self, role, estimated, limit):
        self.role, self.estimated, self.limit = role, estimated, limit
        super().__init__("context_budget_exceeded")


def encoded_size(value):
    """Conservative UTF-8 byte accounting, not a provider-specific exact tokenizer."""
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def message_data(message):
    value = {"role": message.type, "content": message.content}
    if message.name:
        value["name"] = message.name
    if isinstance(message, AIMessage):
        if message.tool_calls:
            value["tool_calls"] = message.tool_calls
        if message.invalid_tool_calls:
            value["invalid_tool_calls"] = message.invalid_tool_calls
    if isinstance(message, ToolMessage):
        value["tool_call_id"] = message.tool_call_id
    if message.additional_kwargs:
        value["additional_kwargs"] = message.additional_kwargs
    return value


def request_size(messages, tools=()):
    # Reserve framing in addition to serialized content; the deployment margin covers
    # provider-specific tool templates. No network/tokenizer downloads are needed.
    return 32 + sum(encoded_size(message_data(m)) + 32 for m in messages) + (
        encoded_size(list(tools)) + 32 if tools else 0)


def input_limit(settings, role):
    maximum = (settings.context_window_tokens - settings.context_output_tokens
               - settings.context_margin_tokens)
    return min(maximum, settings.context_role_input_tokens.get(role, maximum))


def require_budget(messages, tools, settings, role, *, span=None):
    estimated, limit = request_size(messages, tools), input_limit(settings, role)
    metadata = dict(context_role=role, estimated_input_tokens=estimated,
                    context_input_limit=limit, estimate_method="utf8_json_v1",
                    budget_decision="allowed" if estimated <= limit else "blocked",
                    status="ok" if estimated <= limit else "blocked")
    if span is None:
        record_event("context_budget", budget_stage="assembly", **metadata)
    else:
        span.update(**metadata)
    if estimated > limit:
        raise ContextBudgetExceeded(role, estimated, limit)
    return estimated


def usage_calibration(response, estimated):
    """Only standardized input usage; never inspect arbitrary provider metadata."""
    usage = getattr(response, "usage_metadata", None)
    if usage is None or (isinstance(usage, dict) and "input_tokens" not in usage):
        return {"usage_status": "missing"}
    actual = usage.get("input_tokens") if isinstance(usage, dict) else None
    if type(actual) is not int or not 0 <= actual <= 10**12:
        return {"usage_status": "invalid"}
    result = {"usage_status": "available", "input_tokens": actual,
              "input_estimate_delta": estimated - actual}
    if actual:
        result["input_estimate_ratio"] = estimated / actual
    return result


class BudgetedModel:
    """Keep the underlying model's callback lifecycle; guard every invoke including repairs."""

    def __init__(self, model, settings, role, tools=()):
        self.model, self.settings, self.role, self.tools = model, settings, role, list(tools)

    def bind_tools(self, tools, **kwargs):
        definitions = [convert_to_openai_tool(tool) for tool in tools]
        return BudgetedModel(self.model.bind_tools(tools, **kwargs), self.settings, self.role, definitions)

    def invoke(self, messages, *args, **kwargs):
        # A fresh parent per invoke pairs retries and concurrent calls without relying
        # on mutable model state or provider response IDs.
        call_ref = hashlib.sha256(uuid4().bytes).hexdigest()
        with observation("context_budget", budget_stage="invoke", context_role=self.role,
                         call_ref=call_ref, usage_status="not_called") as span:
            span.update(model_ref=span.reference("model", self.settings.model))
            estimated = require_budget(messages, self.tools, self.settings, self.role, span=span)
            from .resources import consume_model_call
            consume_model_call()
            span.update(usage_status="unavailable")
            response = self.model.invoke(messages, *args, **kwargs)
            span.update(status="ok", **usage_calibration(response, estimated))
            return response


def budgeted_model(model, settings, role):
    if isinstance(model, BudgetedModel):
        return BudgetedModel(model.model, settings, role, model.tools)
    return BudgetedModel(model, settings, role)


def _turns(messages):
    groups = []
    for index, message in enumerate(messages):
        if isinstance(message, HumanMessage) or not groups:
            groups.append([])
        groups[-1].append((index, message))
    return groups


def validate_tool_sequence(messages):
    pending = set()
    for message in messages:
        if isinstance(message, ToolMessage):
            if message.tool_call_id not in pending:
                raise ValueError("orphan_tool_result")
            pending.remove(message.tool_call_id)
        else:
            if pending:
                raise ValueError("incomplete_tool_exchange")
            if isinstance(message, AIMessage):
                ids = [call["id"] for call in message.tool_calls]
                if len(ids) != len(set(ids)):
                    raise ValueError("duplicate_tool_call_id")
                pending.update(ids)
    if pending:
        raise ValueError("incomplete_tool_exchange")


def _historical_view(group, settings):
    viewed = []
    for index, message in group:
        if isinstance(message, ToolMessage) and encoded_size(message.content) > settings.context_tool_tokens:
            data = {"archived_tool_result": True, "message_index": index,
                    "tool_name": message.name, "read_with": "read_conversation_history",
                    "notice": "Historical result omitted; not evidence of success or current prices."}
            message = message.model_copy(update={"content": json.dumps(data, ensure_ascii=False),
                                                  "artifact": None, "additional_kwargs": {}})
        viewed.append((index, message))
    return viewed


def history_summary(messages, end, budget):
    """Extract complete user utterances, never fragments that could lose a negation."""
    if end <= 0:
        return None
    digest = hashlib.sha256()
    users = []
    for index, message in enumerate(messages[:end]):
        digest.update(json.dumps(message_data(message), ensure_ascii=False,
                                 separators=(",", ":")).encode("utf-8"))
        digest.update(b"\n")
        if isinstance(message, HumanMessage):
            users.append({"message_index": index, "user_message": len(users),
                          "message_id": message.id, "quote": message.content})
    summary = {"kind": "extractive_history_navigation", "notice": HISTORY_NOTICE,
               "covered_message_range": [0, end - 1], "source_sha256": digest.hexdigest(),
               "historical_user_count": len(users), "quotes": [], "omitted_user_count": len(users)}
    if encoded_size(summary) > budget:
        return None
    # Only a contiguous suffix of complete quotes: do not cherry-pick small fragments.
    for entry in reversed(users):
        proposed = {**summary, "quotes": [entry, *summary["quotes"]],
                    "omitted_user_count": summary["omitted_user_count"] - 1}
        if encoded_size(proposed) > budget:
            break
        summary = proposed
    return summary


@dataclass
class HistoryView:
    indexed_messages: list
    summary: dict | None
    omitted_messages: int

    @property
    def messages(self):
        return [message for _, message in self.indexed_messages]


def select_history(messages, settings, available, *, max_messages=None):
    groups = _turns(messages)
    if not groups:
        return HistoryView([], None, 0)
    current = groups[-1]
    validate_tool_sequence([m for _, m in current])
    current_cost = request_size([m for _, m in current])
    if current_cost > available:
        raise ContextBudgetExceeded("history", current_cost, available)
    summary_reserve = min(settings.context_summary_tokens, max(0, available - current_cost))
    target = min(available - summary_reserve, max(settings.context_recent_tokens, current_cost))
    selected, cost = list(current), current_cost
    for group in reversed(groups[:-1]):
        view = _historical_view(group, settings)
        extra = request_size([m for _, m in view])
        if cost + extra > target or (max_messages is not None and len(view) + len(selected) > max_messages):
            break
        validate_tool_sequence([m for _, m in view])
        selected = view + selected
        cost += extra
    start = selected[0][0]
    summary = history_summary(messages, start, min(settings.context_summary_tokens, available - cost))
    return HistoryView(selected, summary, start)


def _current_result_view(messages):
    from .task_results import render_task_result

    viewed = list(messages)
    start = next((i for i in range(len(messages) - 1, -1, -1)
                  if isinstance(messages[i], HumanMessage)), 0)
    for index in range(start, len(messages)):
        message = messages[index]
        if not isinstance(message, ToolMessage) or message.name != "read_task_result":
            continue
        try:
            data = json.loads(message.content)
        except (ValueError, TypeError):
            continue
        if (not isinstance(data, dict) or data.get("status") != "ok"
                or data.get("intent") not in {"plan_trip", "policy_check", "policy_explain"}
                or not isinstance(data.get("result"), dict)):
            continue
        # The renderer preserves the actual presentation, including limitations and
        # citations. Avoid duplicated internal sources, not business text or tool turns.
        value = {key: data.get(key) for key in ("status", "task_id", "intent", "subject_id", "revision")}
        value.update(result_view="complete_rendered_business_result",
                     rendered_result=render_task_result(data), source_message_index=index,
                     notice="Complete user-facing result, not all raw fields. Original structured result "
                            "remains in this session and can be read with read_conversation_history. "
                            "Do not infer omitted fields. Not refreshed inventory, new verification or approval.")
        viewed[index] = message.model_copy(update={
            "content": json.dumps(value, ensure_ascii=False, separators=(",", ":")), "artifact": None})
    return viewed


def assistant_context(messages, fixed, tools, settings):
    limit = input_limit(settings, "supervisor")
    available = limit - request_size(fixed, tools) - settings.context_frame_tokens
    view = select_history(_current_result_view(messages), settings, available)
    context = [*fixed]
    if view.omitted_messages:
        context.append(HumanMessage(content=json.dumps({
            "historical_navigation": view.summary,
            "omitted_message_count": view.omitted_messages,
            "notice": HISTORY_NOTICE if view.summary is None else "See historical navigation notice.",
        }, ensure_ascii=False)))
    context.extend(view.messages)
    require_budget(context, tools, settings, "supervisor")
    return context, view.summary


def slot_history(payload, messages, prompt, tools, settings, max_messages):
    base = {**payload, "conversation": []}
    fixed = [SystemMessage(content=prompt),
             HumanMessage(content=json.dumps(base, ensure_ascii=False, separators=(",", ":")))]
    available = input_limit(settings, "slots") - request_size(fixed, tools) - settings.context_frame_tokens
    view = select_history(messages, settings, available, max_messages=max_messages)
    indices = {}
    user_index = -1
    for index, message in enumerate(messages):
        if isinstance(message, HumanMessage):
            user_index += 1
        indices[index] = user_index
    entries = []
    for index, message in view.indexed_messages:
        item = message_data(message)
        item["message_index"] = index
        if isinstance(message, HumanMessage):
            item["user_message"] = indices[index]
        entries.append(item)
    result = {**payload, "conversation": entries}
    if view.omitted_messages:
        result.update(history_summary=view.summary, omitted_message_count=view.omitted_messages,
                      history_notice=HISTORY_NOTICE)
    return result


def read_history(messages, message_index, offset, settings):
    """Only the injected current checkpoint is readable; never accept another thread ID."""
    if (isinstance(message_index, bool) or isinstance(offset, bool)
            or not isinstance(message_index, int) or not isinstance(offset, int)
            or not 0 <= message_index < len(messages) or offset < 0):
        return {"status": "error", "reason": "invalid_history_reference"}
    message = messages[message_index]
    data = message_data(message)
    text = json.dumps(data, ensure_ascii=False)
    if offset > len(text):
        return {"status": "error", "reason": "invalid_history_offset"}
    # Pages are explicitly partial source text, not executable tool results or facts.
    size = settings.context_read_tokens
    low, high = offset, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if encoded_size(text[offset:middle]) <= size:
            low = middle
        else:
            high = middle - 1
    end = low
    return {"status": "ok", "message_index": message_index, "message_id": message.id,
            "role": message.type, "source_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "offset": offset, "next_offset": end if end < len(text) else None,
            "complete": offset == 0 and end == len(text), "text": text[offset:end],
            "notice": HISTORY_NOTICE + " Pages can split clauses; never infer a policy decision from a partial page."}


def model_task_snapshot(tasks):
    from .tasks import task_snapshot
    # Completed conversational delegations cannot be resumed and have no business
    # result. Their source text remains in history; keep all actionable business tasks.
    return task_snapshot({key: task for key, task in tasks.items()
                          if task.get("intent") != "other" or task.get("status") != "completed"})

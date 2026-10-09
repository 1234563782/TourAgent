"""Opt-in tracing with independently enabled, bounded and redacted content."""

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from hashlib import sha256
import hmac
import json
import logging
import math
from threading import RLock
from urllib.parse import urlsplit

from langchain_core.callbacks import BaseCallbackHandler
from .telemetry_content import ContentFilter


_active = ContextVar("travel_observation", default=None)
_log = logging.getLogger(__name__)
_NAMES = frozenset({
    "chat", "prepare", "dispatch", "assistant", "tools", "advance", "finish",
    "chat_model", "tool", "plan_trip", "consult_policy", "read_task_result",
    "query_memory", "query_weather", "confirm_facts", "save_trip", "save_preferences",
    "edit_preferences", "slot_extraction", "slot_attempt", "slot_validation",
    "slot_review", "slot_protocol", "slot_commit", "task_result", "planning", "policy_rag",
    "policy_retrieve", "policy_result", "query_flight", "query_train", "query_hotel",
    "candidate_policy", "commute", "context_budget", "read_conversation_history",
})
_ENUMS = {
    "policy_stop_reason": frozenset({"retrieval_budget_exhausted", "repeated_query",
                                   "review_budget_exhausted"}),
    "policy_stage": frozenset({"policy_retrieve", "policy_generate", "policy_citations", "policy_review"}),
    "policy_failure": frozenset({
        "policy_retrieval_failed", "invalid_policy_sources", "invalid_policy_tool_response",
        "invalid_policy_tool_arguments", "unverified_policy_citation", "policy_review_rejected",
        "invalid_policy_evidence_review",
        "clarification_must_request_only_missing_fields", "context_budget_exceeded",
        "personal_applicability_unresolved",
        "model_authentication_failed", "model_balance_exhausted", "model_rate_limited",
        "model_timeout", "model_connection_failed", "model_service_unavailable",
        "model_request_rejected", "model_service_error", "structured_output_validation_failed",
    }),
    "slot_phase": frozenset({"TurnRoute", "TripExtraction", "ExpenseExtraction"}),
    "status": frozenset({
        "ok", "error", "empty", "unavailable", "clarify", "accepted", "revise",
        "rejected", "completed", "awaiting_confirmation", "failed", "interrupted",
        "needs_clarification", "blocked", "cancelled", "running", "pending",
        "incomplete", "insufficient", "conflict", "answered", "partial", "version_changed",
        "insufficient_information", "unverified", "compliant", "non_compliant",
        "draft", "saved", "confirmed",
    }),
    "verdict": frozenset({"accept", "clarify", "revise", "reject", "conflict"}),
    "intent": frozenset({"plan_trip", "policy_check", "policy_explain", "other"}),
    "operation": frozenset({"new_task", "update_facts", "change_intent"}),
    "context_role": frozenset({"supervisor", "slots", "reviewer", "planner", "policy", "policy_reviewer"}),
    "budget_stage": frozenset({"assembly", "invoke"}),
    "budget_decision": frozenset({"allowed", "blocked"}),
    "usage_status": frozenset({"available", "missing", "invalid", "not_called", "unavailable"}),
    "estimate_method": frozenset({"utf8_json_v1"}),
    "failure_kind": frozenset({"schema", "mechanical", "semantic", "task_control"}),
    "error_kind": frozenset({"timeout", "connection", "http", "validation", "other"}),
    "execution_status": frozenset({"completed", "awaiting_confirmation", "failed", "interrupted"}),
}
_COUNTS = frozenset({"attempt", "count", "task_count", "revision", "retrievals",
                     "input_tokens", "output_tokens", "total_tokens", "http_status", "issue_count",
                     "estimated_input_tokens", "context_input_limit"})
_FLAGS = frozenset({"slots_committed", "checkpoint_committed", "inbound", "context_incomplete"})
_REFS = frozenset({"user_ref", "session_ref", "request_ref", "task_ref", "call_ref", "model_ref"})


def safe_metadata(data):
    """A flat allowlist, not recursive regex redaction of arbitrary user objects."""
    if not isinstance(data, dict):
        return {}
    safe = {}
    for key, value in data.items():
        if key in _ENUMS and isinstance(value, str) and value in _ENUMS[key]:
            safe[key] = value
        elif key in _COUNTS and type(value) is int and 0 <= value <= 10**12:
            if key != "http_status" or 400 <= value <= 599:
                safe[key] = value
        elif key in _FLAGS and type(value) is bool:
            safe[key] = value
        elif key == "input_estimate_delta" and type(value) is int and abs(value) <= 10**12:
            safe[key] = value
        elif (key == "input_estimate_ratio" and type(value) in (int, float)
              and 0 <= value <= 10**12 and math.isfinite(value)):
            safe[key] = value
        elif (key in _REFS and isinstance(value, str) and len(value) == 64
              and all(c in "0123456789abcdef" for c in value)):
            safe[key] = value
    return safe


def safe_usage_details(data):
    """Map standard provider usage to Langfuse's native generation fields."""
    if not isinstance(data, dict):
        return {}
    result = {}
    for source, target in (("input_tokens", "input"),
                           ("output_tokens", "output"),
                           ("total_tokens", "total")):
        value = data.get(source)
        if type(value) is int and 0 <= value <= 10**12:
            result[target] = value
    return result


def error_metadata(error):
    from openai import APIConnectionError, APIStatusError, APITimeoutError
    from pydantic import ValidationError
    kind = ("timeout" if isinstance(error, (TimeoutError, APITimeoutError)) else
            "connection" if isinstance(error, (ConnectionError, APIConnectionError)) else
            "http" if isinstance(error, APIStatusError) else
            "validation" if isinstance(error, ValidationError) else "other")
    result = {"status": "error", "error_kind": kind}
    if isinstance(error, APIStatusError):
        result.update(safe_metadata({"http_status": error.status_code}))
    return result


def configuration_problem(settings):
    if not settings.langfuse_enabled:
        return None
    if not all((settings.langfuse_base_url, settings.langfuse_public_key,
                settings.langfuse_secret_key, settings.langfuse_hash_key)):
        return "缺少 LANGFUSE_BASE_URL、PUBLIC_KEY、SECRET_KEY 或 TRAVEL_LANGFUSE_HASH_KEY"
    if len(settings.langfuse_hash_key) < 32:
        return "TRAVEL_LANGFUSE_HASH_KEY 至少需要32个字符，建议使用随机生成值"
    if not 1024 <= settings.langfuse_content_max_chars <= 1048576:
        return "TRAVEL_LANGFUSE_CONTENT_MAX_CHARS 必须在 1024 到 1048576 之间"
    try:
        url = urlsplit(settings.langfuse_base_url)
        valid = (url.scheme in {"http", "https"} and bool(url.hostname)
                 and not url.username and not url.password and not url.query and not url.fragment
                 and url.path in {"", "/"} and url.port != 0)
        valid = valid and (url.scheme == "https" or url.hostname in {"localhost", "127.0.0.1", "::1"})
    except ValueError:
        valid = False
    return None if valid else "LANGFUSE_BASE_URL 必须是 HTTPS 根地址；仅本机自建允许 HTTP"


def _make_client(settings):
    from langfuse import Langfuse
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider

    # Do not register globally: unrelated HTTP/SQL instrumentation must not be exported.
    provider = TracerProvider(resource=Resource({"service.name": "touragent"}))
    content_filter = ContentFilter(settings) if settings.langfuse_capture_content else None
    try:
        client = Langfuse(
            public_key=settings.langfuse_public_key, secret_key=settings.langfuse_secret_key,
            base_url=settings.langfuse_base_url, tracer_provider=provider,
            mask=lambda *, data: content_filter.clean(data) if content_filter else safe_metadata(data),
            sample_rate=1.0, environment="travel", release="content-v2", timeout=5,
            should_export_span=lambda span: span.name in _NAMES,
        )
    except Exception:
        provider.shutdown()
        raise
    return client, provider


class Observation:
    def __init__(self, raw=None, *, name="chat", service=None):
        self.raw, self.name, self.service = raw, name, service

    def content(self, **payload):
        if self.raw is None or not self.service.settings.langfuse_capture_content:
            return
        try:
            cleaned = {key: self.service.content_filter.clean(value() if callable(value) else value)
                       for key, value in payload.items() if key in {"input", "output"}}
            self.raw.update(**cleaned)
        except Exception:
            self.service.warn()

    def update(self, *, usage_details=None, **metadata):
        if self.raw is not None:
            try:
                payload = {
                    "metadata": safe_metadata(metadata),
                    "level": "ERROR" if metadata.get("status") == "error" else "DEFAULT",
                }
                native_usage = safe_usage_details(usage_details)
                if native_usage:
                    payload["usage_details"] = native_usage
                self.raw.update(**payload)
            except Exception:
                self.service.warn()

    def end(self):
        if self.raw is not None:
            try:
                self.raw.end()
            except Exception:
                self.service.warn()

    def child(self, name, *, kind="span", **metadata):
        raw = None
        if self.raw is not None:
            try:
                raw = self.raw.start_observation(
                    name=name if name in _NAMES else "tool", as_type=kind,
                    metadata=safe_metadata(metadata))
            except Exception:
                self.service.warn()
        return Observation(raw, name=name, service=self.service)

    def reference(self, kind, value):
        if self.service is None:
            return ""
        return self.service.reference(kind, value)


@contextmanager
def observation(name, *, kind="span", **metadata):
    parent = _active.get()
    span = parent.child(name, kind=kind, **metadata) if parent else Observation()
    token = _active.set(span)
    try:
        yield span
    except BaseException as error:
        span.update(**error_metadata(error))
        raise
    finally:
        _active.reset(token)
        span.end()


def observed(name):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with observation(name):
                return function(*args, **kwargs)
        return wrapped
    return decorate


def record_event(name, *, input=None, output=None, **metadata):
    with observation(name, **metadata) as span:
        if input is not None:
            span.content(input=input)
        if output is not None:
            span.content(output=output)


class MetadataCallback(BaseCallbackHandler):
    """Explicit model/tool projections; never export serialized credentials or tags."""

    run_inline = True

    def __init__(self, root):
        self.root = root
        self.spans = {}
        self.lock = RLock()

    def _start(self, run_id, parent_run_id, name, kind):
        with self.lock:
            active = _active.get()
            parent = (active if active and active.raw and active is not self.root else
                      self.spans.get(parent_run_id, self.root))
            self.spans[run_id] = parent.child(name, kind=kind)
            return self.spans[run_id]

    def _end(self, run_id, error=None, usage=None, output=None):
        with self.lock:
            span = self.spans.pop(run_id, None)
        if span:
            if output is not None:
                span.content(output=output)
            span.update(**(error_metadata(error) if error is not None else {"status": "ok"}),
                        **safe_metadata(usage), usage_details=usage)
            span.end()

    def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None, **kwargs):
        span = self._start(run_id, parent_run_id, "chat_model", "generation")
        span.content(input=lambda: {"messages": messages, "parameters": self._parameters(kwargs)})

    @staticmethod
    def _parameters(kwargs):
        params = kwargs.get("invocation_params") or {}
        return {key: params[key] for key in (
            "model", "model_name", "temperature", "tools", "functions", "tool_choice",
            "response_format", "parallel_tool_calls") if key in params}

    def on_llm_start(self, serialized, prompts, *, run_id, parent_run_id=None, **kwargs):
        span = self._start(run_id, parent_run_id, "chat_model", "generation")
        span.content(input=lambda: {"prompts": prompts, "parameters": self._parameters(kwargs)})

    def on_llm_end(self, response, *, run_id, **kwargs):
        usage = {}
        # Only standardized numeric usage, never llm_output or provider metadata.
        try:
            message = response.generations[0][0].message
            usage = safe_metadata(getattr(message, "usage_metadata", None))
        except (AttributeError, IndexError, TypeError):
            pass
        self._end(run_id, usage=usage, output=lambda: [
            [getattr(generation, "message", generation.text) for generation in batch]
            for batch in response.generations])

    def on_llm_error(self, error, *, run_id, **kwargs):
        self._end(run_id, error=error)

    def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None, **kwargs):
        name = serialized.get("name") if isinstance(serialized, dict) else None
        span = self._start(run_id, parent_run_id, name if name in _NAMES else "tool", "tool")
        span.content(input=lambda: kwargs.get("inputs") if kwargs.get("inputs") is not None else input_str)

    def on_tool_end(self, output, *, run_id, **kwargs):
        self._end(run_id, output=lambda: output)

    def on_tool_error(self, error, *, run_id, **kwargs):
        # A LangGraph human interrupt is control flow, not a provider failure.
        from langgraph.errors import GraphInterrupt
        if isinstance(error, GraphInterrupt):
            with self.lock:
                span = self.spans.pop(run_id, None)
            if span:
                span.update(status="awaiting_confirmation")
                span.end()
        else:
            self._end(run_id, error=error)

    def close(self):
        with self.lock:
            remaining, self.spans = list(self.spans.values()), {}
        for span in remaining:
            span.update(status="interrupted")
            span.end()


class Telemetry:
    def __init__(self, settings):
        self.settings = settings
        self.content_filter = ContentFilter(settings) if settings.langfuse_capture_content else None
        self.client = self.provider = None
        self.initialized = self.closed = self.warned = False
        self.lock = RLock()

    def warn(self):
        with self.lock:
            if not self.warned:
                self.warned = True
                _log.warning("Langfuse 观测不可用或配置不完整；聊天继续运行，请检查观测配置与连通性。")

    def reference(self, kind, *parts):
        data = json.dumps([kind, *parts], ensure_ascii=True, separators=(",", ":"))
        return hmac.new(self.settings.langfuse_hash_key.encode(), data.encode(), sha256).hexdigest()

    def _client(self):
        with self.lock:
            if self.closed or not self.settings.langfuse_enabled:
                return None
            if not self.initialized:
                self.initialized = True
                if configuration_problem(self.settings):
                    self.warn()
                    return None
                try:
                    self.client, self.provider = _make_client(self.settings)
                except Exception:
                    self.warn()
            return self.client

    @contextmanager
    def request(self, config, identity, session, request_id):
        root, callback, propagation = Observation(), None, None
        traced_config = config
        try:
            client = self._client()
            if client is not None:
                from langfuse import propagate_attributes
                user_ref = self.reference("user", identity.company_id, identity.user_id)
                session_ref = self.reference("session", user_ref, session)
                request_ref = self.reference("request", session_ref, request_id)
                propagation = propagate_attributes(user_id=user_ref, session_id=session_ref)
                propagation.__enter__()
                root = Observation(client.start_observation(
                    name="chat", trace_context={"trace_id": request_ref[:32]},
                    metadata={"user_ref": user_ref, "session_ref": session_ref,
                              "request_ref": request_ref}), service=self)
                callback = MetadataCallback(root)
                traced_config = {**config, "callbacks": [*config.get("callbacks", []), callback]}
        except Exception:
            self.warn()
        token = _active.set(root)
        try:
            yield root, traced_config
        except BaseException as error:
            root.update(**error_metadata(error), execution_status="failed")
            raise
        finally:
            _active.reset(token)
            if callback:
                callback.close()
            root.end()
            if propagation:
                try:
                    propagation.__exit__(None, None, None)
                except Exception:
                    self.warn()

    def shutdown(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
        for resource in (self.client, self.provider):
            if resource:
                try:
                    resource.shutdown()
                except Exception:
                    self.warn()

"""Shared provider compatibility for constrained tool-calling workflows."""

from urllib.parse import urlparse

from langchain_openai import ChatOpenAI
from openai import APIConnectionError, APIStatusError, APITimeoutError


_SAFE_ERROR_MESSAGES = {
    "model_authentication_failed": "模型服务鉴权失败，请检查模型 API Key 和服务配置。",
    "model_balance_exhausted": "模型服务余额或调用额度不足，请补充额度或联系管理员。",
    "model_rate_limited": "模型服务请求过于频繁，触发了供应商限流，请稍后再试。",
    "model_timeout": "模型服务响应超时，请稍后重试。",
    "model_connection_failed": "无法连接模型服务，请检查网络和服务地址。",
    "model_service_unavailable": "模型服务暂时不可用，请稍后重试。",
    "model_request_rejected": "模型服务拒绝了本次请求，请检查模型配置或请求内容。",
    "context_budget_exceeded": (
        "本次问题、业务状态或必要证据超过了模型的安全上下文预算，"
        "请缩小本次问题范围，或在新对话中提供当前条件。"
    ),
    "structured_output_validation_failed": (
        "模型返回的结构化结果未通过校验，请重试，或补充清晰的当前条件。"
    ),
    "slot_validation_failed": (
        "模型输出的事项信息未通过校验，请重试，或补充清晰的当前条件。"
    ),
    "semantic_review_failed": "语义复核未完成，请稍后重试。",
    "model_service_error": "当前请求处理失败，请稍后重试。",
}


_QUOTA_ERROR_CODES = {
    "billing_hard_limit_reached",
    "budget_exceeded",
    "credit_balance_insufficient",
    "insufficient_balance",
    "insufficient_quota",
    "payment_required",
    "quota_exceeded",
}


def _provider_error_code(error):
    """Read only a structured provider code; never classify from free-form text."""
    body = getattr(error, "body", None)
    if not isinstance(body, dict):
        return None
    candidates = [body]
    nested = body.get("error")
    if isinstance(nested, dict):
        candidates.insert(0, nested)
    for candidate in candidates:
        for key in ("code", "type"):
            value = candidate.get(key)
            if isinstance(value, str) and value:
                return value.strip().lower()
    return None


def _is_structured_validation_error(error):
    # Avoid importing LangGraph's tool exception in this shared low-level module.
    return type(error).__name__ in {
        "ValidationError",
        "OutputParserException",
        "ToolInvocationError",
    }


def classify_model_error(error):
    """Return a stable, safe category for model and structured-output failures."""
    try:
        from .context_budget import ContextBudgetExceeded
    except ImportError:  # pragma: no cover - supports isolated utility imports
        ContextBudgetExceeded = ()
    if ContextBudgetExceeded and isinstance(error, ContextBudgetExceeded):
        return "context_budget_exceeded"
    if _is_structured_validation_error(error):
        return "structured_output_validation_failed"
    if isinstance(error, APITimeoutError) or isinstance(error, TimeoutError):
        return "model_timeout"
    if isinstance(error, APIConnectionError) or isinstance(error, ConnectionError):
        return "model_connection_failed"
    if isinstance(error, APIStatusError):
        status = error.status_code
        if status in {401, 403}:
            return "model_authentication_failed"
        if status == 402:
            return "model_balance_exhausted"
        if status == 429:
            if _provider_error_code(error) in _QUOTA_ERROR_CODES:
                return "model_balance_exhausted"
            return "model_rate_limited"
        if 500 <= status <= 599:
            return "model_service_unavailable"
        if 400 <= status <= 499:
            return "model_request_rejected"
    return "model_service_error"


def model_error_details(error, stage=None):
    """Keep diagnosis metadata without exposing response bodies or URLs."""
    cause_code = classify_model_error(error)
    failure_code = "semantic_review_failed" if stage == "semantic_review" else cause_code
    details = {
        "error_category": type(error).__name__,
        "failure_code": failure_code,
        "user_message": safe_model_error_message({
            "failure_code": failure_code,
            "cause_code": cause_code,
        }),
    }
    provider_code = _provider_error_code(error)
    if provider_code in _QUOTA_ERROR_CODES:
        details["provider_code"] = provider_code
    if stage:
        details["stage"] = stage
    if stage == "semantic_review":
        details["cause_code"] = cause_code
    if isinstance(error, APIStatusError) and 400 <= error.status_code <= 599:
        details["http_status"] = error.status_code
    return details


def safe_model_error_message(details):
    """Render only an allow-listed Chinese message for a classified failure."""
    failure_code = (details or {}).get("failure_code", "model_service_error")
    message = _SAFE_ERROR_MESSAGES.get(failure_code, _SAFE_ERROR_MESSAGES["model_service_error"])
    cause_code = (details or {}).get("cause_code")
    if failure_code == "semantic_review_failed" and cause_code and cause_code != failure_code:
        cause = _SAFE_ERROR_MESSAGES.get(cause_code)
        if cause:
            message = f"{message}{cause}"
    return message


def create_model(settings):
    extra_body = settings.model_extra_body
    if extra_body is None and urlparse(settings.base_url).hostname == "api.deepseek.com":
        # This provider rejects forced tool_choice while thinking mode is enabled.
        extra_body = {"thinking": {"type": "disabled"}}
    return ChatOpenAI(model=settings.model, api_key=settings.api_key, base_url=settings.base_url,
                      timeout=settings.timeout, max_retries=0, temperature=0, extra_body=extra_body,
                      max_tokens=settings.context_output_tokens)

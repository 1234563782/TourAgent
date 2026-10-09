"""Public execution stages; no prompts, tool arguments or supplier payloads."""

from langgraph.config import get_stream_writer


STAGE_LABELS = {
    "accepted": "请求已接收",
    "understand": "正在理解本次需求",
    "dispatch": "正在处理当前任务",
    "assistant": "正在组织回答",
    "query_flight": "正在查询航班",
    "query_train": "正在查询火车",
    "query_hotel": "正在查询酒店",
    "select": "正在筛选候选方案",
    "policy": "正在核对差旅政策",
    "policy_retrieve": "正在检索适用政策",
    "policy_explain": "正在整理政策依据",
    "policy_review": "正在复核回答与引用",
    "commute": "正在估算通勤",
    "memory": "正在读取个人记录",
    "weather": "正在查询天气",
    "confirmation": "正在核对确认事项",
    "preferences": "正在处理偏好变更",
    "save": "正在处理行程保存",
}

PUBLIC_DELTA_MAX_CHARS = 80
PUBLIC_DELTA_BOUNDARIES = frozenset("\n。！？；：,.!?; ")


def progress_event(stage):
    if stage not in STAGE_LABELS:
        return None
    return {"stage": stage, "text": STAGE_LABELS[stage]}


def report_stage(stage):
    event = progress_event(stage)
    if event is None:
        return
    try:
        writer = get_stream_writer()
    except RuntimeError:
        # CLI and unit-level planners also run outside a LangGraph context.
        return
    writer(event)


def split_public_text(text, max_chars=PUBLIC_DELTA_MAX_CHARS):
    """Split reviewed text without changing its exact content or ordering."""
    if not isinstance(text, str) or not text:
        return []
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            boundary = max(
                (text.rfind(mark, start + max_chars // 2, end) for mark in PUBLIC_DELTA_BOUNDARIES),
                default=-1,
            )
            if boundary > start:
                end = boundary + 1
        chunks.append(text[start:end])
        start = end
    return chunks


def public_graph_events(chunks):
    """Only reviewed task answers are provisional deltas; done is authoritative."""
    answered = False
    for mode, payload in chunks:
        if mode == "custom" and isinstance(payload, dict):
            event = progress_event(payload.get("stage"))
            if event:
                yield "progress", event
        elif mode == "updates" and isinstance(payload, dict):
            update = payload.get("advance") or {}
            answers = update.get("task_answers") or []
            text = answers[-1] if answers else None
            if isinstance(text, str) and text:
                for index, chunk in enumerate(split_public_text(text)):
                    yield "delta", {"text": ("\n\n" if answered and index == 0 else "") + chunk}
                answered = True

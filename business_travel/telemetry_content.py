"""Bounded, best-effort redaction for explicitly opted-in telemetry payloads."""

from dataclasses import fields
import json
import math
import re
from urllib.parse import unquote, urlsplit

from langchain_core.messages import BaseMessage


REDACTED = "[REDACTED]"
MAX_DEPTH = 24
MAX_NODES = 12000
_PRIVATE_KEYS = frozenset({
    "password", "passwd", "pwd", "authorization", "cookie", "setcookie",
    "token", "accesstoken", "refreshtoken", "apikey", "secret", "secretkey",
    "publickey", "hashkey", "credential", "credentials", "databaseurl",
    "userid", "companyid", "sessionid", "threadid", "email", "phone", "mobile",
    "idcard", "identitynumber", "passport", "姓名", "手机号", "身份证", "密码", "密钥",
})
_ASSIGNMENT = re.compile(
    r"""(?i)((?:api[_-]?key|password|passwd|access[_-]?token|refresh[_-]?token|"""
    r"""secret(?:[_-]?key)?|token|authorization|cookie|密码|密钥)\s*["']?\s*[:=：]\s*)"""
    r"""(?:\[REDACTED\]|"[^"]*"|'[^']*'|[^\s,;&}\]]+)""")
_BEARER = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+")
_KEY = re.compile(r"\b(?:sk|pk)-[A-Za-z0-9_-]{8,}")
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")
_IDCARD = re.compile(r"(?<!\w)\d{17}[\dXx](?!\w)")
_URL_USER = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)[^/\s@]+@")
_BINARY = re.compile(r"(?i)data:[^;\s]+;base64,[A-Za-z0-9+/=\s]+")
_COOKIE_HEADER = re.compile(r"(?im)(\b(?:set-cookie|cookie)\s*:)[^\r\n]+")
_MEDIA_KEYS = frozenset({"image_url", "input_audio", "audio", "video_url", "file_data", "image"})


def private_key(key):
    normalized = re.sub(r"[\W_]+", "", key).lower()
    return normalized in _PRIVATE_KEYS or any(
        normalized.endswith(suffix) for suffix in (
            "apikey", "password", "accesstoken", "refreshtoken",
            "secretkey", "hashkey", "databaseurl"))


class ContentFilter:
    def __init__(self, settings):
        self.limit = settings.langfuse_content_max_chars
        secrets = set()
        for field in fields(settings):
            value = getattr(settings, field.name)
            if not isinstance(value, str) or not value:
                continue
            if private_key(field.name) or field.name in {
                    "amap_key", "langfuse_public_key", "tuniu_flight_token",
                    "tuniu_train_token", "tuniu_hotel_token"}:
                secrets.add(value)
            if "url" in field.name:
                try:
                    url = urlsplit(value)
                    if url.password:
                        secrets.update((url.password, unquote(url.password)))
                    if url.username:
                        secrets.update((url.username, unquote(url.username)))
                except ValueError:
                    pass
        self.secrets = tuple(sorted(secrets, key=len, reverse=True))

    def text(self, value):
        for secret in self.secrets:
            value = value.replace(secret, REDACTED)
        value = _BINARY.sub("[OMITTED media]", value)
        value = _COOKIE_HEADER.sub(r"\1[REDACTED]", value)
        value = _URL_USER.sub(r"\1[REDACTED]@", value)
        value = _BEARER.sub(REDACTED, value)
        value = _ASSIGNMENT.sub(lambda match: match[1] + REDACTED, value)
        for pattern in (_KEY, _EMAIL, _PHONE, _IDCARD):
            value = pattern.sub(REDACTED, value)
        return value

    def clean(self, value):
        """Never stringify unknown objects or pass binary/media objects to the SDK."""
        remaining, nodes, truncated = self.limit, 0, False
        seen = set()

        def omit(reason):
            nonlocal truncated
            truncated = True
            return "[OMITTED " + reason + "]"

        def walk(item, depth=0):
            nonlocal remaining, nodes
            nodes += 1
            if depth > MAX_DEPTH or nodes > MAX_NODES or remaining <= 0:
                return omit("content limit")
            if item is None or type(item) in (bool, int):
                remaining -= 16
                return item
            if type(item) is float:
                remaining -= 24
                return item if math.isfinite(item) else None
            if isinstance(item, str):
                # Omit oversized strings whole: never export an unredacted secret prefix.
                if len(item) > self.limit:
                    return omit("oversized text")
                if item.lstrip().startswith(("{", "[")):
                    try:
                        return walk(json.loads(item), depth + 1)
                    except (ValueError, RecursionError):
                        pass
                text = self.text(item)
                if len(text) > remaining:
                    return omit("text budget")
                remaining -= len(text)
                return text
            if isinstance(item, BaseMessage):
                item = {key: getattr(item, key) for key in (
                    "type", "content", "name", "tool_calls", "invalid_tool_calls", "tool_call_id")
                    if hasattr(item, key)}
            if type(item) not in (dict, list, tuple):
                return "[OMITTED unsupported object]"
            if id(item) in seen:
                return omit("cycle")
            seen.add(id(item))
            try:
                if isinstance(item, dict):
                    result = {}
                    for key, child in item.items():
                        if remaining <= 0 or nodes >= MAX_NODES:
                            result["_omitted"] = omit("content limit")
                            break
                        if not isinstance(key, str):
                            continue
                        safe_key = walk(key, depth + 1)
                        if not isinstance(safe_key, str):
                            safe_key = "[OMITTED key]"
                        result[safe_key] = (
                            REDACTED if private_key(key) else "[OMITTED media]"
                            if key in _MEDIA_KEYS else walk(child, depth + 1))
                    return result
                result = []
                for child in item:
                    if remaining <= 0 or nodes >= MAX_NODES:
                        result.append(omit("content limit"))
                        break
                    result.append(walk(child, depth + 1))
                return result
            finally:
                seen.remove(id(item))

        try:
            result = walk(value)
            if truncated:
                result = {"_truncated": True, "content": result}
            # Include JSON framing in the final bound; do not truncate serialized JSON.
            if len(json.dumps(result, ensure_ascii=False)) > self.limit:
                return {"_truncated": True, "reason": "serialized content limit"}
            return result
        except Exception:
            return {"_omitted": "content filtering failed"}

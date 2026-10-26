"""Bounded DashScope text reranking; errors never contain provider payloads."""

import json
import math
from urllib.parse import urlsplit

import httpx

from .resources import consume_model_call


class RerankError(ValueError):
    pass


class QwenReranker:
    def __init__(self, settings, *, transport=None):
        self.settings = settings
        self.transport = transport

    def configuration_error(self):
        s = self.settings
        if not s.rerank_api_key:
            return "rerank_key_missing"
        try:
            url = urlsplit(s.rerank_base_url)
            valid = (url.scheme == "https" and url.hostname and not url.username
                     and not url.password and not url.query and not url.fragment
                     and url.path.rstrip("/") == "/api/v1" and url.port != 0
                     and "{" not in url.netloc and "<" not in url.netloc)
        except ValueError:
            valid = False
        if not valid or not s.rerank_model:
            return "rerank_configuration_required"
        return None

    def rank(self, query, documents):
        reason = self.configuration_error()
        if reason:
            raise RerankError(reason)
        if (not isinstance(query, str) or not query.strip() or len(query) > 4000
                or not isinstance(documents, list) or not 1 <= len(documents) <= 200
                or any(not isinstance(text, str) or not text.strip() for text in documents)):
            raise RerankError("rerank_invalid_input")
        s = self.settings
        # Quota failures propagate; falling back must not bypass a request limit.
        consume_model_call()
        try:
            with httpx.Client(timeout=s.rerank_timeout, transport=self.transport,
                              trust_env=False, follow_redirects=False) as client:
                with client.stream(
                    "POST", s.rerank_base_url.rstrip("/") + "/services/rerank/text-rerank/text-rerank",
                    headers={"Authorization": f"Bearer {s.rerank_api_key}"},
                    json={"model": s.rerank_model,
                          "input": {"query": query, "documents": documents},
                          "parameters": {"top_n": len(documents), "return_documents": False}},
                ) as response:
                    if response.status_code != 200:
                        raise RerankError("rerank_request_rejected")
                    data = bytearray()
                    for part in response.iter_bytes():
                        data.extend(part)
                        if len(data) > 1024 * 1024:
                            raise RerankError("rerank_response_too_large")
            body = json.loads(data)
            results = body["output"]["results"]
            if not isinstance(results, list) or len(results) != len(documents):
                raise RerankError("rerank_invalid_results")
            scores = {}
            for item in results:
                index, score = item["index"], item["relevance_score"]
                if (type(index) is not int or not 0 <= index < len(documents) or index in scores
                        or type(score) not in (float, int) or not math.isfinite(score)
                        or not 0 <= score <= 1):
                    raise RerankError("rerank_invalid_results")
                scores[index] = float(score)
            usage = body.get("usage") or {}
            tokens = usage.get("total_tokens") if isinstance(usage, dict) else None
            return {"results": [{"index": i, "score": scores[i]}
                                for i in sorted(scores, key=lambda i: (-scores[i], i))],
                    "usage": {"total_tokens": tokens} if type(tokens) is int and tokens >= 0 else {}}
        except RerankError:
            raise
        except httpx.TimeoutException:
            raise RerankError("rerank_timeout") from None
        except httpx.RequestError:
            raise RerankError("rerank_connection_failed") from None
        except (ValueError, KeyError, TypeError):
            raise RerankError("rerank_invalid_response") from None

"""Offline API contracts and production reranking; no real .env or services."""

import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

import httpx

from business_travel.policy_rerank import QwenReranker, RerankError
from business_travel.resources import CallBudgetExceeded
from business_travel.settings import Settings
from evals.rag_retrieval_eval import run_eval
from test_policy_rag import chunk_row, database_doc, settings, store_with_db


def response_body(scores=(0.1, 0.9)):
    return {"output": {"results": [{"index": i, "relevance_score": score}
                                   for i, score in enumerate(scores)]},
            "usage": {"total_tokens": 42}}


class ClientTests(unittest.TestCase):
    def client(self, handler, **changes):
        config = settings(rerank_api_key="synthetic-rerank-key", **changes)
        return QwenReranker(config, transport=httpx.MockTransport(handler))

    def test_native_protocol_model_index_mapping_and_usage(self):
        def handler(request):
            self.assertEqual(str(request.url),
                "https://maas.qianwenaiapi.com/api/v1/services/rerank/text-rerank/text-rerank")
            self.assertEqual(request.headers["Authorization"], "Bearer synthetic-rerank-key")
            self.assertEqual(json.loads(request.content), {
                "model": "qwen3.7-text-rerank", "input": {"query": "question", "documents": ["a", "b"]},
                "parameters": {"top_n": 2, "return_documents": False}})
            return httpx.Response(200, json=response_body())
        data = self.client(handler, rerank_model="qwen3.7-text-rerank").rank("question", ["a", "b"])
        self.assertEqual(data["results"], [{"index": 1, "score": 0.9}, {"index": 0, "score": 0.1}])
        self.assertEqual(data["usage"], {"total_tokens": 42})

    def test_ties_keep_rrf_input_order(self):
        body = response_body((0.5, 0.5))
        body["output"]["results"].reverse()
        result = self.client(lambda r: httpx.Response(200, json=body)).rank("q", ["a", "b"])
        self.assertEqual([r["index"] for r in result["results"]], [0, 1])

    def test_invalid_indices_scores_counts_and_json_rejected(self):
        results = response_body()["output"]["results"]
        bodies = [None, {}, {"output": None}, {"output": {"results": []}},
                  {"output": {"results": [results[0], results[0]]}},
                  {"output": {"results": [{"index": -1, "relevance_score": 0.2}, results[1]]}},
                  {"output": {"results": [{"index": True, "relevance_score": 0.2}, results[1]]}}]
        for score in (True, -0.1, 1.1, "0.5", float("nan"), float("inf")):
            bodies.append({"output": {"results": [{"index": 0, "relevance_score": score}, results[1]]}})
        for body in bodies:
            with self.subTest(body=body), self.assertRaises(RerankError):
                raw = json.dumps(body).encode()
                self.client(lambda r: httpx.Response(200, content=raw)).rank("q", ["a", "b"])
        with self.assertRaises(RerankError):
            self.client(lambda r: httpx.Response(200, text="private-invalid-json")).rank("q", ["a"])

    def test_http_failures_do_not_retry_or_expose_response(self):
        for status in (301, 401, 429, 500):
            handler = Mock(return_value=httpx.Response(status, text="private-provider-error"))
            with self.subTest(status=status), self.assertRaisesRegex(RerankError, "rerank_request_rejected"):
                self.client(handler).rank("q", ["a"])
            self.assertEqual(handler.call_count, 1)

    def test_network_failures_have_fixed_codes(self):
        for exception, reason in ((httpx.ReadTimeout("private"), "rerank_timeout"),
                                  (httpx.ConnectError("private"), "rerank_connection_failed")):
            with self.subTest(reason=reason), self.assertRaisesRegex(RerankError, reason):
                self.client(Mock(side_effect=exception)).rank("q", ["a"])

    def test_oversized_response_refused(self):
        with self.assertRaisesRegex(RerankError, "rerank_response_too_large"):
            self.client(lambda r: httpx.Response(200, content=b"x" * (1024 * 1024 + 1))).rank("q", ["a"])

    def test_invalid_configuration_and_inputs_never_call_provider(self):
        handler = Mock(side_effect=AssertionError("must stay offline"))
        for url in ("http://example.com/api/v1", "https://user:secret@example.com/api/v1",
                    "https://example.com/api/v1?key=secret", "https://example.com/compatible-api/v1",
                    "https://{WorkspaceId}.example.com/api/v1", "https://example.com:bad/api/v1"):
            with self.subTest(url=url), self.assertRaises(RerankError):
                self.client(handler, rerank_base_url=url).rank("q", ["a"])
        client = self.client(handler)
        for query, documents in (("", ["a"]), ("q", []), ("q", [""]), ("q", [None])):
            with self.subTest(query=query, documents=documents), self.assertRaises(RerankError):
                client.rank(query, documents)
        handler.assert_not_called()

    def test_quota_is_consumed_and_exhaustion_does_not_fallback_inside_client(self):
        handler = Mock(return_value=httpx.Response(200, json=response_body((0.5,))))
        with patch("business_travel.policy_rerank.consume_model_call") as consume:
            self.client(handler).rank("q", ["a"])
            consume.assert_called_once_with()
        handler.reset_mock()
        with patch("business_travel.policy_rerank.consume_model_call", side_effect=CallBudgetExceeded("quota")):
            with self.assertRaises(CallBudgetExceeded):
                self.client(handler).rank("q", ["a"])
        handler.assert_not_called()


class ProductionTests(unittest.TestCase):
    def store(self, rows, *, handler=None, **changes):
        store, connection, _ = store_with_db(rows)
        store.settings = replace(store.settings, rerank_api_key="synthetic-rerank-key", **changes)
        store.reranker = QwenReranker(store.settings, transport=httpx.MockTransport(handler or (
            lambda r: httpx.Response(200, json=response_body((0.1, 0.9, 0.3))))))
        return store, connection

    def test_rerank_sees_beyond_top_k_then_retains_scope_and_related_evidence(self):
        store, conn = self.store([
            [database_doc(scope_clauses=["scope"])],
            [{"chunk_id": "a"}, {"chunk_id": "b"}, {"chunk_id": "c"}], [],
            [chunk_row("c"), chunk_row("a"), chunk_row("b")],
            [chunk_row("b", ["linked"])], [chunk_row("scope")], [chunk_row("linked")],
        ], policy_top_k=1)
        result = store.search_for_facts("q", {}, include_rankings=True)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["rankings"]["rrf_ids"], ["a"])
        self.assertEqual(result["rankings"]["selected_ids"], ["b"])
        self.assertEqual([s["chunk"] for s in result["sources"]], ["b", "scope", "linked"])
        self.assertFalse(result["context_incomplete"])
        self.assertEqual(result["rankings"]["rerank"]["usage"], {"total_tokens": 42})
        for call in conn.execute.call_args_list:
            if "policy_chunks" in call.args[0]:
                self.assertIn("document_id=%s", call.args[0])

    def test_candidate_pool_is_bounded_and_duplicate_ids_are_removed(self):
        def handler(request):
            self.assertEqual(len(json.loads(request.content)["input"]["documents"]), 2)
            return httpx.Response(200, json=response_body())
        store, _ = self.store([[database_doc()],
            [{"chunk_id": x} for x in ("a", "b", "c")], [{"chunk_id": "a"}],
            [chunk_row("a"), chunk_row("b")], [chunk_row("b")]],
            policy_top_k=1, rerank_candidates=2, handler=handler)
        result = store.search("q", _include_rankings=True)
        self.assertEqual(result["rankings"]["rerank"]["candidate_ids"], ["a", "b"])

    def test_rrf_switch_missing_key_and_missing_url_preserve_baseline(self):
        for changes, status in (({"policy_rerank_mode": "rrf"}, "disabled"),
                                ({"rerank_api_key": ""}, "fallback"),
                                ({"rerank_base_url": ""}, "fallback")):
            with self.subTest(changes=changes):
                store, _, _ = store_with_db([[database_doc()], [{"chunk_id": "a"}], [], [chunk_row("a")]])
                config = replace(store.settings, rerank_api_key="synthetic-rerank-key")
                store.settings = replace(config, **changes)
                store.reranker = QwenReranker(store.settings, transport=httpx.MockTransport(
                    Mock(side_effect=AssertionError("must not call"))))
                result = store.search("q", _include_rankings=True)
                self.assertEqual([s["chunk"] for s in result["sources"]], ["a"])
                self.assertEqual(result["rankings"]["rerank"]["status"], status)
                if "rerank_base_url" in changes:
                    self.assertEqual(result["rankings"]["rerank"]["reason"], "rerank_configuration_required")

    def test_api_failure_returns_exact_original_rrf_order(self):
        store, _ = self.store([[database_doc()],
            [{"chunk_id": "a"}, {"chunk_id": "b"}], [],
            [chunk_row("a"), chunk_row("b")], [chunk_row("a")]], policy_top_k=1,
            handler=lambda r: httpx.Response(401, text="private-error"))
        result = store.search("q", _include_rankings=True)
        self.assertEqual(result["rankings"]["selected_ids"], ["a"])
        self.assertEqual(result["rankings"]["rerank"]["reason"], "rerank_request_rejected")
        self.assertNotIn("private-error", json.dumps(result))

    def test_exact_lodging_row_is_not_filtered_by_reranker(self):
        store, _ = self.store([[database_doc(policy_profile="university")],
            [{"chunk_id": "a"}, {"chunk_id": "b"}], [], [chunk_row("a"), chunk_row("b")],
            [], [chunk_row("b"), chunk_row("exact-lodging")]], policy_top_k=1,
            handler=lambda r: httpx.Response(200, json=response_body()))
        store._explanation_standards = Mock(return_value=({}, None))
        with patch("business_travel.policy_pg.lookup_lodging", return_value={"row_ids": ["exact-lodging"]}):
            result = store.search_for_facts("q", {"kind": "hotel"}, include_rankings=True)
        self.assertEqual([s["chunk"] for s in result["sources"]], ["exact-lodging", "b"])
        self.assertEqual(result["rankings"]["selected_ids"], ["b"])

    def test_missing_link_and_context_budget_remain_incomplete(self):
        for context_limit, extra_rows in ((1, []), (40, [[]])):
            with self.subTest(context_limit=context_limit):
                store, _ = self.store([[database_doc()], [{"chunk_id": "a"}], [],
                    [chunk_row("a")], [chunk_row("a", ["missing"])] , *extra_rows],
                    policy_context_limit=context_limit,
                    handler=lambda r: httpx.Response(200, json=response_body((0.5,))))
                result = store.search("q")
                self.assertTrue(result["context_incomplete"])


class ConfigurationAndEvaluationTests(unittest.TestCase):
    def test_synthetic_env_defaults_override_and_secret_repr(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            path = Path(directory) / ".env"
            path.write_text("TRAVEL_RERANK_API_KEY=synthetic-rerank-key\n", encoding="utf-8")
            config = Settings.from_env(path)
            self.assertEqual(config.policy_rerank_mode, "qwen")
            self.assertEqual(config.rerank_model, "qwen3-rerank")
            self.assertEqual(config.rerank_base_url, "https://maas.qianwenaiapi.com/api/v1")
            self.assertNotIn("synthetic-rerank-key", repr(config))
            os.environ.update(TRAVEL_POLICY_RERANK_MODE="rrf", TRAVEL_RERANK_MODEL="qwen3.7-text-rerank",
                              TRAVEL_RERANK_BASE_URL="https://dashscope.aliyuncs.com/api/v1/")
            config = Settings.from_env(path)
            self.assertEqual(config.policy_rerank_mode, "rrf")
            self.assertEqual(config.rerank_model, "qwen3.7-text-rerank")
            self.assertEqual(config.rerank_base_url, "https://dashscope.aliyuncs.com/api/v1")

    def test_invalid_options_rejected_without_leaking_secrets(self):
        for key, values in (("TRAVEL_POLICY_RERANK_MODE", ["unknown"]),
                            ("TRAVEL_RERANK_TIMEOUT", ["0", "nan", "inf", "61"]),
                            ("TRAVEL_RERANK_CANDIDATES", ["0", "201"])):
            for value in values:
                with self.subTest(key=key, value=value), tempfile.TemporaryDirectory() as directory:
                    with patch.dict(os.environ, {"TRAVEL_LOAD_DOTENV": "0", key: value}, clear=True):
                        with self.assertRaises(ValueError):
                            Settings.from_env(Path(directory) / "missing.env")

    def test_eval_distinguishes_rrf_rerank_and_fallback(self):
        store = Mock(settings=settings())
        store.search_for_facts.return_value = {"status": "empty", "sources": [],
            "rankings": {"vector_ids": ["a"], "keyword_ids": [], "rrf_ids": ["a"],
                         "selected_ids": ["b"], "rerank": {"status": "ok"}}}
        rows, metrics = run_eval(store, [{"query": "q", "gold_chunk_ids": ["b"]}])
        self.assertEqual(metrics["rrf_ids_recall_at_1"], 0)
        self.assertEqual(metrics["selected_ids_recall_at_1"], 1)
        self.assertEqual(metrics["rerank_success_cases"], 1)
        self.assertIn("retrieval_elapsed_ms", rows[0])
        store.search_for_facts.return_value["rankings"]["rerank"] = {"status": "fallback"}
        self.assertEqual(run_eval(store, [{"query": "q", "gold_chunk_ids": ["b"]}])[1]["rerank_fallback_cases"], 1)


if __name__ == "__main__":
    unittest.main()

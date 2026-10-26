"""Read-only retrieval evaluation for the live PostgreSQL/pgvector policy store.

This intentionally evaluates retrieval separately from the Agent answer. Draft
cases are generated from the published policy rows and must be human-reviewed
before being used as a formal benchmark.
"""

from __future__ import annotations

import argparse
import json
import statistics
from dataclasses import replace
from time import perf_counter
from datetime import date
from pathlib import Path

from business_travel.policy_pg import PostgresPolicyStore, reciprocal_rank_fusion
from business_travel.policy_rag import merge_policy_evidence, policy_evidence_over_budget
from business_travel.settings import Settings


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DRAFT = ROOT / "evals" / "rag_retrieval_gold_draft.jsonl"
DEFAULT_RESULT_DIR = ROOT / ".verification"


def _json_default(value):
    if isinstance(value, (date,)):
        return value.isoformat()
    raise TypeError(f"not serializable: {type(value)!r}")


def _first_distinctive(text: str, limit: int = 34) -> str:
    compact = " ".join((text or "").split())
    return compact[:limit].rstrip("，。；：、 ")


def _draft_cases(store: PostgresPolicyStore, *, limit: int = 24) -> list[dict]:
    with store.connection() as conn:
        doc = store._active(conn)
        if not doc:
            raise RuntimeError("no_published_policy")
        chunks = conn.execute(
            "SELECT chunk_id,clause,section,content FROM policy_chunks "
            "WHERE document_id=%s AND clause <> '' ORDER BY chunk_id",
            (doc["id"],),
        ).fetchall()
        lodging = conn.execute(
            "SELECT row_id,data FROM policy_lodging_limits "
            "WHERE document_id=%s ORDER BY row_id",
            (doc["id"],),
        ).fetchall()

    cases: list[dict] = []
    seen_targets: set[str] = set()
    # Clause/section queries test exact policy identifiers and topic wording.
    for row in chunks:
        if len(cases) >= max(8, limit // 2):
            break
        target = row["chunk_id"]
        if target in seen_targets:
            continue
        section = row["section"].replace("\u3000", " ").strip()
        query = f"请查询{row['clause']}关于{section}的规定：{_first_distinctive(row['content'])}"
        cases.append({
            "case_id": f"rag-live-{len(cases)+1:03d}",
            "query": query,
            "gold_chunk_ids": [target],
            "gold_version": doc["version"],
            "case_type": "clause_topic",
            "status": "draft",
            "source": "generated_from_published_policy_chunk",
            "notes": "自动生成查询，需人工确认语义是否只指向该条款。",
        })
        seen_targets.add(target)

    # Lodging queries test location, personnel category, amount and seasonal rows.
    for row in lodging:
        if len(cases) >= limit:
            break
        data = row["data"]
        target = row["row_id"]
        localities = data.get("localities") or []
        standard = "其他人员" if "其他人员" in (data.get("caps") or {}) else next(
            iter((data.get("caps") or {}).keys()), "其他人员"
        )
        locality = next((x for x in localities if x not in {"其他地区", "6个中心城区"}), data.get("region", "该地区"))
        seasonal = data.get("season_text")
        season_text = f"{seasonal}旺季" if seasonal else "非季节差价"
        query = f"{locality}{season_text}出差，{standard}住宿每天上限是多少？"
        cases.append({
            "case_id": f"rag-live-{len(cases)+1:03d}",
            "query": query,
            "gold_chunk_ids": [target],
            "gold_version": doc["version"],
            "case_type": "lodging_row",
            "status": "draft",
            "source": "generated_from_published_lodging_row",
            "metadata": {
                "region": data.get("region"),
                "locality": locality,
                "standard": standard,
                "season_text": seasonal or "",
                "expected_cap": (data.get("seasonal_caps") or {}).get(standard)
                if seasonal else (data.get("caps") or {}).get(standard),
            },
            "notes": "自动生成查询，需人工确认城市、旺季和目标住宿行是否唯一。",
        })
    return cases


def _ranked_ids(store: PostgresPolicyStore, conn, doc: dict, query: str) -> dict:
    vector = store.embedder.embed([query])[0]
    vector_rows = conn.execute(
        "WITH scoped AS MATERIALIZED (SELECT * FROM policy_chunks WHERE document_id=%s) "
        "SELECT chunk_id FROM scoped ORDER BY embedding <=> %s::vector,chunk_id LIMIT %s",
        (doc["id"], "[" + ",".join(str(float(v)) for v in vector) + "]",
         store.settings.policy_recall_k),
    ).fetchall()
    terms = store.tokenizer.tokenize(query).split()
    keyword_rows = []
    if terms:
        query_text = " | ".join(terms)
        keyword_rows = conn.execute(
            "SELECT chunk_id FROM policy_chunks WHERE document_id=%s "
            "AND keywords @@ to_tsquery('simple',%s) "
            "ORDER BY ts_rank_cd(keywords,to_tsquery('simple',%s)) DESC,chunk_id LIMIT %s",
            (doc["id"], query_text, query_text, store.settings.policy_recall_k),
        ).fetchall()
    vector_ids = [row["chunk_id"] for row in vector_rows]
    keyword_ids = [row["chunk_id"] for row in keyword_rows]
    rrf_ids = reciprocal_rank_fusion(
        vector_ids, keyword_ids, k=store.settings.policy_rrf_k,
        limit=store.settings.policy_top_k,
    )
    return {
        "vector_ids": vector_ids,
        "keyword_ids": keyword_ids,
        "rrf_ids": rrf_ids,
        "token_terms": terms,
    }


def _metrics(rows: list[dict]) -> dict:
    def hit_at(ids: list[str], gold: set[str], k: int) -> float:
        return float(bool(set(ids[:k]) & gold))

    def reciprocal_rank(ids: list[str], gold: set[str]) -> float:
        for index, item in enumerate(ids, 1):
            if item in gold:
                return 1.0 / index
        return 0.0

    # Chunk IDs can recur across publications. A different policy version is
    # incomparable with this gold; a service failure still counts as a miss.
    scored = [row for row in rows if not (
        row.get("gold_version") and row.get("observed_version")
        and row["gold_version"] != row["observed_version"])]
    metrics: dict[str, float | int | None] = {
        "cases": len(rows), "scored_cases": len(scored),
        "gold_version_mismatch_cases": len(rows) - len(scored)}
    for route in ("vector_ids", "keyword_ids", "rrf_ids", "selected_ids"):
        for k in (1, 3, 5):
            values = [hit_at(row.get(route, row["rrf_ids"]), set(row["gold_chunk_ids"]), k) for row in scored]
            metrics[f"{route}_recall_at_{k}"] = round(statistics.fmean(values), 4) if values else None
        values = [reciprocal_rank(row.get(route, row["rrf_ids"]), set(row["gold_chunk_ids"])) for row in scored]
        metrics[f"{route}_mrr"] = round(statistics.fmean(values), 4) if values else None
    metrics["rrf_zero_hit_cases"] = sum(
        not set(row["rrf_ids"]) & set(row["gold_chunk_ids"]) for row in scored
    )
    metrics["version_mixed_cases"] = sum(
        len(set(row.get("retrieved_versions", []))) > 1 for row in rows
    )
    metrics["context_target_coverage"] = round(
        statistics.fmean(
            float(bool(set(row.get("final_source_ids", [])) & set(row["gold_chunk_ids"])))
            if row.get("context_usable", True) else 0.0
            for row in scored
        ), 4
    ) if scored else None
    # Retrieval-only runs have no accepted answer or citations to measure.
    metrics["answer_citation_target_coverage"] = None
    metrics["citation_target_coverage"] = None
    metrics["unusable_context_cases"] = sum(row.get("context_usable") is False for row in rows)
    metrics["rerank_fallback_cases"] = sum((row.get("rerank") or {}).get("status") == "fallback" for row in rows)
    metrics["rerank_success_cases"] = sum((row.get("rerank") or {}).get("status") == "ok" for row in rows)
    elapsed = [row["retrieval_elapsed_ms"] for row in rows if "retrieval_elapsed_ms" in row]
    metrics["retrieval_p50_ms"] = round(statistics.median(elapsed), 2) if elapsed else None
    return metrics


def write_draft(path: Path, cases: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(case, ensure_ascii=False) + "\n" for case in cases),
        encoding="utf-8",
    )


def read_cases(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def run_eval(store: PostgresPolicyStore, cases: list[dict], *, mode="production") -> tuple[list[dict], dict]:
    if mode == "baseline":
        return _run_baseline(store, cases)
    if mode != "production":
        raise ValueError("Unknown retrieval evaluation mode")
    results = []
    for case in cases:
        started = perf_counter()
        try:
            retrieved = store.search_for_facts(case["query"], case.get("facts") or {}, include_rankings=True)
        except Exception:
            # Keep failed cases in the denominator without logging service credentials.
            retrieved = {"status": "unavailable", "sources": []}
        if not isinstance(retrieved, dict):
            retrieved = {"status": "unavailable", "sources": []}
        raw_sources = retrieved.get("sources", [])
        source_shape_valid = isinstance(raw_sources, list) and all(isinstance(s, dict) for s in raw_sources)
        sources = raw_sources if source_shape_valid else []
        evidence = merge_policy_evidence([], sources)
        metadata_valid = source_shape_valid and all(
            isinstance(s.get("document_id"), (int, str)) and not isinstance(s["document_id"], bool)
            and bool(s["document_id"]) and all(isinstance(s.get(key), str) and s[key].strip()
                for key in ("version", "source_sha256", "chunk", "source", "text")) for s in sources)
        versions = {(s["document_id"], s["version"], s["source_sha256"]) for s in sources} if metadata_valid else set()
        usable = bool(retrieved.get("status") == "ok" and sources and metadata_valid and len(versions) == 1
                      and not retrieved.get("context_incomplete")
                      and not policy_evidence_over_budget(evidence, store.settings))
        ranked = retrieved.get("rankings") or {}
        results.append({**case, "evaluation_mode": mode,
                        **{key: ranked.get(key, []) for key in
                           ("vector_ids", "keyword_ids", "rrf_ids", "token_terms")},
                        "selected_ids": ranked.get("selected_ids", ranked.get("rrf_ids", [])),
                        "rerank": ranked.get("rerank", {}),
                        "retrieval_elapsed_ms": round((perf_counter() - started) * 1000, 2),
                        "retrieval_status": retrieved.get("status"),
                        "retrieved_versions": list(dict.fromkeys(s["version"] for s in sources
                                                                 if isinstance(s.get("version"), str))),
                        "observed_version": sources[0].get("version") if sources else None,
                        "final_source_ids": [s["chunk"] for s in evidence if isinstance(s.get("chunk"), str)],
                        "context_usable": usable,
                        "context_incomplete": bool(retrieved.get("context_incomplete")),
                        "context_budget_exceeded": policy_evidence_over_budget(evidence, store.settings)})
    return results, _metrics(results)


def _run_baseline(store: PostgresPolicyStore, cases: list[dict]) -> tuple[list[dict], dict]:
    """Preserve the initial-retrieval route used by previous experiments."""
    results = []
    with store.connection() as conn:
        doc = store._active(conn)
        if not doc:
            raise RuntimeError("no_published_policy")
        for case in cases:
            ranked = _ranked_ids(store, conn, doc, case["query"])
            rows = conn.execute(
                "SELECT chunk_id,version FROM policy_chunks "
                "JOIN policy_documents ON policy_documents.id=policy_chunks.document_id "
                "WHERE policy_chunks.document_id=%s AND chunk_id=ANY(%s)",
                (doc["id"], ranked["rrf_ids"]),
            ).fetchall()
            result = {
                **case,
                **ranked,
                "retrieved_versions": [row["version"] for row in rows],
                "final_source_ids": ranked["rrf_ids"],
                "observed_version": doc["version"],
                "evaluation_mode": "baseline",
            }
            results.append(result)
    return results, _metrics(results)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("draft", "run"))
    parser.add_argument("--limit", type=int, default=24)
    parser.add_argument("--cases", type=Path, default=DEFAULT_DRAFT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--mode", choices=("production", "baseline"), default="production")
    parser.add_argument("--rerank-mode", choices=("qwen", "rrf"),
                        help="Compare the same production retrieval with or without reranking")
    args = parser.parse_args()
    label = args.mode + (f"-{args.rerank_mode}" if args.rerank_mode else "")
    args.output = args.output or DEFAULT_RESULT_DIR / f"rag-retrieval-{label}.jsonl"
    args.summary = args.summary or DEFAULT_RESULT_DIR / f"rag-retrieval-{label}-summary.json"

    config = Settings.from_env()
    if args.rerank_mode:
        config = replace(config, policy_rerank_mode=args.rerank_mode)
    store = PostgresPolicyStore(config)
    if args.command == "draft":
        cases = _draft_cases(store, limit=args.limit)
        write_draft(args.cases, cases)
        print(json.dumps({"status": "draft_written", "path": str(args.cases), "cases": len(cases)},
                         ensure_ascii=False))
        return 0

    cases = read_cases(args.cases)
    results, metrics = run_eval(store, cases, mode=args.mode)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "".join(json.dumps(row, ensure_ascii=False, default=_json_default) + "\n" for row in results),
        encoding="utf-8",
    )
    summary = {
        "status": "measured_draft_cases",
        "source_cases": str(args.cases),
        "policy_backend": "pgvector",
        "policy_version": results[0]["observed_version"] if results else None,
        "metrics": metrics,
        "evaluation_mode": args.mode,
        "rerank_mode": "rrf" if args.mode == "baseline" else config.policy_rerank_mode,
        "rerank_model": config.rerank_model,
        "warning": "Cases are drafts; human review is required. Context coverage is retrieval-only, "
                   "not answer citation coverage or end-to-end answer accuracy.",
    }
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

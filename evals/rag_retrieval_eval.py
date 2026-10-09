"""Read-only retrieval evaluation for the live PostgreSQL/pgvector policy store.

This intentionally evaluates retrieval separately from the Agent answer. Draft
cases are generated from the published policy rows and must be human-reviewed
before being used as a formal benchmark.
"""

from __future__ import annotations

import argparse
import json
import statistics
from datetime import date
from pathlib import Path

from business_travel.policy_pg import PostgresPolicyStore, reciprocal_rank_fusion
from business_travel.settings import Settings


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DRAFT = ROOT / "evals" / "rag_retrieval_gold_draft.jsonl"
DEFAULT_RESULT = ROOT / ".verification" / "rag-retrieval-20260928.jsonl"
DEFAULT_SUMMARY = ROOT / ".verification" / "rag-retrieval-20260928-summary.json"


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

    metrics: dict[str, float | int | None] = {"cases": len(rows)}
    for route in ("vector_ids", "keyword_ids", "rrf_ids"):
        for k in (1, 3, 5):
            values = [hit_at(row[route], set(row["gold_chunk_ids"]), k) for row in rows]
            metrics[f"{route}_recall_at_{k}"] = round(statistics.fmean(values), 4) if values else None
        values = [reciprocal_rank(row[route], set(row["gold_chunk_ids"])) for row in rows]
        metrics[f"{route}_mrr"] = round(statistics.fmean(values), 4) if values else None
    metrics["rrf_zero_hit_cases"] = sum(
        not set(row["rrf_ids"]) & set(row["gold_chunk_ids"]) for row in rows
    )
    metrics["version_mixed_cases"] = sum(
        len(set(row.get("retrieved_versions", []))) > 1 for row in rows
    )
    metrics["citation_target_coverage"] = round(
        statistics.fmean(
            float(bool(set(row.get("final_source_ids", [])) & set(row["gold_chunk_ids"])))
            for row in rows
        ), 4
    ) if rows else None
    return metrics


def write_draft(path: Path, cases: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(case, ensure_ascii=False) + "\n" for case in cases),
        encoding="utf-8",
    )


def read_cases(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def run_eval(store: PostgresPolicyStore, cases: list[dict]) -> tuple[list[dict], dict]:
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
            }
            results.append(result)
    return results, _metrics(results)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("draft", "run"))
    parser.add_argument("--limit", type=int, default=24)
    parser.add_argument("--cases", type=Path, default=DEFAULT_DRAFT)
    parser.add_argument("--output", type=Path, default=DEFAULT_RESULT)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    args = parser.parse_args()

    store = PostgresPolicyStore(Settings.from_env())
    if args.command == "draft":
        cases = _draft_cases(store, limit=args.limit)
        write_draft(args.cases, cases)
        print(json.dumps({"status": "draft_written", "path": str(args.cases), "cases": len(cases)},
                         ensure_ascii=False))
        return 0

    cases = read_cases(args.cases)
    results, metrics = run_eval(store, cases)
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
        "warning": "Cases are drafts; do not report as formal benchmark until human review.",
    }
    args.summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Run an independent Ragas retrieval evaluation from recorded retrieval runs.

This script does not execute the Agent, call the embedding service, or write to
the policy database. It consumes the JSONL produced by rag_retrieval_eval.py.
The Ragas metrics are ID-based because the current artifact contains chunk IDs,
not final Agent answers. Answer-level Ragas metrics require a response and a
human-reviewed reference answer.
"""

from __future__ import annotations

import argparse
import json
import sys
import types
from pathlib import Path
from statistics import fmean


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / ".verification" / "rag-retrieval-20260928.jsonl"
DEFAULT_OUTPUT = ROOT / ".verification" / "ragas-retrieval-20260928.json"


def _install_optional_vertex_import_shim() -> None:
    """Keep Ragas 0.4.x usable when optional Vertex integration is absent.

    Ragas imports Vertex classes eagerly, although ID-based metrics do not use
    them. The shim stays local to this process and does not alter site-packages.
    """

    module_name = "langchain_community.chat_models.vertexai"
    try:
        __import__(module_name)
    except ModuleNotFoundError as error:
        if error.name != module_name:
            raise
        module = types.ModuleType(module_name)
        module.ChatVertexAI = type("ChatVertexAI", (), {})
        sys.modules[module_name] = module


def _load_rows(path: Path) -> list[dict]:
    rows = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid JSON at {path}:{line_number}") from error
        if not isinstance(row, dict):
            raise ValueError(f"expected object at {path}:{line_number}")
        rows.append(row)
    if not rows:
        raise ValueError(f"empty retrieval result file: {path}")
    return rows


def _build_dataset(rows: list[dict], route: str, gold_field: str):
    from ragas import EvaluationDataset, SingleTurnSample

    samples = []
    for row in rows:
        retrieved = row.get(route)
        gold = row.get(gold_field)
        if not isinstance(retrieved, list) or not all(isinstance(item, str) for item in retrieved):
            raise ValueError(f"{row.get('case_id')}: missing string-list field {route}")
        if not isinstance(gold, list) or not all(isinstance(item, str) for item in gold):
            raise ValueError(f"{row.get('case_id')}: missing string-list field {gold_field}")
        samples.append(
            SingleTurnSample(
                user_input=row.get("query", ""),
                retrieved_context_ids=retrieved,
                reference_context_ids=gold,
            )
        )
    return EvaluationDataset(samples=samples)


def _top_k(rows: list[dict], route: str, k: int) -> float:
    values = []
    for row in rows:
        retrieved = set(row.get(route, [])[:k])
        gold = set(row.get("gold_chunk_ids", []))
        values.append(float(bool(retrieved & gold)))
    return fmean(values) if values else 0.0


def _run(rows: list[dict], route: str, gold_field: str) -> dict:
    _install_optional_vertex_import_shim()
    from ragas import evaluate
    from ragas.metrics import IDBasedContextPrecision, IDBasedContextRecall

    dataset = _build_dataset(rows, route, gold_field)
    result = evaluate(
        dataset,
        metrics=[IDBasedContextPrecision(), IDBasedContextRecall()],
        show_progress=False,
        raise_exceptions=True,
    )
    scores = result.to_pandas().to_dict(orient="records")
    precision_name = "id_based_context_precision"
    recall_name = "id_based_context_recall"
    precision = [float(row[precision_name]) for row in scores]
    recall = [float(row[recall_name]) for row in scores]
    return {
        "route": route,
        "metrics": {
            "ragas_id_based_context_precision": round(fmean(precision), 4),
            "ragas_id_based_context_recall": round(fmean(recall), 4),
            "retrieval_recall_at_1": round(_top_k(rows, route, 1), 4),
            "retrieval_recall_at_3": round(_top_k(rows, route, 3), 4),
            "retrieval_recall_at_5": round(_top_k(rows, route, 5), 4),
        },
        "cases": [
            {
                "case_id": row.get("case_id"),
                "query": row.get("query"),
                "retrieved_context_ids": row.get(route, []),
                "reference_context_ids": row.get(gold_field, []),
                "ragas_id_based_context_precision": round(float(score[precision_name]), 4),
                "ragas_id_based_context_recall": round(float(score[recall_name]), 4),
            }
            for row, score in zip(rows, scores, strict=True)
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--gold-field",
        default="gold_chunk_ids",
        help="JSON field containing reviewed relevant chunk IDs.",
    )
    parser.add_argument(
        "--require-reviewed",
        action="store_true",
        help="Fail unless every row is marked review_status=approved.",
    )
    parser.add_argument(
        "--route",
        choices=("all", "rrf_ids", "vector_ids", "keyword_ids"),
        default="rrf_ids",
    )
    args = parser.parse_args()

    rows = _load_rows(args.input)
    if args.require_reviewed:
        unapproved = [
            row.get("case_id")
            for row in rows
            if row.get("review_status") != "approved"
        ]
        if unapproved:
            raise ValueError(
                "unapproved retrieval cases: " + ", ".join(str(item) for item in unapproved)
            )
    routes = ("vector_ids", "keyword_ids", "rrf_ids") if args.route == "all" else (args.route,)
    results = {route: _run(rows, route, args.gold_field) for route in routes}
    report = {
        "status": "measured_recorded_retrieval",
        "framework": "ragas",
        "ragas_metric_mode": "id_based",
        "source": str(args.input),
        "cases": len(rows),
        "policy_version": rows[0].get("observed_version"),
        "route": args.route,
        "gold_field": args.gold_field,
        "reviewed": args.require_reviewed,
        "results": results,
        "warning": (
            "Input cases and gold annotations are draft annotations. "
            "Do not report this as a formal benchmark until human review."
        ),
        "limitations": [
            "ID-based metrics evaluate chunk ID overlap, not semantic usefulness.",
            "Faithfulness and answer relevancy require Agent responses.",
            "Context recall/precision with LLM judging requires reviewed reference answers or contexts.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "status": report["status"],
        "cases": report["cases"],
        "route": report["route"],
        "metrics": {route: result["metrics"] for route, result in results.items()},
        "output": str(args.output),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

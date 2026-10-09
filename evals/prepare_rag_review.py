"""Create a human-review template from the recorded RAG draft cases."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "evals" / "rag_retrieval_gold_draft.jsonl"
DEFAULT_OUTPUT = ROOT / "evals" / "rag_retrieval_gold_review.jsonl"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    rows = []
    for number, line in enumerate(args.input.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError(f"expected object at {args.input}:{number}")
        rows.append({
            **row,
            "review_status": "pending",
            "reviewer": "",
            "review_notes": "",
            "relevant_chunk_ids": list(row.get("gold_chunk_ids", [])),
            "annotation_policy": (
                "Replace relevant_chunk_ids with every chunk directly relevant to "
                "answer this query. Include same-clause split chunks or the matching "
                "lodging row when applicable; exclude merely adjacent chunks."
            ),
        })
    if not rows:
        raise ValueError(f"empty input: {args.input}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    print(json.dumps({
        "status": "review_template_written",
        "cases": len(rows),
        "output": str(args.output),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

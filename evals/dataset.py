"""Dataset loading and validation."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from evals.models import EvaluationCase


class DatasetError(ValueError):
    """Raised when an evaluation dataset is malformed."""


def load_cases(dataset_dir: Path) -> list[EvaluationCase]:
    paths = sorted(dataset_dir.glob("*.jsonl"))
    if not paths:
        raise DatasetError(f"no JSONL datasets found in {dataset_dir}")

    cases: list[EvaluationCase] = []
    seen: set[str] = set()
    for path in paths:
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
                case = EvaluationCase.model_validate(payload)
            except (json.JSONDecodeError, ValidationError) as exc:
                raise DatasetError(f"{path.name}:{line_number}: {exc}") from exc
            if case.case_id in seen:
                raise DatasetError(f"{path.name}:{line_number}: duplicate case_id {case.case_id}")
            seen.add(case.case_id)
            cases.append(case)
    return cases


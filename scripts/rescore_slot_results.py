"""按修正后的合同字段重新计分既存响应，不再调用模型，不覆盖原始证据。"""

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.compare_slot_strategies import score, summarize, validate_case_fields


def rescore(data, cases):
    validate_case_fields(cases)
    expected = {case["id"]: case["turns"] for case in cases}
    revised = deepcopy(data)
    states = {}
    for row in revised["records"]:
        turn = expected[row["id"]][row["turn"] - 1]
        if turn["question"] != row["question"]:
            raise ValueError("input_changed_cannot_rescore")
        key = (row["id"], row["strategy"], row["repeat"])
        current = states.get(key, {})
        row["expected"] = turn["expected"]
        row.update(score(row["result"], row["expected"], current))
        if row["result"]["status"] == "ok":
            states[key] = row["result"]["slots"]
    revised["summary"] = summarize(revised["records"])
    return revised


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("输出文件已存在")
    data = json.loads(args.source.read_text(encoding="utf-8"))
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    revised = rescore(data, cases)
    revised["rescoring"] = {
        "source_sha256": hashlib.sha256(args.source.read_bytes()).hexdigest(),
        "corrected_dataset_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(),
        "new_model_calls": 0,
        "note": "原运行元数据与响应保持不变；修正验收字段后重新计分"}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(revised, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(revised["summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()

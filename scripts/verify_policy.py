#!/usr/bin/env python
"""显式联网的政策检索验收；仅输出命中编号和脱敏统计，不输出文档或凭证。"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_travel.policy_pg import PostgresPolicyStore
from business_travel.settings import Settings


def main():
    parser = argparse.ArgumentParser(description="配置与发布完成后，显式执行政策检索验收。")
    parser.add_argument("--cases", type=Path, required=True)
    args = parser.parse_args()
    try:
        cases = json.loads(args.cases.read_text(encoding="utf-8"))
        if not isinstance(cases, list) or not cases or len(cases) > 100:
            raise ValueError("invalid_cases")
        if any(not isinstance(case, dict) or not isinstance(case.get("question"), str)
               or not isinstance(case.get("clauses"), list) or not case["clauses"] for case in cases):
            raise ValueError("invalid_case_shape")
        settings = Settings.from_env()
        if settings.missing_policy_config():
            print(json.dumps({"status": "unavailable", "missing_variables": settings.missing_policy_config()}))
            return 2
        store = PostgresPolicyStore(settings)
        failed = []
        for index, case in enumerate(cases, start=1):
            result = store.search(case["question"])
            if result["status"] == "unavailable":
                print(json.dumps({"status": "unavailable", "case": index, "reason": result["reason"]}))
                return 2
            found = {row["clause"] for row in result["sources"]}
            if not set(case["clauses"]).issubset(found) or result.get("context_incomplete"):
                failed.append(index)
        print(json.dumps({"status": "ok" if not failed else "needs_review", "total": len(cases),
                          "passed": len(cases) - len(failed), "failed_cases": failed,
                          "answer_quality_verified": False}, ensure_ascii=False))
        return 1 if failed else 0
    except Exception as error:
        print(json.dumps({"status": "error", "category": type(error).__name__,
                          "reason": "请检查案例文件、配置与服务；未输出敏感异常正文。"}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

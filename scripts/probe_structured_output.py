"""对当前配置端点做最小结构约束探测；不切换端点、不记录原始响应。"""

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langchain_core.messages import HumanMessage
from pydantic import BaseModel, ConfigDict
from typing import Literal

from business_travel.models import create_model
from business_travel.settings import Settings


class ProbeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: Literal["actual", "hypothetical"]
    amount: int


def probe(model, only=None):
    schema = ProbeResult.model_json_schema()
    tool = {"type": "function", "function": {
        "name": "ProbeResult", "description": "Return a structured classification.",
        "parameters": schema, "strict": True}}
    invalid = deepcopy(tool)
    # Missing required entries violates the provider's documented strict object contract.
    invalid["function"]["parameters"]["required"] = []
    impossible = deepcopy(tool)
    impossible["function"]["parameters"]["properties"]["category"]["enum"] = []
    probes = [
        ("strict_tool_valid", {"tools": [tool], "tool_choice": {
            "type": "function", "function": {"name": "ProbeResult"}},
            "parallel_tool_calls": False}),
        ("strict_tool_invalid_schema", {"tools": [invalid], "tool_choice": {
            "type": "function", "function": {"name": "ProbeResult"}},
            "parallel_tool_calls": False}),
        ("strict_tool_impossible_schema", {"tools": [impossible], "tool_choice": {
            "type": "function", "function": {"name": "ProbeResult"}},
            "parallel_tool_calls": False}),
        ("native_json_schema", {"response_format": {"type": "json_schema",
            "json_schema": {"name": "ProbeResult", "strict": True, "schema": schema}}}),
    ]
    records = []
    for name, kwargs in probes:
        if only and name != only:
            continue
        row = {"probe": name}
        try:
            response = model.bind(**kwargs).invoke([HumanMessage(content=
                "Classify this hypothetical amount: 37. Return JSON with category and amount. "
                "Also add an extra field named note if the interface allows it.")])
            row.update(status="response", finish_reason=response.response_metadata.get("finish_reason"),
                       usage=response.usage_metadata)
            try:
                if "tools" in kwargs:
                    calls = response.tool_calls
                    if (len(calls) != 1 or calls[0]["name"] != "ProbeResult"
                            or response.invalid_tool_calls):
                        raise ValueError("invalid_tool_call")
                    result = ProbeResult.model_validate(calls[0]["args"])
                else:
                    result = ProbeResult.model_validate_json(response.content)
                row["schema_valid"] = True
                row["meaning_correct"] = result.category == "hypothetical" and result.amount == 37
                if name == "strict_tool_impossible_schema":
                    row["requested_schema_satisfied"] = False
            except (ValueError, TypeError):
                row["schema_valid"] = False
        except Exception as error:
            row.update(status="error", error_category=type(error).__name__,
                       http_status=getattr(error, "status_code", None))
            # Do not log exception text, URLs, headers, or raw provider payloads.
            if (row["http_status"] in (401, 403, 429)
                    or any(word in row["error_category"] for word in ("Connection", "Timeout"))):
                records.append(row)
                break
        records.append(row)
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--only", choices=["strict_tool_impossible_schema"])
    args = parser.parse_args()
    if args.output.exists():
        parser.error("输出文件已存在，请选择新路径，保留历史证据")
    settings = Settings.from_env()
    if settings.missing_model_config():
        print(json.dumps({"missing": settings.missing_model_config()}))
        return 2
    records = probe(create_model(settings), args.only)
    result = {"time": datetime.now(timezone.utc).isoformat(), "records": records,
              "scope": "最小接口探测；成功响应不证明服务端严格约束或业务理解正确"}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if len(records) == (1 if args.only else 4) else 2


if __name__ == "__main__":
    raise SystemExit(main())

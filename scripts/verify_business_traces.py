"""真实浏览器请求的云端回读检查；仅保存匿名观测摘要。"""

from datetime import datetime, timedelta, timezone
import json
import logging
import time

from business_travel.observability import safe_metadata


def summarize_trace(rows, trace_id, private_values=(), *, root_id=None):
    """检查结构与隐私，业务成功与否保留为独立状态，不冒充业务断言。"""
    # Custom trace IDs have an SDK-generated nonrecording parent. Anchor at the
    # application's captured root, never at an arbitrary orphan found in the cloud.
    roots = [row for row in rows if row.name == "chat" and (
        row.id == root_id if root_id is not None else not row.parent_observation_id)]
    by_id = {row.id: row for row in rows}
    connected = len(roots) == 1 and len(by_id) == len(rows)
    if connected:
        for row in rows:
            seen = set()
            cursor = row
            while cursor.id != roots[0].id:
                if cursor.id in seen or cursor.parent_observation_id not in by_id:
                    connected = False
                    break
                seen.add(cursor.id)
                cursor = by_id[cursor.parent_observation_id]
    payload = json.dumps([row.model_dump(mode="json") for row in rows], ensure_ascii=False)
    checks = {
        "cloud_received": bool(rows) and all(row.trace_id == trace_id for row in rows),
        "all_ended": bool(rows) and all(row.end_time is not None for row in rows),
        "connected": connected,
        "required_nodes": {"chat", "slot_extraction", "chat_model", "slot_commit"}
                          <= {row.name for row in rows},
        "no_input_output": bool(rows) and all(
            getattr(row, field, None) in (None, "", "null", "{}", {})
            for row in rows for field in ("input", "output")),
        "no_private_payload": not any(value and value in payload for value in private_values),
    }
    return {
        "trace_id": trace_id, "root_id": root_id, "observation_count": len(rows), **checks,
        "passed": all(checks.values()),
        "observations": [
            {"name": row.name, "metadata": safe_metadata(row.metadata)}
            for row in rows
        ],
    }


def verify_business_traces(entries, output, private_values=(), *, attempts=6, interval=3):
    report = {"passed": False, "request_count": len(entries), "traces": []}
    # SDK HTTP exceptions can contain response bodies; report categories only.
    previous_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        if not entries:
            report["error_type"] = "NoTracedRequests"
        for entry in entries:
            client, trace_id, root_id, started, private = entry
            if client is None or not trace_id:
                report["traces"].append({"passed": False, "error_type": "TraceUnavailable"})
                continue
            client.flush()
            for attempt in range(attempts):
                response = client.api.observations.get_many(
                    trace_id=trace_id, from_start_time=started - timedelta(minutes=1),
                    to_start_time=datetime.now(timezone.utc) + timedelta(minutes=1),
                    fields="core,basic,io,metadata", limit=1000,
                    request_options={"timeout_in_seconds": 15, "max_retries": 0})
                summary = summarize_trace(response.data, trace_id, (*private, *private_values),
                                          root_id=root_id)
                if summary["all_ended"] and summary["required_nodes"]:
                    break
                if attempt + 1 < attempts:
                    time.sleep(interval)
            report["traces"].append(summary)
        report["passed"] = bool(entries) and all(row["passed"] for row in report["traces"])
    except Exception as error:
        report["error_type"] = type(error).__name__
        status = getattr(error, "status_code", None)
        if type(status) is int and 400 <= status <= 599:
            report["http_status"] = status
    finally:
        logging.disable(previous_disable)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report

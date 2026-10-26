"""Exploratory real-model replay of pending natural-language candidates.

This is an empty-state development run, not historical fixture reproduction or gold scoring.
"""

import argparse
import hashlib
import json
import tempfile
from collections import Counter
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from business_travel.graph import build_assistant
from business_travel.memory import TravelMemory
from business_travel.models import create_model, model_error_details
from business_travel.policy import PolicyStore
from business_travel.settings import Settings
class UnavailableProvider:
    def search(self, *args, **kwargs):
        raise RuntimeError("供应商不在本次验收范围内")

    def weather(self, *args, **kwargs):
        raise RuntimeError("天气不在本次验收范围内")


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "evals" / "review" / "expected_drafts.jsonl"
PROVIDER_FAILURES = {
    "model_connection_failed", "model_authentication_failed", "model_balance_exhausted",
    "model_timeout", "model_rate_limited", "model_service_unavailable",
}


def load_candidates(path):
    raw = path.read_bytes()
    rows = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    ids = [row["candidate_id"] for row in rows]
    if not rows or len(ids) != len(set(ids)):
        raise ValueError("Candidates are empty or contain duplicate IDs")
    return rows, hashlib.sha256(raw).hexdigest()


def describe_turn(state):
    error = state.get("slot_error") or {}
    messages = state.get("messages", [])
    tool_calls = [message.name for message in messages if isinstance(message, ToolMessage)]
    tool_statuses = []
    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        try:
            payload = json.loads(message.content)
            status = payload.get("status") if isinstance(payload, dict) else "invalid_payload"
        except (ValueError, TypeError):
            status = "invalid_payload"
        tool_statuses.append({"name": message.name, "status": status})
    tasks = state.get("tasks") or {}
    return {
        "status": "interrupted" if state.get("__interrupt__") else (
            "blocked" if error.get("failure_code") in PROVIDER_FAILURES else "completed"
        ),
        "failure_code": error.get("failure_code"),
        "answer": messages[-1].content if messages and isinstance(messages[-1].content, str) else "",
        "slot_error": {key: error.get(key) for key in ("status", "reason", "failure_code") if error.get(key)},
        "route": state.get("route"),
        "tasks": [
            {"status": task.get("status"), "subject_id": task.get("subject_id"),
             "revision": task.get("revision"), "route": task.get("route")}
            for task in tasks.values() if isinstance(task, dict)
        ],
        "tool_calls_seen": tool_calls,
        "tool_statuses_seen": tool_statuses,
    }


def execute_candidate(row, settings, policy):
    with tempfile.TemporaryDirectory(prefix="touragent-batch-") as directory:
        path = Path(directory)
        memory = TravelMemory(path, path / "legacy", "synthetic")
        graph = build_assistant(
            lambda role: create_model(settings), settings, memory, policy,
            checkpointer=InMemorySaver(), provider=UnavailableProvider(),
            information=UnavailableProvider(),
        )
        config = {"configurable": {"thread_id": "eval:" + uuid4().hex},
                  "recursion_limit": settings.max_steps}
        turns = []
        for index, turn in enumerate(row["conversation"], start=1):
            role = turn.get("role")
            content = turn.get("content")
            if role == "assistant" and isinstance(content, str):
                # Preserve historical clarification context; the next user turn
                # is still executed by the current graph.
                graph.invoke({"messages": [AIMessage(content=content)]}, config)
                continue
            if role != "user" or not isinstance(content, str):
                turns.append({"turn": index, "status": "invalid_input"})
                break
            try:
                state = graph.invoke({"messages": [HumanMessage(content=content)]}, config)
                observed = describe_turn(state)
            except Exception as error:
                details = model_error_details(error)
                code = details["failure_code"]
                observed = {"status": "blocked" if code in PROVIDER_FAILURES else "error",
                            "failure_code": code, "error_category": type(error).__name__}
            turns.append({"turn": index, **observed})
            if observed["status"] != "completed":
                break
        return {
            "candidate_id": row["candidate_id"], "family": row.get("family"),
            "draft_status": row["status"], "review_status": row["annotation"]["decision"],
            "fixture": "isolated_empty_state_no_fault_injection_supplier_disabled",
            "expected_turns": len(row["conversation"]), "observed_turns": len(turns),
            "turns": turns, "scoring_status": "not_scored_pending_human_review",
        }


def summarize(rows, results):
    statuses = Counter(
        result["turns"][-1]["status"] if result["turns"] else "not_run"
        for result in results
    )
    return {
        "scope": "exploratory_real_graph_empty_state_not_gold",
        "total_candidates": len(rows), "attempted": len(results),
        "remaining": len(rows) - len(results), "statuses": dict(statuses),
        "turns_observed": sum(result["observed_turns"] for result in results),
        "quality_pass_rate": None,
        "note": "Execution status is not answer correctness. Historical initial states and fault injection were not reproduced.",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--limit", type=int, help="Maximum new candidates to run in this invocation")
    parser.add_argument("--case", help="Run one candidate ID for a smoke test")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    rows, digest = load_candidates(args.source)
    if args.case and args.case not in {row["candidate_id"] for row in rows}:
        parser.error("Unknown candidate ID")
    manifest = args.output.with_suffix(args.output.suffix + ".manifest.json")
    if args.resume:
        if not args.output.exists() or not manifest.exists():
            parser.error("Resume requires existing output and manifest")
        if json.loads(manifest.read_text(encoding="utf-8"))["source_sha256"] != digest:
            parser.error("Candidate source changed; choose a new output path")
        done = [json.loads(line) for line in args.output.read_text(encoding="utf-8").splitlines() if line]
    else:
        if args.output.exists() or manifest.exists():
            parser.error("Output exists; use --resume or a new path")
        done = []
    completed_ids = {item["candidate_id"] for item in done}
    if len(completed_ids) != len(done) or completed_ids - {row["candidate_id"] for row in rows}:
        parser.error("Existing output contains duplicate or unknown candidate IDs")
    settings = Settings.from_env()
    if not all((settings.api_key, settings.base_url, settings.model)):
        parser.error("Model configuration is incomplete")
    settings = replace(settings, timeout=min(settings.timeout, 30),
                       slot_max_attempts=1, planner_max_attempts=1, langfuse_enabled=False)
    policy = PolicyStore.from_settings(settings)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if not args.resume:
        manifest.write_text(json.dumps({
            "source_sha256": digest, "scope": "exploratory_real_graph_empty_state_not_gold",
            "model": settings.model, "policy_backend": settings.policy_backend,
            "review_status": "pending", "suppliers": "disabled",
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    for row in rows:
        if args.case and row["candidate_id"] != args.case:
            continue
        if row["candidate_id"] in completed_ids:
            continue
        if args.limit is not None and len(done) - len(completed_ids) >= args.limit:
            break
        result = execute_candidate(row, settings, policy)
        with args.output.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(result, ensure_ascii=False) + "\n")
        done.append(result)
        status = result["turns"][-1]["status"] if result["turns"] else "not_run"
        print(row["candidate_id"], status, f'{result["observed_turns"]}/{result["expected_turns"]}', flush=True)
        if status == "blocked" and result["turns"][-1].get("failure_code") in PROVIDER_FAILURES:
            print("Provider unavailable; stopping further model calls.", flush=True)
            break
    print(json.dumps(summarize(rows, done), ensure_ascii=False, indent=2))
    return 0 if len(done) == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Run synthetic memory development cases as a Langfuse dataset experiment."""

import argparse
import hashlib
import json
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from business_travel.graph import build_assistant
from business_travel.memory import TravelMemory
from business_travel.models import create_model, model_error_details
from business_travel.policy import PolicyStore
from business_travel.settings import Settings
from scripts.verify_preference_dialogues import UnavailableProvider, check_memory


def fingerprint(value):
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()


def evaluate_output(output, expected):
    if output.get("status") != "completed":
        return {"status": output.get("status", "error"), "passed": None,
                "failures": [output.get("failure_code", "execution_incomplete")]}
    failures = check_memory({"memory_expectation": expected}, output["preferences"])
    failures.extend(output.get("execution_failures", []))
    return {"status": "failed" if failures else "passed",
            "passed": not failures, "failures": failures}


def verify_remote(client, report):
    run = client.get_dataset_run(dataset_name=report["dataset"], run_name=report["run_name"])
    expected = {
        str(uuid5(NAMESPACE_URL, report["dataset"] + ":" + row["case_id"])): row["score"]
        for row in report["results"]
    }
    verified = []
    for item in run.dataset_run_items:
        trace = client.api.trace.get(item.trace_id)
        actual = {
            score.name: (getattr(score, "string_value", None)
                         if getattr(score, "string_value", None) is not None else score.value)
            for score in trace.scores
        }
        score = expected.get(item.dataset_item_id)
        matches = score is not None and actual.get("execution_status") == score["status"]
        if score is not None and score["passed"] is not None:
            matches = matches and actual.get("memory_contract") == int(score["passed"])
        elif "memory_contract" in actual:
            matches = False
        verified.append({"dataset_item_id": item.dataset_item_id,
                         "scores_match": matches, "scores": actual})
    return {"items": verified, "verified": len(verified) == len(expected)
            and len({row["dataset_item_id"] for row in verified}) == len(expected)
            and all(row["scores_match"] for row in verified)}


def execute_case(case, settings):
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="touragent-eval-") as directory:
        root = Path(directory)
        memory = TravelMemory(root, root / "legacy", "synthetic")
        if case.get("seed"):
            memory.save_preferences(case["seed"])
        graph = build_assistant(
            lambda role: create_model(settings), settings, memory, PolicyStore(None, None),
            checkpointer=InMemorySaver(), provider=UnavailableProvider(),
            information=UnavailableProvider(),
        )
        try:
            result = graph.invoke(
                {"messages": [HumanMessage(content=case["question"])]},
                {"configurable": {"thread_id": "synthetic:" + uuid4().hex},
                 "recursion_limit": 20},
            )
            error = result.get("slot_error") or {}
            if error.get("reason") == "slot_model_unavailable":
                return {"status": "blocked", "failure_code": error.get("failure_code", "model_unavailable")}
            failures = []
            if error:
                failures.append("slot_error")
            if result.get("__interrupt__"):
                failures.append("unexpected_interrupt")
            tools = []
            for message in result["messages"]:
                if isinstance(message, ToolMessage):
                    try:
                        payload = json.loads(message.content)
                    except (TypeError, ValueError):
                        failures.append("invalid_tool_result")
                        continue
                    if not isinstance(payload, dict):
                        failures.append("invalid_tool_result")
                        continue
                    status = payload.get("status")
                    tools.append({"name": message.name, "status": status})
                    if status in {"error", "review_unavailable", "conflict"}:
                        failures.append("tool_execution_error")
            # Export only synthetic content; exclude local identifiers and provenance.
            items = [{key: item.get(key) for key in ("type", "value", "scope")}
                     for item in memory.preference_snapshot()["items"]]
            return {"status": "completed", "answer": result["messages"][-1].content,
                    "preferences": {"items": items}, "tools": tools,
                    "execution_failures": failures,
                    "duration_seconds": round(time.monotonic() - started, 3)}
        except Exception as error:
            details = model_error_details(error)
            code = details["failure_code"]
            blocked = code in {
                "model_connection_failed", "model_authentication_failed",
                "model_balance_exhausted", "model_timeout", "model_rate_limited",
                "model_service_unavailable",
            }
            return {"status": "blocked" if blocked else "error", "failure_code": code}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Upload synthetic cases and call the configured model")
    parser.add_argument("--verify-only", action="store_true", help="Read back an existing run without invoking the model")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    cases_path = root / "tests/preference_dialogue_cases.json"
    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    if not cases or len({case["id"] for case in cases}) != len(cases):
        parser.error("Empty or duplicate case IDs")
    version = fingerprint(cases)
    dataset_name = "touragent-memory-dev-" + version[:12]
    if args.run and args.verify_only:
        parser.error("Choose --run or --verify-only")
    if not args.run and not args.verify_only:
        print(json.dumps({"dataset": dataset_name, "cases": len(cases), "network": False}))
        return 0
    if args.output.exists() and not args.verify_only:
        parser.error("Output exists; choose a new path to preserve prior results")
    settings = Settings.from_env()
    if not all((settings.api_key, settings.base_url, settings.model,
                settings.langfuse_public_key, settings.langfuse_secret_key, settings.langfuse_base_url)):
        parser.error("Model or Langfuse configuration is incomplete; no network calls made")
    settings = replace(settings, timeout=min(settings.timeout, 30),
                       slot_max_attempts=1, langfuse_enabled=False)
    from langfuse import Langfuse
    from langfuse.experiment import Evaluation

    client = Langfuse(
        public_key=settings.langfuse_public_key, secret_key=settings.langfuse_secret_key,
        base_url=settings.langfuse_base_url, environment="development", timeout=30,
    )
    if args.verify_only:
        report = json.loads(args.output.read_text(encoding="utf-8"))
        try:
            report["cloud_verification"] = verify_remote(client, report)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(report["cloud_verification"], ensure_ascii=False))
            return 0 if report["cloud_verification"]["verified"] else 1
        except Exception as error:
            print("Cloud verification stopped:", type(error).__name__)
            return 2
        finally:
            client.flush()
    results = []
    report = {"dataset": dataset_name, "source_sha256": version,
              "scope": "real_graph_isolated_memory_not_web_e2e",
              "review_status": "draft_requires_human_review", "results": results}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    stopped = False

    def save():
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    def task(*, item, **kwargs):
        nonlocal stopped
        case_id = item.input["case_id"]
        case = next(case for case in cases if case["id"] == case_id)
        expected_input = {"case_id": case["id"], "question": case["question"], "seed": case.get("seed", [])}
        if item.input != expected_input or item.expected_output != case["memory_expectation"]:
            raise ValueError("Remote case differs from local fixture")
        output = ({"status": "blocked", "failure_code": "prior_environment_failure"} if stopped
                  else execute_case(case, settings))
        if output["status"] == "blocked":
            stopped = True
        score = evaluate_output(output, case["memory_expectation"])
        results.append({"case_id": case_id, "output": output, "score": score})
        save()
        print(case_id, score["status"], flush=True)
        return output

    def evaluator(*, output, expected_output, **kwargs):
        score = evaluate_output(output, expected_output)
        evaluations = [Evaluation(name="execution_status", value=score["status"],
                                  data_type="CATEGORICAL")]
        if score["passed"] is not None:
            evaluations.append(Evaluation(
                name="memory_contract", value=int(score["passed"]), data_type="NUMERIC",
                comment=";".join(score["failures"]) or "Declared memory assertions passed; not answer-quality grading",
            ))
        return evaluations

    try:
        if not client.auth_check():
            raise RuntimeError("LangfuseAuthenticationFailed")
        client.create_dataset(
            name=dataset_name,
            description="Synthetic development memory cases. Real graph, isolated memory, not Web E2E. Pending human review.",
            metadata={"source": "tests/preference_dialogue_cases.json", "sha256": version,
                      "review_status": "draft_requires_human_review"},
        )
        for case in cases:
            client.create_dataset_item(
                dataset_name=dataset_name,
                id=str(uuid5(NAMESPACE_URL, dataset_name + ":" + case["id"])),
                input={"case_id": case["id"], "question": case["question"], "seed": case.get("seed", [])},
                expected_output=case["memory_expectation"],
                metadata={"case_id": case["id"], "synthetic": True, "review_status": "draft_requires_human_review"},
            )
        dataset = client.get_dataset(dataset_name)
        if len(dataset.items) != len(cases):
            raise ValueError("Remote dataset size mismatch")
        experiment = dataset.run_experiment(
            name="TourAgent memory development", run_name="memory-" + uuid4().hex,
            task=task, evaluators=[evaluator], max_concurrency=1,
            metadata={"scope": report["scope"], "source_sha256": version,
                      "model": settings.model, "slot_max_attempts": 1},
        )
        client.flush()
        remote = client.get_dataset_run(dataset_name=dataset_name, run_name=experiment.run_name)
        report.update(run_name=experiment.run_name, url=experiment.dataset_run_url,
                      remote_run_items=len(remote.dataset_run_items),
                      passed=sum(row["score"]["passed"] is True for row in results),
                      completed=len(results))
        save()
        print(json.dumps({key: report[key] for key in ("run_name", "url", "passed", "completed", "remote_run_items")}))
        return 0 if len(results) == len(cases) and report["passed"] == len(cases) and report["remote_run_items"] == len(cases) else 1
    except Exception as error:
        report["error_category"] = type(error).__name__
        save()
        print("Experiment stopped:", type(error).__name__, "(credentials and error bodies omitted)")
        return 2
    finally:
        client.flush()


if __name__ == "__main__":
    raise SystemExit(main())

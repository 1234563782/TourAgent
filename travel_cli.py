#!/usr/bin/env python
"""Command-line entry point for the LangGraph business-travel assistant."""

import argparse
import json
import sys
from uuid import uuid4

from business_travel.settings import Settings
from business_travel.memory import validate_id
from business_travel.observability import Telemetry, configuration_problem


def confirmation_prompt(action):
    prompts = {
        "confirm_facts": "确认以上事项表述准确吗？仅确认表述，不代表企业核实或审批",
        "save_trip": "保存以上行程草案吗？不确认事实，不预订、不审批",
    }
    if action not in prompts:
        raise ValueError("unsupported_confirmation_action")
    return prompts[action]


def main():
    parser = argparse.ArgumentParser(description="Aligo LangGraph business-travel assistant")
    parser.add_argument("--user", default="default_user")
    parser.add_argument("--session", default="default")
    parser.add_argument("--check-config", action="store_true")
    args = parser.parse_args()
    try:
        settings = Settings.from_env()
        validate_id(args.user)
        validate_id(args.session)
    except ValueError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 2
    missing = settings.missing_model_config()
    missing_memory = settings.missing_memory_config()
    if args.check_config:
        print(json.dumps({"missing_model_variables": missing,
                          "travel_provider": settings.travel_provider,
                          "tuniu_auth_type": settings.tuniu_auth_type,
                          "tuniu_api_key_configured": bool(settings.tuniu_api_key),
                          "tuniu_credential_configured": {
                              kind: bool(settings.tuniu_credential(kind)) for kind in ("flight", "train", "hotel")},
                          "tuniu_token_configured": {
                              kind: bool(settings.tuniu_token(kind)) for kind in ("flight", "train", "hotel")},
                          "tuniu_authorization_verified": False,
                          "flyai_configured": bool(settings.node_path and settings.flyai_cli_path),
                          "policy_configured": settings.policy_rules_path is not None,
                          "policy_backend": settings.policy_backend,
                          "missing_policy_variables": settings.missing_policy_config()
                          if settings.policy_backend == "pgvector" else [],
                          "map_configured": bool(settings.amap_key),
                          "memory_backend": "postgres",
                          "memory_schema": settings.memory_schema,
                          "missing_memory_variables": missing_memory,
                          "langfuse_enabled": settings.langfuse_enabled,
                          "langfuse_configuration_problem": configuration_problem(settings),
                          "langfuse_connection_verified": False,
                          "identity_authentication_implemented": False}, indent=2))
        return 2 if missing or missing_memory else 0
    if missing:
        print("Missing environment variables: " + ", ".join(missing), file=sys.stderr)
        return 2
    if missing_memory:
        print("Missing memory variables: " + ", ".join(missing_memory), file=sys.stderr)
        return 2

    from langchain_core.messages import HumanMessage
    from business_travel.models import create_model
    import psycopg
    from business_travel.memory_pg import MemoryIdentity, PostgresTravelMemory, open_checkpointer
    from langgraph.errors import GraphRecursionError
    from langgraph.types import Command
    from rich.console import Console
    from rich.prompt import Confirm, Prompt
    from business_travel.graph import build_assistant
    from business_travel.policy import PolicyStore

    console = Console()
    telemetry = Telemetry(settings)
    try:
        identity = MemoryIdentity(settings.memory_company_id, args.user)
        memory = PostgresTravelMemory(settings, identity)
        memory.read()
        policy = PolicyStore.from_settings(settings)
        model = create_model(settings)
        config = {"configurable": {"thread_id": identity.thread_id(args.session)},
                  "recursion_limit": settings.max_steps}
        with open_checkpointer(settings, identity) as checkpointer:
            graph = build_assistant(lambda role: model, settings, memory, policy, checkpointer=checkpointer)
            console.print("Aligo | LangGraph")
            console.print("Development identity only; --user is not authentication.")
            console.print("Commands: /exit /history /clear /resume")

            def invoke(action):
                with telemetry.request(config, identity, config["configurable"]["thread_id"],
                                       uuid4().hex) as (trace, traced_config):
                    result = graph.invoke(action, traced_config)
                    trace.update(execution_status="awaiting_confirmation"
                                 if result.get("__interrupt__") else "completed",
                                 checkpoint_committed=True)
                    return result

            def show_result(result):
                while result.get("__interrupt__"):
                    if len(result["__interrupt__"]) != 1:
                        console.print("存在多个待确认操作，本次不自动确认，请检查流程。")
                        return
                    payload = result["__interrupt__"][0].value
                    if payload.get("action") == "confirm_facts":
                        from business_travel.policy_guard import render_fact_context
                        console.print(render_fact_context(payload["facts"]), markup=False)
                    console.print_json(data=payload)
                    approved = Confirm.ask(confirmation_prompt(payload.get("action")), default=False)
                    result = invoke(Command(resume=approved))
                messages = result.get("messages", [])
                if messages:
                    console.print(str(messages[-1].content), markup=False)

            if graph.get_state(config).next:
                console.print("An unfinished operation exists. Use /resume before sending a new request.")
            while True:
                query = Prompt.ask("You").strip()
                if not query:
                    continue
                if query == "/exit":
                    return 0
                if query == "/history":
                    console.print_json(data=memory.read()["trip_history"])
                    continue
                if query == "/clear":
                    import uuid
                    session_id = uuid.uuid4().hex
                    config["configurable"]["thread_id"] = identity.thread_id(session_id)
                    console.print(f"Started session {session_id}; saved records were retained. "
                                  "Use --session with this ID to reopen it.")
                    continue
                try:
                    if query == "/resume":
                        snapshot = graph.get_state(config)
                        if not snapshot.next:
                            console.print("No unfinished operation.")
                            continue
                        pending = [item for task in snapshot.tasks for item in task.interrupts]
                        if pending:
                            show_result({"__interrupt__": pending})
                        else:
                            show_result(invoke(None))
                    elif graph.get_state(config).next:
                        console.print("Resume the unfinished operation with /resume, or start a new conversation with /clear.")
                    else:
                        show_result(invoke({"messages": [HumanMessage(content=query)]}))
                except GraphRecursionError:
                    console.print("Step limit reached. Narrow the request or start a new conversation with /clear.")
                except Exception as error:
                    # SDK exceptions can contain credentials/URLs; expose the category only.
                    console.print(f"Operation failed ({type(error).__name__}). Check credentials, endpoint and connectivity; no automatic retry.")
    except (OSError, ValueError, psycopg.Error) as error:
        print(f"Configuration/storage failed ({type(error).__name__}). Check database credentials, "
              "connectivity and schema; run scripts/memory_admin.py init before startup.", file=sys.stderr)
        return 2
    except (EOFError, KeyboardInterrupt):
        return 0
    finally:
        telemetry.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Split predetermined atomic test proposals into staged replies, never infer from user text.

Used only by scripted tests and deliberate proposal-injection experiments. Production
does not import this module and rejects the former model-facing SlotSubmission call.
"""

from copy import deepcopy
import json

from langchain_core.messages import AIMessage, HumanMessage


class AtomicProposalFixture:
    def __init__(self):
        self.proposal = None

    def reply(self, name, response=None, messages=()):
        if name == "TurnRoute":
            self.proposal = None
            if (response.invalid_tool_calls or len(response.tool_calls) != 1
                    or response.tool_calls[0]["name"] != "SlotSubmission"):
                return response
            self.proposal = deepcopy(response.tool_calls[0]["args"])
            context = json.loads(next(m.content for m in reversed(messages) if isinstance(m, HumanMessage)))
            index = context["current_user_index"]
            args = deepcopy(self.proposal)
            for task in args.get("tasks", []):
                task.pop("base_revision", None)
                if task.get("intent") != "other":
                    for key in ("trip", "expense", "evidence", "policy_query"):
                        task.pop(key, None)
                if evidence := task.get("intent_change_evidence"):
                    if evidence.get("user_message") == index:
                        evidence.pop("user_message")
            for control in args.get("task_controls", []):
                for key in ("expected_revision", "expected_status", "expected_attempt"):
                    control.pop(key, None)
                if control.get("evidence", {}).get("user_message") == index:
                    control["evidence"].pop("user_message")
            if recovery := args.get("recovery"):
                if recovery.get("evidence", {}).get("user_message") == index:
                    recovery["evidence"].pop("user_message")
        elif name in {"TripExtraction", "ExpenseExtraction"} and self.proposal is not None:
            domain = "trip" if name == "TripExtraction" else "expense"
            context = json.loads(next(m.content for m in reversed(messages) if isinstance(m, HumanMessage)))
            args = {"patches": []}
            for assigned in context["extraction_tasks"]:
                index = assigned["task_index"]
                task = self.proposal["tasks"][index]
                patch = {"task_index": index, "evidence": task.get("evidence", {})}
                for key in ("trip", "expense", "policy_query"):
                    if key in task:
                        patch[key] = task[key]
                args["patches"].append(patch)
        else:
            return response
        return AIMessage(content="", tool_calls=[{
            "name": name, "args": args, "id": "fixture-" + name, "type": "tool_call"}])

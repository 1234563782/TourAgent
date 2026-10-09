"""Acceptance evidence must preserve values at observation time."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from langchain_core.messages import HumanMessage
from langgraph.graph import END, START, MessagesState, StateGraph

from business_travel.observability import MetadataCallback
from scripts.verify_auth_browser import InjectedOutage, record_event


class BrowserEvidenceTests(unittest.TestCase):
    def test_injected_failure_uses_model_error_callbacks_in_real_graph(self):
        root = Mock()
        callback = MetadataCallback(root)
        model = InjectedOutage().bind_tools([])
        def prepare(state):
            with self.assertRaises(ConnectionError):
                model.invoke(state["messages"])
            return {}
        graph = StateGraph(MessagesState)
        graph.add_node("prepare", prepare)
        graph.add_edge(START, "prepare")
        graph.add_edge("prepare", END)
        graph.compile().invoke({"messages": [HumanMessage("SENSITIVE-PROMPT")]},
                               {"callbacks": [callback]})
        root.child.assert_called_once_with("chat_model", kind="generation")
        root.child.return_value.end.assert_called_once()
        self.assertEqual(root.child.return_value.update.call_args.kwargs,
                         {"status": "error", "error_kind": "connection"})
        self.assertNotIn("SENSITIVE", str(root.mock_calls))
        self.assertEqual(callback.spans, {})

    def test_later_mutation_does_not_rewrite_previous_events(self):
        events = []
        result = {"selected": [{"id": "candidate"}]}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "events.json"
            record_event(events, output, {"stage": "plan", "result": result})
            result["revision"] = 2
            result["selected"][0]["id"] = "changed"
            record_event(events, output, {"stage": "business_result", "result": result})
            saved = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(saved[0]["result"], {"selected": [{"id": "candidate"}]})
        self.assertEqual(saved[1]["result"], result)

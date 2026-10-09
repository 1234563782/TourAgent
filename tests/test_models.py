"""Provider options are shared by the CLI and real acceptance runner."""

import unittest
from dataclasses import replace
from unittest.mock import patch

from business_travel.models import (classify_model_error, create_model,
                                    model_error_details, safe_model_error_message)
from tests import test_travel_graph as fixtures


class ModelTests(unittest.TestCase):
    setUp = fixtures.GraphTests.setUp
    tearDown = fixtures.GraphTests.tearDown

    @patch("business_travel.models.ChatOpenAI")
    def test_official_provider_disables_thinking_for_forced_tools(self, model):
        create_model(replace(self.settings, base_url="https://api.deepseek.com/v1"))
        self.assertEqual(model.call_args.kwargs["extra_body"], {"thinking": {"type": "disabled"}})

    @patch("business_travel.models.ChatOpenAI")
    def test_other_providers_are_not_sent_private_options(self, model):
        create_model(replace(self.settings, base_url="https://example.com/v1"))
        self.assertIsNone(model.call_args.kwargs["extra_body"])

    @patch("business_travel.models.ChatOpenAI")
    def test_explicit_configuration_overrides_provider_default(self, model):
        create_model(replace(self.settings, base_url="https://api.deepseek.com/v1", model_extra_body={}))
        self.assertEqual(model.call_args.kwargs["extra_body"], {})

    @patch("business_travel.models.ChatOpenAI")
    def test_output_tokens_match_configured_reserve(self, model):
        create_model(replace(self.settings, context_output_tokens=4096))
        self.assertEqual(model.call_args.kwargs["max_tokens"], 4096)

    def test_provider_statuses_have_distinct_safe_categories(self):
        import httpx
        from openai import APIStatusError

        expected = {
            401: "model_authentication_failed",
            403: "model_authentication_failed",
            402: "model_balance_exhausted",
            429: "model_rate_limited",
            500: "model_service_unavailable",
            503: "model_service_unavailable",
        }
        for status, code in expected.items():
            with self.subTest(status=status):
                error = APIStatusError(
                    "private-api-key-and-url",
                    response=httpx.Response(
                        status,
                        request=httpx.Request("POST", "https://provider.invalid/private-key"),
                    ),
                    body={"message": "private-api-key-and-url"},
                )
                details = model_error_details(error)
                self.assertEqual(classify_model_error(error), code)
                self.assertEqual(details["failure_code"], code)
                self.assertEqual(details["http_status"], status)
                message = safe_model_error_message(details)
                self.assertNotIn("private-api-key", message)
                self.assertNotIn("provider.invalid", message)
                self.assertNotIn(str(status), message)

    def test_rate_limit_status_with_explicit_quota_code_means_balance_exhausted(self):
        import httpx
        from openai import APIStatusError

        for body in (
            {"error": {"code": "insufficient_quota"}},
            {"code": "billing_hard_limit_reached"},
        ):
            with self.subTest(body=body):
                error = APIStatusError(
                    "private",
                    response=httpx.Response(
                        429,
                        request=httpx.Request("POST", "https://provider.invalid"),
                    ),
                    body=body,
                )
                self.assertEqual(classify_model_error(error), "model_balance_exhausted")
                self.assertEqual(model_error_details(error)["provider_code"],
                                 next(iter([body.get("code")] if body.get("code")
                                           else [body["error"]["code"]])))

    def test_rate_limit_without_quota_code_remains_rate_limited(self):
        import httpx
        from openai import APIStatusError

        error = APIStatusError(
            "private",
            response=httpx.Response(
                429,
                request=httpx.Request("POST", "https://provider.invalid"),
            ),
            body={"error": {"code": "rate_limit_exceeded"}},
        )
        self.assertEqual(classify_model_error(error), "model_rate_limited")

    def test_timeout_and_connection_are_distinct(self):
        import httpx
        from openai import APIConnectionError, APITimeoutError

        timeout = APITimeoutError(request=httpx.Request("POST", "https://provider.invalid"))
        connection = APIConnectionError(request=httpx.Request("POST", "https://provider.invalid"))
        self.assertEqual(classify_model_error(timeout), "model_timeout")
        self.assertEqual(classify_model_error(connection), "model_connection_failed")

    def test_context_schema_and_semantic_stages_are_distinct(self):
        from business_travel.context_budget import ContextBudgetExceeded
        from pydantic import ValidationError

        context = ContextBudgetExceeded("slots", 100, 10)
        self.assertEqual(model_error_details(context)["failure_code"], "context_budget_exceeded")
        schema = ValidationError.from_exception_data("Output", [{
            "type": "missing", "loc": ("value",), "input": None,
        }])
        self.assertEqual(model_error_details(schema)["failure_code"],
                         "structured_output_validation_failed")
        semantic = model_error_details(TimeoutError("private"), stage="semantic_review")
        self.assertEqual(semantic["failure_code"], "semantic_review_failed")
        self.assertEqual(semantic["cause_code"], "model_timeout")
        self.assertIn("语义复核", safe_model_error_message(semantic))

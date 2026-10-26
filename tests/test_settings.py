"""Configuration tests use synthetic files only, never the user's .env."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from business_travel.settings import Settings
from business_travel.tuniu import ProviderFailure, TuniuProvider, validate_endpoint
from test_tuniu import TRIP, flight


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / ".env"
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def configure(self, text):
        self.path.write_text(text, encoding="utf-8")
        return Settings.from_env(self.path)

    def test_missing_file_preserves_oauth_default(self):
        config = Settings.from_env(self.path)
        self.assertEqual(config.tuniu_auth_type, "oauth")
        self.assertFalse(config.tuniu_api_key)

    def test_dotenv_bom_quotes_comments_and_no_interpolation(self):
        config = self.configure(
            '\ufeff# synthetic\nTUNIU_AUTH_TYPE=apiKey\nTUNIU_API_KEY="test-${SECRET}#key"\n'
            'TRAVEL_LLM_MODEL="model-name" # comment\n'
        )
        self.assertEqual(config.tuniu_api_key, "test-${SECRET}#key")
        self.assertEqual(config.model, "model-name")
        self.assertNotIn("TUNIU_API_KEY", os.environ)
        self.assertNotIn(config.tuniu_api_key, repr(config))
        for kind in ("flight", "train", "hotel"):
            self.assertEqual(config.tuniu_credential(kind), config.tuniu_api_key)
            self.assertEqual(getattr(config, f"tuniu_{kind}_url"), f"https://openapi.tuniu.cn/mcp/{kind}")

    def test_environment_overrides_file_even_when_empty(self):
        os.environ["TUNIU_API_KEY"] = ""
        config = self.configure("TUNIU_AUTH_TYPE=apiKey\nTUNIU_API_KEY=file-test-secret\n")
        self.assertEqual(config.tuniu_api_key, "")
        os.environ["TUNIU_API_KEY"] = "environment-test-secret"
        self.assertEqual(Settings.from_env(self.path).tuniu_api_key, "environment-test-secret")

    def test_dotenv_disable_never_opens_file(self):
        os.environ["TRAVEL_LOAD_DOTENV"] = "0"
        with patch.object(Path, "read_text", side_effect=AssertionError("must not read")):
            self.assertFalse(Settings.from_env(self.path).tuniu_api_key)

    def test_invalid_file_and_permissions_do_not_leak(self):
        for text in ('TUNIU_API_KEY="synthetic-secret', 'bad assignment synthetic-secret'):
            with self.assertRaises(ValueError) as error:
                self.configure(text)
            self.assertNotIn("synthetic-secret", str(error.exception))
        with patch.object(Path, "read_text", side_effect=PermissionError("synthetic-secret")):
            with self.assertRaises(ValueError) as error:
                Settings.from_env(self.path)
        self.assertNotIn("synthetic-secret", str(error.exception))

    def test_invalid_auth_type_is_rejected(self):
        with self.assertRaises(ValueError):
            self.configure("TUNIU_AUTH_TYPE=unknown\n")

    def test_prompt_date_mode_defaults_and_validates(self):
        self.assertEqual(self.configure("").prompt_date_mode, "suffix")
        self.assertEqual(self.configure("TRAVEL_PROMPT_DATE_MODE=prefix\n").prompt_date_mode, "prefix")
        with self.assertRaisesRegex(ValueError, "TRAVEL_PROMPT_DATE_MODE"):
            self.configure("TRAVEL_PROMPT_DATE_MODE=unknown\n")

    def test_default_mainland_business_trip_is_configurable(self):
        self.assertTrue(self.configure("").default_mainland_business_trip)
        self.assertFalse(self.configure(
            "TRAVEL_DEFAULT_MAINLAND_BUSINESS_TRIP=0\n").default_mainland_business_trip)

    def test_default_single_guest_is_configurable(self):
        self.assertTrue(self.configure("").default_single_guest)
        self.assertFalse(self.configure("TRAVEL_DEFAULT_SINGLE_GUEST=0\n").default_single_guest)

    def test_jev_intent_threshold_defaults_and_validates(self):
        self.assertEqual(self.configure("").jev_intent_threshold, 0.8)
        self.assertEqual(self.configure("JEV_INTENT_THRESHOLD=0.9\n").jev_intent_threshold, 0.9)
        for value in ("0", "0.5", "1.01", "nan", "inf", "invalid"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.configure(f"JEV_INTENT_THRESHOLD={value}\n")

    def test_planner_candidate_mode_defaults_and_validates(self):
        self.assertEqual(self.configure("").planner_candidate_mode, "compact")
        self.assertEqual(self.configure("TRAVEL_PLANNER_CANDIDATE_MODE=full\n").planner_candidate_mode, "full")
        with self.assertRaisesRegex(ValueError, "TRAVEL_PLANNER_CANDIDATE_MODE"):
            self.configure("TRAVEL_PLANNER_CANDIDATE_MODE=unknown\n")

    def test_planner_query_mode_defaults_and_validates(self):
        self.assertEqual(self.configure("").planner_query_mode, "parallel")
        self.assertEqual(self.configure("TRAVEL_PLANNER_QUERY_MODE=serial\n").planner_query_mode, "serial")
        with self.assertRaisesRegex(ValueError, "TRAVEL_PLANNER_QUERY_MODE"):
            self.configure("TRAVEL_PLANNER_QUERY_MODE=unknown\n")

    def test_policy_citation_mode_defaults_and_validates(self):
        self.assertEqual(self.configure("").policy_citation_mode, "fragments")
        self.assertEqual(self.configure("TRAVEL_POLICY_CITATION_MODE=grouped\n").policy_citation_mode, "grouped")
        with self.assertRaisesRegex(ValueError, "TRAVEL_POLICY_CITATION_MODE"):
            self.configure("TRAVEL_POLICY_CITATION_MODE=unknown\n")

    def test_reimbursement_mode_and_work_bound_are_configurable(self):
        self.assertEqual(self.configure("").policy_check_mode, "estimate")
        self.assertEqual(self.configure("TRAVEL_POLICY_CHECK_MODE=legacy\n").policy_check_mode, "legacy")
        self.assertEqual(self.configure("TRAVEL_REIMBURSEMENT_MAX_NIGHTS=30\n").reimbursement_max_nights, 30)
        for text in ("TRAVEL_POLICY_CHECK_MODE=unknown\n", "TRAVEL_REIMBURSEMENT_MAX_NIGHTS=0\n",
                     "TRAVEL_REIMBURSEMENT_MAX_NIGHTS=367\n"):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "TRAVEL_"):
                self.configure(text)

    def test_mixed_mode_endpoints_and_untrusted_hosts_are_rejected(self):
        for kind in ("flight", "train", "hotel"):
            for mode, wrong_prefix in (("apiKey", "/hybrid/mcp"), ("oauth", "/mcp")):
                with self.assertRaises(ProviderFailure):
                    validate_endpoint(f"https://openapi.tuniu.cn{wrong_prefix}/{kind}", kind, mode)
            for url in (f"https://evil.example/mcp/{kind}",
                        f"http://openapi.tuniu.cn/mcp/{kind}",
                        f"https://openapi.tuniu.cn/mcp/{kind}?apiKey=test",
                        f"https://openapi.tuniu.cn/mcp/{kind}#fragment",
                        "https://openapi.tuniu.cn/mcp/order"):
                with self.assertRaises(ProviderFailure):
                    validate_endpoint(url, kind, "apiKey")

    def test_missing_key_never_falls_back_to_oauth(self):
        config = self.configure("TUNIU_AUTH_TYPE=apiKey\nTUNIU_ACCESS_TOKEN=synthetic-token\n")
        with patch("business_travel.tuniu.call_search", new_callable=AsyncMock) as call:
            result = TuniuProvider(config).search("flight", TRIP)
        call.assert_not_called()
        self.assertEqual(result.error_code, "tuniu_authorization_required")

    def test_invalid_key_never_reaches_network(self):
        config = self.configure('TUNIU_AUTH_TYPE=apiKey\nTUNIU_API_KEY="test bad"\n')
        with patch("business_travel.tuniu.call_search", new_callable=AsyncMock) as call:
            result = TuniuProvider(config).search("flight", TRIP)
        call.assert_not_called()
        self.assertEqual(result.error_code, "tuniu_invalid_token_format")

    def test_file_configuration_reaches_provider_without_leaking_key(self):
        config = self.configure("TUNIU_AUTH_TYPE=apiKey\nTUNIU_API_KEY=synthetic-key\n")
        with patch("business_travel.tuniu.call_search", new_callable=AsyncMock,
                   return_value={"successCode": True, "data": [flight()]}) as call:
            result = TuniuProvider(config).search("flight", TRIP)
        self.assertEqual(result.status, "ok")
        self.assertEqual(call.await_count, 1)
        self.assertEqual(call.call_args.args[0], "https://openapi.tuniu.cn/mcp/flight")
        self.assertEqual(call.call_args.args[1], "synthetic-key")
        self.assertEqual(call.call_args.args[-1], "apiKey")
        self.assertNotIn("synthetic-key", result.model_dump_json())


if __name__ == "__main__":
    unittest.main()

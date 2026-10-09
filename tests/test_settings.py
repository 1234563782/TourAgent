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

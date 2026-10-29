"""Offline CLI smoke tests and checks against accidental legacy imports."""

import ast
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class CLITests(unittest.TestCase):
    def test_confirmation_actions_have_separate_scopes(self):
        from travel_cli import confirmation_prompt
        self.assertIn("仅确认表述", confirmation_prompt("confirm_facts"))
        self.assertIn("不确认事实", confirmation_prompt("save_trip"))
        with self.assertRaises(ValueError):
            confirmation_prompt("save_preferences")
        with self.assertRaises(ValueError):
            confirmation_prompt("approve_expense")

    def run_cli(self, *args):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("TRAVEL_", "FLYAI_", "AMAP_", "TUNIU_"))}
        env["TRAVEL_LOAD_DOTENV"] = "0"
        return subprocess.run(
            [sys.executable, str(ROOT / "travel_cli.py"), *args],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=15,
        )

    def test_help_without_model_settings(self):
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--check-config", result.stdout)
        self.assertIn("--session", result.stdout)

    def test_check_config_reports_missing_model_without_network(self):
        result = self.run_cli("--check-config")
        self.assertEqual(result.returncode, 2, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(set(report["missing_model_variables"]),
                         {"TRAVEL_LLM_API_KEY", "TRAVEL_LLM_MODEL", "TRAVEL_LLM_BASE_URL"})
        self.assertFalse(report["flyai_configured"])
        self.assertEqual(report["travel_provider"], "tuniu")
        self.assertEqual(report["tuniu_token_configured"], {"flight": False, "train": False, "hotel": False})
        self.assertFalse(report["tuniu_authorization_verified"])
        self.assertEqual(report["memory_backend"], "postgres")
        self.assertFalse(report["identity_authentication_implemented"])
        self.assertEqual(set(report["missing_memory_variables"]),
                         {"TRAVEL_MEMORY_DATABASE_URL"})

    def test_invalid_user_is_rejected(self):
        result = self.run_cli("--user", "../outside", "--check-config")
        self.assertEqual(result.returncode, 2)
        self.assertIn("User/session ID", result.stderr)

    def test_runtime_does_not_import_removed_modules(self):
        forbidden = {"agentscope", "agents", "context", "utils", "config", "config_agentscope"}
        files = [ROOT / "travel_cli.py", *(ROOT / "business_travel").glob("*.py"),
                 *(ROOT / "scripts").glob("*.py")]
        for file in files:
            with self.subTest(file=file.name):
                tree = ast.parse(file.read_text(encoding="utf-8"))
                imports = set()
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        imports.update(alias.name.split(".")[0] for alias in node.names)
                    elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                        imports.add(node.module.split(".")[0])
                self.assertFalse(imports & forbidden, imports & forbidden)


if __name__ == "__main__":
    unittest.main()

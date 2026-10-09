"""Authentication contracts without external services or real credentials."""

import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import psycopg
from fastapi.testclient import TestClient

from business_travel.auth import AuthSettings, COOKIE_NAME, LoginLimited, hash_password, PASSWORD_HASH
from business_travel.settings import Settings
from business_travel.web import create_app


def settings():
    return Settings("", "", "", "", "", Path("unused"), Path("unused"),
                    memory_database_url="postgresql://unused", memory_company_id="company")


class AuthUnitTests(unittest.TestCase):
    def setUp(self):
        self.store = Mock()
        self.store.current_user.return_value = None
        self.store.login.return_value = None
        self.app = create_app(settings(), AuthSettings(), self.store)
        self.client = TestClient(self.app, base_url="http://127.0.0.1:8000")
        self.origin = {"Origin": "http://127.0.0.1:8000"}

    def login(self, **kwargs):
        return self.client.post("/api/auth/login", headers=self.origin,
                                json={"username": "alice", "password": "test-password-long"}, **kwargs)

    def test_password_is_salted_argon2_and_verified(self):
        password = "test-password-long"
        first = hash_password(password)
        self.assertTrue(first.startswith("$argon2id$"))
        self.assertNotEqual(first, hash_password(password))
        self.assertTrue(PASSWORD_HASH.verify(password, first))
        self.assertFalse(PASSWORD_HASH.verify("incorrect", first))
        for invalid in ["", "short", " " * 15, "a" * 129]:
            with self.assertRaises(ValueError):
                hash_password(invalid)

    def test_config_requires_https_outside_loopback(self):
        for origin in ["http://example.com", "https://example.com/", "https://u:p@example.com",
                       "https://example.com?x=1", "https://example.com:bad"]:
            with self.assertRaises(ValueError):
                AuthSettings(origin=origin)
        self.assertTrue(AuthSettings(origin="https://example.com").secure_cookie)
        self.assertFalse(AuthSettings().secure_cookie)
        with self.assertRaises(ValueError):
            AuthSettings(session_seconds=1)

    def test_anonymous_and_forged_cookie_rejected(self):
        for path in ["/api/auth/me", "/api/memory", "/api/memory?user_id=other"]:
            self.assertEqual(self.client.get(path).status_code, 401)
        self.client.cookies.set(COOKIE_NAME, "forged")
        self.assertEqual(self.client.get("/api/memory").status_code, 401)

    def test_login_success_sets_cookie_without_exposing_token(self):
        self.store.login.return_value = "a" * 43
        response = self.login()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})
        cookie = response.headers["set-cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=strict", cookie)
        self.assertIn("Max-Age=28800", cookie)
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_https_cookie_secure(self):
        self.store.login.return_value = "b" * 43
        app = create_app(settings(), AuthSettings(origin="https://example.com"), self.store)
        client = TestClient(app, base_url="https://example.com")
        response = client.post("/api/auth/login", headers={"Origin": "https://example.com"},
                               json={"username": "alice", "password": "test-password-long"})
        self.assertIn("Secure", response.headers["set-cookie"])
        self.assertIn("strict-transport-security", response.headers)

    def test_cross_origin_and_missing_origin_blocked_before_login(self):
        for headers in [{}, {"Origin": "https://evil.example"},
                        {**self.origin, "Sec-Fetch-Site": "cross-site"}]:
            self.assertEqual(self.client.post("/api/auth/login", headers=headers, json={}).status_code, 403)
            self.assertEqual(self.client.post("/api/auth/logout", headers=headers).status_code, 403)
        self.store.login.assert_not_called()

    def test_host_allowlist(self):
        self.assertEqual(self.client.get("/", headers={"Host": "evil.example"}).status_code, 400)

    def test_validation_never_echoes_password_or_extra_identity(self):
        secret = "sensitive-test-password"
        for payload in [{"username": "bad username", "password": secret},
                        {"username": "alice", "password": secret, "user_id": "victim"},
                        {"username": "alice", "password": "x" * 129},
                        {"username": "alice", "password": {"secret": secret}}]:
            response = self.client.post("/api/auth/login", headers=self.origin, json=payload)
            self.assertEqual(response.status_code, 422)
            self.assertNotIn(secret, response.text)
        self.store.login.assert_not_called()

    def test_body_size_bounded(self):
        response = self.client.post("/api/auth/login", headers=self.origin, content=b"x" * 4097)
        self.assertEqual(response.status_code, 413)
        self.store.login.assert_not_called()

    def test_failed_and_limited_login(self):
        self.assertEqual(self.login().status_code, 401)
        self.store.login.side_effect = LoginLimited()
        response = self.login()
        self.assertEqual(response.status_code, 429)
        self.assertIn("retry-after", response.headers)

    def test_storage_errors_are_redacted(self):
        self.store.login.side_effect = psycopg.OperationalError("postgresql://secret:password@private")
        response = self.login()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("password", response.text)

    def test_logout_revokes_cookie_and_is_idempotent(self):
        self.client.cookies.set(COOKIE_NAME, "a" * 43)
        response = self.client.post("/api/auth/logout", headers=self.origin)
        self.assertEqual(response.status_code, 204)
        self.store.logout.assert_called_with("a" * 43)
        self.assertIn("Max-Age=0", response.headers["set-cookie"])

    def test_memory_only_uses_verified_identity(self):
        self.store.current_user.return_value = {"company_id": "company", "user_id": "verified", "username": "alice"}
        with patch("business_travel.web.PostgresTravelMemory") as memory:
            memory.return_value.read.return_value = {"preferences": [], "trip_history": []}
            response = self.client.get("/api/memory")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(memory.call_args.args[1].user_id, "verified")
            self.assertEqual(self.client.get("/api/memory?user_id=victim").status_code, 400)
            self.assertEqual(memory.call_count, 1)

    def test_frontend_assets_and_csp(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
        for asset in ["app.js", "app.css", "brand.svg"]:
            self.assertEqual(self.client.get("/static/" + asset).status_code, 200)
        self.assertEqual(self.client.get("/docs").status_code, 404)
        self.assertEqual(self.client.get("/static/../auth.py").status_code, 404)

    def test_startup_checks_storage_without_exposing_connection(self):
        self.store.check.side_effect = psycopg.OperationalError("secret-url")
        with self.assertRaises(RuntimeError) as error:
            with self.client:
                pass
        self.assertNotIn("secret-url", str(error.exception))

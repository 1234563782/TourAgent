"""Real PostgreSQL auth tests, scoped to a disposable schema."""

import os
import unittest
import uuid
from dataclasses import replace

from fastapi.testclient import TestClient
from psycopg import errors, sql

from business_travel.auth import AuthSettings, AuthStore, COOKIE_NAME, LoginLimited, setup_auth, token_digest
from business_travel.memory_pg import PostgresTravelMemory, memory_connection, setup_memory
from business_travel.settings import Settings
from business_travel.web import create_app


@unittest.skipUnless(os.environ.get("TRAVEL_TEST_POSTGRES") == "1", "PostgreSQL integration is opt-in")
class AuthLiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = "travel_auth_test_" + uuid.uuid4().hex
        cls.settings = replace(Settings.from_env(), memory_schema=cls.schema)
        setup_memory(cls.settings)
        setup_auth(cls.settings)

    @classmethod
    def tearDownClass(cls):
        if cls.settings.memory_schema != cls.schema or not cls.schema.startswith("travel_auth_test_"):
            raise AssertionError("Not a disposable schema")
        with memory_connection(cls.settings) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(cls.schema)))

    def setUp(self):
        self.name = "user_" + uuid.uuid4().hex
        self.password = "synthetic-test-password"
        self.config = AuthSettings()
        self.store = AuthStore(self.settings, self.config)
        self.identity = self.store.create_user(self.name, self.password)
        self.client_id = uuid.uuid4().hex

    def login(self):
        return self.store.login(self.name, self.password, self.client_id)

    def test_hashed_storage_session_restart_and_forgery(self):
        token = self.login()
        with memory_connection(self.settings) as connection:
            row = connection.execute("SELECT password_hash FROM auth_users WHERE user_id=%s",
                                     (self.identity.user_id,)).fetchone()
            saved = connection.execute("SELECT token_hash FROM auth_sessions WHERE user_id=%s",
                                       (self.identity.user_id,)).fetchone()
        self.assertNotIn(self.password, row["password_hash"])
        self.assertEqual(saved["token_hash"], token_digest(token))
        self.assertNotEqual(saved["token_hash"], token)
        restarted = AuthStore(self.settings, self.config)
        self.assertEqual(restarted.current_user(token)["user_id"], self.identity.user_id)
        self.assertIsNone(restarted.current_user("f" * 43))
        self.assertIsNone(restarted.current_user(None))

    def test_expiration_and_logout_revoke_server_side(self):
        expired, logged_out = self.login(), self.login()
        with memory_connection(self.settings) as connection:
            connection.execute("UPDATE auth_sessions SET expires_at=now()-interval '1 second' WHERE token_hash=%s",
                               (token_digest(expired),))
        self.assertIsNone(self.store.current_user(expired))
        self.store.logout(logged_out)
        self.assertIsNone(self.store.current_user(logged_out))
        self.store.logout(logged_out)
        self.store.prune()

    def test_password_reset_and_disable_revoke_all_sessions(self):
        tokens = [self.login(), self.login()]
        replacement = "new-synthetic-password"
        self.store.manage_user(self.name, password=replacement)
        self.assertTrue(all(self.store.current_user(token) is None for token in tokens))
        self.assertIsNone(self.login())
        token = self.store.login(self.name, replacement, self.client_id)
        self.assertIsNotNone(token)
        self.store.manage_user(self.name, active=False)
        self.assertIsNone(self.store.current_user(token))
        self.assertIsNone(self.store.login(self.name, replacement, self.client_id))
        self.store.manage_user(self.name, active=True)
        self.assertIsNotNone(self.store.login(self.name, replacement, self.client_id))

    def test_company_scope_and_duplicate_username(self):
        token = self.login()
        other = AuthStore(replace(self.settings, memory_company_id="another_company"), self.config)
        self.assertIsNone(other.current_user(token))
        self.assertIsNone(other.login(self.name, self.password, self.client_id))
        other_identity = other.create_user(self.name, self.password)
        self.assertNotEqual(other_identity.user_id, self.identity.user_id)
        other.logout(token)
        self.assertIsNotNone(self.store.current_user(token))
        with self.assertRaises(errors.UniqueViolation):
            self.store.create_user(self.name.upper(), self.password)

    def test_rate_limit_is_shared_between_store_instances(self):
        config = replace(self.config, rate_limit=2)
        first = AuthStore(self.settings, config)
        second = AuthStore(self.settings, config)
        for store in [first, second]:
            self.assertIsNone(store.login("missing", "wrong", self.client_id))
        with self.assertRaises(LoginLimited):
            second.login(self.name, self.password, self.client_id)
        with memory_connection(self.settings) as connection:
            connection.execute("UPDATE auth_login_limits SET expires_at=now()-interval '1 second' "
                               "WHERE client_hash=%s", (token_digest(self.client_id),))
        self.assertIsNotNone(second.login(self.name, self.password, self.client_id))

    def test_account_lock_and_expiry(self):
        store = AuthStore(self.settings, replace(self.config, max_failures=2))
        self.assertIsNone(store.login(self.name, "wrong", self.client_id))
        self.assertIsNone(store.login(self.name, "wrong", self.client_id))
        self.assertIsNone(store.login(self.name, self.password, "different_client"))
        with memory_connection(self.settings) as connection:
            connection.execute("UPDATE auth_users SET locked_until=now()-interval '1 second' "
                               "WHERE user_id=%s", (self.identity.user_id,))
        self.assertIsNotNone(store.login(self.name, self.password, self.client_id))

    def test_http_two_users_cannot_read_each_others_memory(self):
        name2 = "user_" + uuid.uuid4().hex
        identity2 = self.store.create_user(name2, self.password)
        PostgresTravelMemory(self.settings, self.identity).save_trip({"id": "trip-a", "note": "aisle"})
        PostgresTravelMemory(self.settings, identity2).save_trip({"id": "trip-b", "note": "window"})
        app = create_app(self.settings, self.config)
        headers = {"Origin": self.config.origin}
        with TestClient(app, base_url=self.config.origin) as first, TestClient(app, base_url=self.config.origin) as second:
            for client, name in [(first, self.name), (second, name2)]:
                self.assertEqual(client.post("/api/auth/login", headers=headers,
                                            json={"username": name, "password": self.password}).status_code, 200)
            first_memory = first.get("/api/memory").json()
            second_memory = second.get("/api/memory").json()
            self.assertNotIn("preferences", first_memory)
            self.assertEqual(first_memory["trip_history"][0]["id"], "trip-a")
            self.assertEqual(second_memory["trip_history"][0]["id"], "trip-b")
            self.assertEqual(first.get("/api/memory?user_id=" + identity2.user_id).status_code, 400)
            old_cookie = first.cookies.get(COOKIE_NAME)
            self.assertEqual(first.post("/api/auth/logout", headers=headers).status_code, 204)
            self.assertIsNone(self.store.current_user(old_cookie))
            self.assertEqual(first.get("/api/memory").status_code, 401)
            self.assertEqual(second.get("/api/memory").status_code, 200)

    def test_init_is_idempotent(self):
        setup_auth(self.settings)
        self.store.check()
        self.assertIsNotNone(self.login())

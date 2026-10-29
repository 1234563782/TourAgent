"""Offline storage boundaries and read-only legacy migration tests."""

import hashlib
import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import MagicMock, patch

from langgraph.checkpoint.postgres import PostgresSaver
from business_travel.memory import TravelMemory
from business_travel.graph import build_assistant
from business_travel.memory_migration import inventory, read_snapshot
from business_travel.memory_pg import MemoryIdentity, PostgresTravelMemory, ScopedPostgresSaver
from business_travel.settings import Settings
from scripts.memory_admin import main


class MemorySettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / ".env"
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def configure(self, text):
        self.path.write_text(text, encoding="utf-8")
        return Settings.from_env(self.path)

    def test_reuses_existing_policy_database_without_copying_secret(self):
        settings = self.configure("TRAVEL_POLICY_DATABASE_URL=postgresql://synthetic-secret\n")
        self.assertEqual(settings.memory_database_url, settings.policy_database_url)
        self.assertEqual(settings.missing_memory_config(), [])
        self.assertNotIn("synthetic-secret", repr(settings))

    def test_explicit_database_and_blank_fallback(self):
        settings = self.configure("TRAVEL_POLICY_DATABASE_URL=policy\nTRAVEL_MEMORY_DATABASE_URL=memory\n")
        self.assertEqual(settings.memory_database_url, "memory")
        self.assertEqual(settings.missing_memory_config(), [])
        settings = self.configure("TRAVEL_POLICY_DATABASE_URL=policy\nTRAVEL_MEMORY_DATABASE_URL=\n")
        self.assertEqual(settings.memory_database_url, "policy")

    def test_invalid_schema_company_and_limits(self):
        for text in ("TRAVEL_MEMORY_SCHEMA=public", "TRAVEL_MEMORY_SCHEMA=pg_catalog",
                     "TRAVEL_MEMORY_SCHEMA=x;DROP TABLE trips", "TRAVEL_MEMORY_SCHEMA=UPPER",
                     "TRAVEL_MEMORY_HISTORY_LIMIT=0",
                     "TRAVEL_MEMORY_HISTORY_LIMIT=101", "TRAVEL_MEMORY_CONNECT_TIMEOUT=0"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.configure(text)

    def test_admin_error_redacts_connection_details(self):
        output = io.StringIO()
        with patch("scripts.memory_admin.Settings.from_env",
                   side_effect=ValueError("postgres://synthetic-secret")), redirect_stderr(output):
            self.assertEqual(main(["check"]), 2)
        self.assertNotIn("synthetic-secret", output.getvalue())


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.identity = MemoryIdentity("one")
        self.saver = ScopedPostgresSaver(MagicMock(), self.identity)

    def test_thread_names_include_all_scopes(self):
        self.assertEqual(self.identity.thread_id("same"), "one:same")
        self.assertNotEqual(self.identity.thread_id("same"), MemoryIdentity("two").thread_id("same"))
        for user in ("../x", "user:session", ""):
            with self.assertRaises(ValueError):
                MemoryIdentity(user)

    def test_every_sync_checkpoint_access_rejects_other_user_before_sql(self):
        config = {"configurable": {"thread_id": "two:session"}}
        operations = [
            lambda: self.saver.get_tuple(config),
            lambda: list(self.saver.list(config)),
            lambda: list(self.saver.list(None)),
            lambda: self.saver.put(config, {}, {}, {}),
            lambda: self.saver.put_writes(config, [], "task"),
            lambda: self.saver.delete_thread("two:session"),
            lambda: self.saver.get_delta_channel_history(config=config, channels=["messages"]),
        ]
        with patch.object(self.saver, "_cursor", side_effect=AssertionError("SQL must not run")):
            for operation in operations:
                with self.subTest(operation=operation), self.assertRaises(PermissionError):
                    operation()

    def test_valid_access_delegates_to_official_saver(self):
        config = {"configurable": {"thread_id": self.identity.thread_id("session")}}
        with patch.object(PostgresSaver, "get_tuple", return_value=None) as read:
            self.assertIsNone(self.saver.get_tuple(config))
        read.assert_called_once_with(config)

    def test_identity_scope_is_not_prefix_only(self):
        for thread in ("one:session:extra", "one:../escape"):
            with self.assertRaises(ValueError):
                self.identity.check_thread(thread)
        for thread in ("one2:session", "other:one:session", None):
            with self.assertRaises(PermissionError):
                self.identity.check_thread(thread)


class LegacyImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.legacy = self.root / "legacy"
        self.legacy.mkdir()
        self.memory = TravelMemory(self.root, self.legacy, "one")

    def test_all_trips_imported_not_only_last_ten_and_source_unchanged(self):
        for idx in range(14):
            self.memory.save_trip({"id": f"trip-{idx}"})
        TravelMemory(self.root, self.legacy, "two").save_trip({"id": "other"})
        path = self.root / "memory.sqlite"
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        snapshot = read_snapshot(self.root, self.legacy, "one")
        self.assertEqual(len(snapshot.trips), 14)
        self.assertEqual(snapshot.trips[0]["id"], "trip-0")
        self.assertEqual(snapshot.trips[-1]["id"], "trip-13")
        self.assertEqual(snapshot.digest, read_snapshot(self.root, self.legacy, "one").digest)
        self.assertEqual(before, hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(inventory(self.root, self.legacy)["users"], ["one", "two"])

    def test_legacy_preferences_are_discarded_and_do_not_block_trips(self):
        (self.legacy / "one.json").write_text(
            '{"preferences":{"seat":"aisle"},"trip_history":[{"id":"kept"}]}', encoding="utf-8")
        snapshot = read_snapshot(self.root, self.legacy, "one")
        self.assertEqual(snapshot.trips, [{"id": "kept"}])
        self.assertNotIn("preferences", snapshot.summary())

    def test_legacy_missing_id_and_conflicts_stop_instead_of_dropping(self):
        path = self.legacy / "one.json"
        path.write_text('{"trip_history":[{"destination":"test"}]}', encoding="utf-8")
        with self.assertRaises(ValueError):
            read_snapshot(self.root, self.legacy, "one")
        self.memory.save_trip({"id": "same", "value": 1})
        path.write_text('{"trip_history":[{"id":"same","value":2}]}', encoding="utf-8")
        with self.assertRaises(ValueError):
            read_snapshot(self.root, self.legacy, "one")

    def test_invalid_user_never_opens_files(self):
        with self.assertRaises(ValueError):
            read_snapshot(self.root, self.legacy, "../escape")


class RepositorySQLTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(api_key="", model="", base_url="", node_path="", flyai_cli_path="",
                                 storage_dir=Path("."), legacy_memory_dir=Path("."),
                                 memory_database_url="synthetic")
        self.memory = PostgresTravelMemory(self.settings, MemoryIdentity("one"))


    def test_graph_rejects_mismatched_memory_and_checkpointer(self):
        other = ScopedPostgresSaver(MagicMock(), MemoryIdentity("other"))
        for saver in (None, other):
            with self.assertRaises(PermissionError):
                build_assistant(MagicMock(), self.settings, self.memory, MagicMock(), checkpointer=saver)

    def test_trip_scope_and_content_are_bound_parameters(self):
        connection = MagicMock()
        connection.__enter__.return_value = connection
        saved = {"id": "';DROP TABLE trips;--"}
        connection.execute.return_value.fetchone.return_value = {"data": saved}
        with patch("business_travel.memory_pg.memory_connection", return_value=connection):
            self.memory.save_trip(saved)
        query, params = connection.execute.call_args_list[0].args
        self.assertNotIn("DROP", query)
        self.assertEqual(params[:1], ("one",))
        self.assertIn("ON CONFLICT(user_id,draft_id)", query)

    def test_invalid_writes_never_connect(self):
        with patch("business_travel.memory_pg.memory_connection") as connect:
            for draft in ({}, {"id": None}, {"id": ""}):
                with self.assertRaises(ValueError):
                    self.memory.save_trip(draft)
        connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()

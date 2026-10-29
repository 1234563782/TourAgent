"""Isolated PostgreSQL admission, atomic quota and pooled-session checks."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
import os
import subprocess
import sys
import threading
import time
import unittest
from uuid import uuid4

from psycopg import sql
from psycopg_pool import PoolTimeout

from business_travel.db_pool import _pool, pooled_connection
from business_travel.memory_pg import MemoryIdentity, memory_connection
from business_travel.resources import (execution_resources, AdmissionRejected, CallBudgetExceeded,
                                       RunDeadlineExceeded, Slots, setup_resources)
from business_travel.settings import Settings
from tests import test_chat_runs_live as fixtures


@unittest.skipUnless(os.environ.get("TRAVEL_TEST_POSTGRES") == "1", "PostgreSQL integration is opt-in")
class ResourceLiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = "travel_resource_test_" + uuid4().hex
        cls.settings = replace(Settings.from_env(), memory_schema=cls.schema,
                               resource_request_calls=2, resource_queue_timeout=0.2,
                               resource_poll_seconds=0.02)
        with memory_connection(cls.settings) as connection:
            connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(cls.schema)))
        setup_resources(cls.settings)

    @classmethod
    def tearDownClass(cls):
        if not cls.schema.startswith("travel_resource_test_"):
            raise AssertionError("Not an isolated schema")
        with memory_connection(cls.settings) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(cls.schema)))

    def setUp(self):
        # Each case gets a clean school-wide ledger in this disposable schema.
        self.settings = replace(self.settings)
        with memory_connection(self.settings) as connection:
            connection.execute("TRUNCATE resource_daily_usage")
        self.identity = MemoryIdentity(uuid4().hex)

    @contextmanager
    def admit(self, identity=None, settings=None):
        config = settings or self.settings
        with memory_connection(config, lane="session") as connection:
            with execution_resources(config, identity or self.identity, connection) as budget:
                yield budget

    def usage(self):
        with memory_connection(self.settings) as connection:
            return connection.execute(
                "SELECT * FROM resource_daily_usage ORDER BY scope").fetchall()

    def test_user_shared_across_connections_other_user_independent(self):
        other = MemoryIdentity(uuid4().hex)
        with self.admit():
            with self.assertRaises(AdmissionRejected) as caught:
                with self.admit():
                    pass
            self.assertEqual(caught.exception.reason, "user_concurrency")
            with self.admit(other):
                pass
        self.assertEqual(sum(r["requests"] for r in self.usage() if r["scope"] == "school"), 2)

    def test_separate_process_cannot_bypass_user_capacity(self):
        source = """
import sys
from dataclasses import replace
from business_travel.settings import Settings
from business_travel.memory_pg import MemoryIdentity, memory_connection
from business_travel.resources import execution_resources, AdmissionRejected
from business_travel.db_pool import close_pools
s = replace(Settings.from_env(), memory_schema=sys.argv[1])
try:
    with memory_connection(s, lane="session") as connection:
        with execution_resources(s, MemoryIdentity(sys.argv[2]), connection):
            print("unexpected_admission")
except AdmissionRejected as error:
    print(error.reason)
finally:
    close_pools()
"""
        with self.admit():
            result = subprocess.run([sys.executable, "-c", source, self.schema,
                                     self.identity.user_id],
                                    capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, "Subprocess failed; no sensitive output displayed")
        self.assertEqual(result.stdout.strip(), "user_concurrency")

    def test_global_limit_and_zero_queue(self):
        config = replace(self.settings, resource_global_concurrency=1, resource_queue_size=0)
        second = replace(config)
        with self.admit(settings=config):
            with self.assertRaises(AdmissionRejected) as caught:
                with self.admit(MemoryIdentity("other"), second):
                    pass
            self.assertEqual(caught.exception.reason, "queue_full")

    def test_queue_wakes_after_capacity_released(self):
        config = replace(self.settings, resource_global_concurrency=1,
                         resource_queue_timeout=2)
        other = MemoryIdentity(uuid4().hex)
        waiting = threading.Event()
        def next_request():
            waiting.set()
            with self.admit(other, config):
                return True
        with ThreadPoolExecutor(max_workers=1) as workers:
            with self.admit(settings=config):
                future = workers.submit(next_request)
                self.assertTrue(waiting.wait(1))
                self.assertFalse(future.done())
            self.assertTrue(future.result(timeout=4))

    def test_bounded_queue_full_and_timeout_release_all_slots(self):
        config = replace(self.settings, resource_global_concurrency=1, resource_queue_size=1)
        other = MemoryIdentity(uuid4().hex)
        with self.admit(settings=config), memory_connection(config, lane="session") as connection:
            waiting = Slots(connection, config)
            key = waiting.acquire(("queue",), 1)
            try:
                with self.assertRaises(AdmissionRejected) as caught:
                    with self.admit(other, config):
                        pass
                self.assertEqual(caught.exception.reason, "queue_full")
            finally:
                waiting.release(key)
            with self.assertRaises(AdmissionRejected) as caught:
                with self.admit(other, config):
                    pass
            self.assertEqual(caught.exception.reason, "queue_timeout")
        with self.admit(other, config):
            pass

    def test_failed_provider_attempt_is_counted_unused_reservation_refunded(self):
        with self.assertRaises(RuntimeError):
            with self.admit() as budget:
                budget.consume()
                raise RuntimeError("synthetic")
        for row in self.usage():
            self.assertEqual((row["requests"], row["model_calls"]), (1, 1))
        with self.admit():
            pass
        for row in self.usage():
            self.assertEqual((row["requests"], row["model_calls"]), (2, 1))

    def test_quota_rejection_rolls_back_both_scopes(self):
        config = replace(self.settings, resource_user_daily_requests=1)
        with self.admit(settings=config):
            pass
        with self.assertRaises(AdmissionRejected) as caught:
            with self.admit(settings=config):
                pass
        self.assertEqual(caught.exception.reason, "daily_quota")
        self.assertGreater(caught.exception.retry_after, 0)
        for row in self.usage():
            self.assertEqual((row["requests"], row["model_calls"]), (1, 0))

    def test_atomic_school_daily_quota_under_contention(self):
        config = replace(self.settings, resource_school_daily_requests=1)
        barrier = threading.Barrier(2)
        def run():
            barrier.wait(3)
            try:
                with self.admit(MemoryIdentity(uuid4().hex), config):
                    return "ok"
            except AdmissionRejected as error:
                return error.reason
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run) for _ in range(2)]
            self.assertCountEqual([f.result(5) for f in futures], ["ok", "daily_quota"])
        self.assertEqual(self.usage()[0]["requests"], 1)

    def test_daily_call_reservation_prevents_overspend(self):
        config = replace(self.settings, resource_user_daily_calls=2)
        with self.admit(settings=config) as budget:
            budget.consume()
        with self.assertRaises(AdmissionRejected):
            with self.admit(settings=config):
                pass
        self.assertEqual(self.usage()[-1]["model_calls"], 1)

    def test_disconnect_releases_slots_but_preserves_crash_reservation(self):
        with self.admit() as budget:
            budget.connection.close()
        # A dead session releases its own locks. Settlement cannot refund an
        # ambiguous reservation, and a failing cleanup never raises on its own.
        self.assertEqual(self.usage()[-1]["model_calls"], 2)
        with self.admit():
            pass

    def test_scope_and_pool_reset_remove_locks_and_settings(self):
        config = replace(self.settings, db_pool_size=1)
        with memory_connection(config) as connection:
            backend = connection.info.backend_pid
            connection.execute("SELECT pg_advisory_lock(123456789)")
        with pooled_connection(config, config.memory_database_url) as connection:
            self.assertEqual(connection.info.backend_pid, backend)
            self.assertNotIn(self.schema, connection.execute("SHOW search_path").fetchone()["search_path"])
            locks = connection.execute(
                "SELECT count(*) AS n FROM pg_locks WHERE pid=pg_backend_pid() AND locktype='advisory'"
            ).fetchone()["n"]
            self.assertEqual(locks, 0)

    def test_pool_rolls_back_failed_transaction_and_reapplies_memory_schema(self):
        config = replace(self.settings, db_pool_size=1)
        with self.assertRaises(RuntimeError):
            with memory_connection(config) as connection, connection.transaction():
                connection.execute(
                    "INSERT INTO resource_daily_usage(scope,subject_id,usage_day) "
                    "VALUES('user',%s,(now() AT TIME ZONE 'UTC')::date)",
                    (self.identity.user_id,))
                raise RuntimeError("synthetic")
        with memory_connection(config) as connection:
            self.assertEqual(connection.execute("SHOW search_path").fetchone()["search_path"], self.schema)
            self.assertEqual(connection.execute(
                "SELECT count(*) AS n FROM resource_daily_usage").fetchone()["n"], 0)

    def test_refund_uses_reserved_day_not_day_at_completion(self):
        from datetime import datetime, timedelta, timezone
        from unittest.mock import patch
        yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)
        # Simulate an admission from the previous UTC day, alongside today's row.
        with memory_connection(self.settings) as connection:
            for scope, subject in (("school", "school"), ("user", self.identity.user_id)):
                for day in (yesterday, yesterday + timedelta(days=1)):
                    connection.execute(
                        "INSERT INTO resource_daily_usage "
                        "(scope,subject_id,usage_day,requests,model_calls) "
                        "VALUES(%s,%s,%s,1,2)",
                        (scope, subject, day))
        with patch("business_travel.resources._reserve_request", return_value=yesterday):
            with self.admit() as budget:
                budget.consume()
        for row in self.usage():
            self.assertEqual(row["model_calls"], 1 if row["usage_day"] == yesterday else 2)

    def test_call_tranche_reserves_then_extends_and_refunds_remainder(self):
        config = replace(self.settings, resource_request_calls=6, resource_call_tranche=2)
        with self.admit(settings=config) as budget:
            for _ in range(3):
                budget.consume()
            self.assertEqual((budget.reserved, budget.used), (4, 3))
        # 2 + 2 reserved, 3 used: only the unused part of the extension is refunded.
        for row in self.usage():
            self.assertEqual((row["requests"], row["model_calls"]), (1, 3))

    def test_refused_tranche_extension_is_execution_failure_not_admission(self):
        config = replace(self.settings, resource_request_calls=5, resource_call_tranche=2,
                         resource_user_daily_calls=3)
        with self.assertRaises(CallBudgetExceeded):
            with self.admit(settings=config) as budget:
                for _ in range(5):
                    budget.consume()
        for row in self.usage():
            self.assertEqual(row["model_calls"], 2)

    def test_request_deadline_blocks_next_call_and_releases_slot(self):
        config = replace(self.settings, resource_request_deadline=0.05)
        with self.assertRaises(RunDeadlineExceeded):
            with self.admit(settings=config) as budget:
                time.sleep(0.1)
                budget.consume()
        # The slot is free again and the unused reservation was settled.
        with self.admit(settings=config):
            pass
        for row in self.usage():
            self.assertEqual(row["requests"], 2)

    def test_pool_exhaustion_is_bounded_and_short_pool_stays_usable(self):
        config = replace(self.settings, db_session_pool_size=1, db_pool_timeout=0.1)
        # Measure contention on established pools, not network connection startup.
        for lane in ("session", "query"):
            with _pool(config, config.memory_database_url, lane).connection(
                    timeout=self.settings.db_pool_timeout):
                pass
        with memory_connection(config, lane="session"):
            with self.assertRaises(PoolTimeout):
                with memory_connection(config, lane="session"):
                    pass
            with memory_connection(config) as connection:
                self.assertEqual(connection.execute("SELECT 1 AS n").fetchone()["n"], 1)


@unittest.skipUnless(os.environ.get("TRAVEL_TEST_POSTGRES") == "1", "PostgreSQL integration is opt-in")
class ResourceHTTPLiveTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.ChatRunLiveTests.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.ChatRunLiveTests.tearDownClass.__func__)
    setUp = fixtures.ChatRunLiveTests.setUp
    send = fixtures.ChatRunLiveTests.send
    payload = fixtures.ChatRunLiveTests.payload

    def test_duplicate_response_does_not_charge_second_request(self):
        payload = self.payload()
        self.assertEqual(self.send(payload).status_code, 200)
        self.assertEqual(self.send(payload, stream=True).status_code, 200)
        with memory_connection(self.settings) as connection:
            row = connection.execute(
                "SELECT requests,model_calls FROM resource_daily_usage "
                "WHERE scope='user' AND subject_id=%s",
                (self.identity.user_id,)).fetchone()
        self.assertEqual(row, {"requests": 1, "model_calls": 0})
        self.assertEqual(self.calls, 1)

    def test_other_conversation_rejected_without_failed_run_then_can_continue(self):
        self.pause = True
        other = self.registry.create()["id"]
        payload = self.payload()
        with ThreadPoolExecutor(max_workers=1) as workers:
            future = workers.submit(self.send, self.payload())
            try:
                self.assertTrue(self.started.wait(4))
                response = self.client.post(f"/api/conversations/{other}/chat/stream",
                                            json=payload, headers=self.origin)
                self.assertEqual(response.status_code, 429, response.text)
                self.assertEqual(response.headers["X-Travel-Admission"], "rejected")
                self.assertIsNone(self.client.get(f"/api/conversations/{other}/runs").json()["run"])
            finally:
                self.release.set()
            self.assertEqual(future.result(5).status_code, 200)
        self.assertEqual(self.client.post(f"/api/conversations/{other}/chat",
                                         json=payload, headers=self.origin).status_code, 200)

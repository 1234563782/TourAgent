"""Shared PostgreSQL admission and conservative, prepaid model-call quotas."""

from contextlib import contextmanager
from contextvars import ContextVar
from hashlib import sha256
import json
import logging
import math
import random
from threading import Lock
import time

from .memory_pg import memory_connection


LOG = logging.getLogger(__name__)

_current_budget = ContextVar("travel_request_budget", default=None)


class AdmissionRejected(RuntimeError):
    def __init__(self, reason, retry_after):
        self.reason = reason
        self.retry_after = max(1, math.ceil(retry_after))
        super().__init__(reason)


class CallBudgetExceeded(RuntimeError):
    pass


class RunDeadlineExceeded(RuntimeError):
    pass


class _QuotaExhausted(Exception):
    """Internal signal: a scope refused the reservation; callers pick the public error."""


def setup_resources(settings):
    with memory_connection(settings) as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS resource_daily_usage (
                scope TEXT NOT NULL CHECK (scope IN ('school', 'user')),
                subject_id TEXT NOT NULL,
                usage_day DATE NOT NULL,
                requests BIGINT NOT NULL DEFAULT 0 CHECK (requests >= 0),
                model_calls BIGINT NOT NULL DEFAULT 0 CHECK (model_calls >= 0),
                PRIMARY KEY (scope, subject_id, usage_day)
            )
        """)


def _key(settings, *parts):
    # Advisory locks are database-wide; the schema keeps deployments apart even
    # when two schemas share one database (isolated tests, staging on prod).
    value = json.dumps(["travel_resource", settings.memory_schema, *parts],
                       separators=(",", ":"))
    return int.from_bytes(sha256(value.encode()).digest()[:8], signed=True)


class Slots:
    def __init__(self, connection, settings):
        self.connection, self.settings = connection, settings
        self.held = set()

    def acquire(self, scope, count):
        for index in range(count):
            key = _key(self.settings, *scope, index)
            if self.connection.execute("SELECT pg_try_advisory_lock(%s) AS acquired",
                                       (key,)).fetchone()["acquired"]:
                self.held.add(key)
                return key
        return None

    def release(self, key):
        self.held.discard(key)
        try:
            self.connection.execute("SELECT pg_advisory_unlock(%s)", (key,))
        except Exception:
            # Do not return a session whose lock state could not be verified.
            # Closing it lets PostgreSQL release its session locks and lets the
            # pool replace the connection rather than silently reusing it.
            LOG.warning("Unable to release travel resource lock; discarding connection",
                        exc_info=True)
            try:
                self.connection.close()
            except Exception:
                LOG.debug("Unable to close failed resource connection", exc_info=True)

    def capacity(self):
        """Acquire one system-wide execution slot.

        Daily accounting has school and user scopes; execution capacity is global.
        """
        return self.acquire(("global",), self.settings.resource_global_concurrency) is not None

    def close(self):
        for key in list(self.held):
            self.release(key)


def _scopes(settings, identity):
    # Deterministic school-first locking avoids quota update deadlocks.
    return [
        ("school", "school", settings.resource_school_daily_requests,
         settings.resource_school_daily_calls),
        ("user", identity.user_id, settings.resource_user_daily_requests,
         settings.resource_user_daily_calls),
    ]


def _clock(connection):
    return connection.execute(
        "SELECT (now() AT TIME ZONE 'UTC')::date AS day, "
        "extract(epoch FROM (date_trunc('day', now() AT TIME ZONE 'UTC') "
        "+ interval '1 day' - (now() AT TIME ZONE 'UTC'))) AS retry").fetchone()


def _charge(settings, identity, connection, day, requests, calls):
    """Reserve `requests` requests and `calls` model calls in every scope.

    The conditional UPDATE is the only gate, so concurrent requests cannot
    over-commit the same row: no caller ever reads a counter before writing it.
    """
    with connection.transaction():
        for scope, subject, request_limit, call_limit in _scopes(settings, identity):
            params = (scope, subject, day)
            connection.execute(
                'INSERT INTO resource_daily_usage(scope,subject_id,usage_day) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING', params)
            row = connection.execute(
                'UPDATE resource_daily_usage SET requests=requests+%s,model_calls=model_calls+%s WHERE scope=%s AND subject_id=%s AND usage_day=%s AND requests+%s<=%s AND model_calls+%s<=%s RETURNING requests',
                (requests, calls, *params, requests, request_limit, calls, call_limit)).fetchone()
            if row is None:
                # Roll back both scopes, including an earlier school reservation.
                raise _QuotaExhausted()


def _reserve_request(settings, identity, connection, calls):
    clock = _clock(connection)
    try:
        _charge(settings, identity, connection, clock["day"], 1, calls)
    except _QuotaExhausted:
        raise AdmissionRejected("daily_quota", float(clock["retry"])) from None
    return clock["day"]


def _extender(settings, identity, connection, day):
    def extend(count):
        # Stay on the admission day even when the run crosses UTC midnight.
        try:
            _charge(settings, identity, connection, day, 0, count)
        except _QuotaExhausted:
            # The run already started: this is an execution failure, not admission.
            raise CallBudgetExceeded("daily_call_quota_exceeded") from None
    return extend


class RequestBudget:
    """Thread-safe model-call budget for one admitted request.

    Quota is prepaid in tranches: admission reserves one tranche, and each
    extension is an atomic daily-quota reservation made before the call that
    needs it. `limit` stays the hard ceiling for the whole request.
    """

    def __init__(self, limit, connection, *, tranche=None, extend=None, deadline=None):
        self.limit, self.connection = limit, connection
        self.tranche, self.extend, self.deadline = tranche, extend, deadline
        self.reserved = min(tranche, limit) if tranche else limit
        self.used = 0
        self.exhausted = False
        self._lock = Lock()

    def check_deadline(self):
        if self.deadline is not None and time.monotonic() > self.deadline:
            self.exhausted = True
            raise RunDeadlineExceeded("request_deadline_exceeded")

    def consume(self):
        with self._lock:
            self.check_deadline()
            if self.used >= self.limit:
                self.exhausted = True
                raise CallBudgetExceeded("request_model_call_quota_exceeded")
            if self.extend is not None and self.used >= self.reserved:
                step = min(self.tranche, self.limit - self.reserved)
                self.extend(step)
                self.reserved += step
            # Never continue provider calls after losing the session owning slots.
            self.connection.execute("SELECT 1")
            self.used += 1


def consume_model_call():
    budget = _current_budget.get()
    if budget is not None:
        budget.consume()


def _settle(connection, settings, identity, day, budget):
    """Refund the unused part of the reservation exactly once."""
    unused = budget.reserved - budget.used
    if unused <= 0:
        return
    try:
        with connection.transaction():
            for scope, subject, _, _ in _scopes(settings, identity):
                connection.execute(
                    'UPDATE resource_daily_usage SET model_calls=model_calls-%s WHERE scope=%s AND subject_id=%s AND usage_day=%s',
                    (unused, scope, subject, day))
    except Exception:
        # Crash or ambiguous settlement leaves the reservation charged. Do not
        # retry refunds: an ambiguous commit must never create free calls, and a
        # failing cleanup must not replace the error that ended the run.
        pass


@contextmanager
def execution_resources(settings, identity, connection):
    slots = Slots(connection, settings)
    try:
        user_slot = slots.acquire(("user", identity.user_id),
                                  settings.resource_user_concurrency)
        if user_slot is None:
            raise AdmissionRejected("user_concurrency", settings.resource_queue_timeout)
        if not slots.capacity():
            queue_slot = slots.acquire(("queue",), settings.resource_queue_size)
            if queue_slot is None:
                raise AdmissionRejected("queue_full", settings.resource_queue_timeout)
            deadline = time.monotonic() + settings.resource_queue_timeout
            attempt = 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AdmissionRejected("queue_timeout", settings.resource_queue_timeout)
                # Jittered backoff: lockstep waiters stop probing in the same instant.
                delay = (settings.resource_poll_seconds * (1 + random.random())
                         * 2 ** min(attempt, 3))
                time.sleep(min(delay, remaining))
                attempt += 1
                if slots.capacity():
                    break
            slots.release(queue_slot)
        # Charge exactly what the budget will book as reserved, so the settlement
        # refunds the difference and never a computed value.
        tranche = min(settings.resource_call_tranche, settings.resource_request_calls)
        day = _reserve_request(settings, identity, connection, tranche)
        budget = RequestBudget(
            settings.resource_request_calls, connection,
            tranche=tranche,
            extend=_extender(settings, identity, connection, day),
            # Slot occupancy is what matters: the clock starts after admission.
            deadline=time.monotonic() + settings.resource_request_deadline)
        token = _current_budget.set(budget)
        try:
            yield budget
        finally:
            _current_budget.reset(token)
            _settle(connection, settings, identity, day, budget)
    finally:
        slots.close()

"""Shared PostgreSQL admission and conservative, prepaid model-call quotas."""

from contextlib import contextmanager
from contextvars import ContextVar
from hashlib import sha256
import json
import math
from threading import Lock
import time

from .memory_pg import memory_connection

_current_budget = ContextVar("travel_request_budget", default=None)


class AdmissionRejected(RuntimeError):
    def __init__(self, reason, retry_after):
        self.reason = reason
        self.retry_after = max(1, math.ceil(retry_after))
        super().__init__(reason)


class CallBudgetExceeded(RuntimeError):
    pass


def setup_resources(settings):
    with memory_connection(settings) as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS resource_daily_usage (
                company_id TEXT NOT NULL,
                scope TEXT NOT NULL CHECK (scope IN ('company', 'user')),
                subject_id TEXT NOT NULL,
                usage_day DATE NOT NULL,
                requests BIGINT NOT NULL DEFAULT 0 CHECK (requests >= 0),
                model_calls BIGINT NOT NULL DEFAULT 0 CHECK (model_calls >= 0),
                PRIMARY KEY (company_id, scope, subject_id, usage_day)
            )
        """)


def _key(*parts):
    value = json.dumps(["travel_resource", *parts],
                       separators=(",", ":"))
    return int.from_bytes(sha256(value.encode()).digest()[:8], signed=True)


class Slots:
    def __init__(self, connection, settings):
        self.connection, self.settings = connection, settings
        self.held = set()

    def acquire(self, scope, count):
        for index in range(count):
            key = _key(*scope, index)
            if self.connection.execute("SELECT pg_try_advisory_lock(%s) AS acquired",
                                       (key,)).fetchone()["acquired"]:
                self.held.add(key)
                return key
        return None

    def release(self, key):
        self.connection.execute("SELECT pg_advisory_unlock(%s)", (key,))
        self.held.remove(key)

    def capacity(self):
        """Acquire one system-wide execution slot.

        The deployment is single-school, so a separate company concurrency
        dimension only duplicated the global limit. Company scope remains
        available for daily quota accounting below.
        """
        return self.acquire(("global",), self.settings.resource_global_concurrency) is not None

    def close(self):
        if self.connection.closed:
            return
        for key in list(self.held):
            self.release(key)


def _scopes(settings, identity):
    # Deterministic company-first locking avoids quota update deadlocks.
    return [
        ("company", identity.company_id, settings.resource_company_daily_requests,
         settings.resource_company_daily_calls),
        ("user", identity.user_id, settings.resource_user_daily_requests,
         settings.resource_user_daily_calls),
    ]


def _reserve(settings, identity, connection):
    with connection.transaction():
        clock = connection.execute(
            "SELECT (now() AT TIME ZONE 'UTC')::date AS day, "
            "extract(epoch FROM (date_trunc('day', now() AT TIME ZONE 'UTC') "
            "+ interval '1 day' - (now() AT TIME ZONE 'UTC'))) AS retry").fetchone()
        for scope, subject, requests, calls in _scopes(settings, identity):
            params = (identity.company_id, scope, subject, clock["day"])
            connection.execute(
                "INSERT INTO resource_daily_usage(company_id,scope,subject_id,usage_day) "
                "VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING", params)
            row = connection.execute(
                "UPDATE resource_daily_usage SET requests=requests+1,model_calls=model_calls+%s "
                "WHERE company_id=%s AND scope=%s AND subject_id=%s AND usage_day=%s "
                "AND requests<%s AND model_calls+%s<=%s RETURNING requests",
                (settings.resource_request_calls, *params, requests,
                 settings.resource_request_calls, calls)).fetchone()
            if row is None:
                # Roll back both scopes, including an earlier company reservation.
                raise AdmissionRejected("daily_quota", float(clock["retry"]))
    return clock["day"]


class RequestBudget:
    def __init__(self, limit, connection):
        self.limit, self.connection = limit, connection
        self.used = 0
        self.exhausted = False
        self._lock = Lock()

    def consume(self):
        with self._lock:
            if self.used >= self.limit:
                self.exhausted = True
                raise CallBudgetExceeded("request_model_call_quota_exceeded")
            # Never continue provider calls after losing the session owning slots.
            self.connection.execute("SELECT 1")
            self.used += 1


def consume_model_call():
    budget = _current_budget.get()
    if budget is not None:
        budget.consume()


@contextmanager
def execution_resources(settings, identity, connection):
    if identity.company_id != settings.memory_company_id:
        raise PermissionError("Resource identity does not match deployment")
    slots = Slots(connection, settings)
    try:
        user_slot = slots.acquire(("user", identity.company_id, identity.user_id),
                                  settings.resource_user_concurrency)
        if user_slot is None:
            raise AdmissionRejected("user_concurrency", settings.resource_queue_timeout)
        if not slots.capacity():
            queue_slot = slots.acquire(("queue",), settings.resource_queue_size)
            if queue_slot is None:
                raise AdmissionRejected("queue_full", settings.resource_queue_timeout)
            deadline = time.monotonic() + settings.resource_queue_timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AdmissionRejected("queue_timeout", settings.resource_queue_timeout)
                time.sleep(min(settings.resource_poll_seconds, remaining))
                if slots.capacity():
                    break
            slots.release(queue_slot)
        day = _reserve(settings, identity, connection)
        budget = RequestBudget(settings.resource_request_calls, connection)
        token = _current_budget.set(budget)
        try:
            yield budget
        finally:
            _current_budget.reset(token)
            # Crash or failed settlement leaves the reservation charged. Do not
            # retry refunds: an ambiguous commit must never create free calls.
            unused = budget.limit - budget.used
            if unused:
                with connection.transaction():
                    for scope, subject, _, _ in _scopes(settings, identity):
                        connection.execute(
                            "UPDATE resource_daily_usage SET model_calls=model_calls-%s "
                            "WHERE company_id=%s AND scope=%s AND subject_id=%s AND usage_day=%s",
                            (unused, identity.company_id, scope, subject, day))
    finally:
        slots.close()

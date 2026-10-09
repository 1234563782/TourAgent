"""Bounded official pools; session owners never compete with short SQL work."""

import atexit
from contextlib import contextmanager
from threading import Lock

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

_pools = {}
_lock = Lock()


def _reset(connection):
    # A returned session must not retain locks, settings, roles or temp objects.
    connection.execute("DISCARD ALL")


def _pool(settings, database_url, lane):
    size = settings.db_session_pool_size if lane == "session" else settings.db_pool_size
    key = (database_url, lane, size, settings.db_pool_waiting,
           settings.db_pool_timeout, settings.memory_connect_timeout)
    with _lock:
        if key not in _pools:
            _pools[key] = ConnectionPool(
                database_url, min_size=0, max_size=size, open=True,
                max_waiting=settings.db_pool_waiting, timeout=settings.db_pool_timeout,
                kwargs={"autocommit": True, "row_factory": dict_row, "prepare_threshold": None,
                        "connect_timeout": settings.memory_connect_timeout},
                check=ConnectionPool.check_connection, reset=_reset,
            )
        return _pools[key]


@contextmanager
def pooled_connection(settings, database_url, *, lane="query"):
    # Schema is deliberately not part of the key: every borrower sets its own
    # search_path, and isolated test schemas must not create unbounded pool threads.
    with _pool(settings, database_url, lane).connection() as connection:
        yield connection


def close_pools():
    with _lock:
        pools = list(_pools.values())
        _pools.clear()
    for pool in pools:
        pool.close()


atexit.register(close_pools)

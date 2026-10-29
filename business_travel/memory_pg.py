"""PostgreSQL business memory and identity-scoped official checkpoints."""

import re
from contextlib import contextmanager
from dataclasses import dataclass

from psycopg import sql
from psycopg.types.json import Jsonb
from langgraph.checkpoint.postgres import PostgresSaver

from .memory import validate_id
from .db_pool import pooled_connection


@dataclass(frozen=True)
class MemoryIdentity:
    # HTTP callers must construct this value from the verified server-side session.
    user_id: str

    def __post_init__(self):
        validate_id(self.user_id)

    def thread_id(self, session_id: str) -> str:
        return f"{self.user_id}:{validate_id(session_id)}"

    def check_thread(self, thread_id: str):
        prefix = f"{self.user_id}:"
        if not isinstance(thread_id, str) or not thread_id.startswith(prefix):
            raise PermissionError("Conversation does not belong to this identity")
        validate_id(thread_id[len(prefix):])


@contextmanager
def memory_connection(settings, *, lane="query"):
    if settings.missing_memory_config():
        raise ValueError("Configure TRAVEL_MEMORY_DATABASE_URL")
    schema = settings.memory_schema
    if (not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema)
            or schema == "public" or schema.startswith("pg_")):
        raise ValueError("A dedicated memory schema is required")
    with pooled_connection(settings, settings.memory_database_url, lane=lane) as connection:
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        connection.execute("SELECT set_config('statement_timeout', %s, false)",
                           (str(int(settings.timeout * 1000)),))
        yield connection


def setup_memory(settings):
    """Explicit deployment step; never invoked by a model or normal CLI startup."""
    with memory_connection(settings) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
            sql.Identifier(settings.memory_schema)))
        with connection.transaction():
            # Explicit init only. Drops the retired preference table and does not recreate it.
            connection.execute("DROP TABLE IF EXISTS user_preferences")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS trips (
                    user_id TEXT NOT NULL,
                    draft_id TEXT NOT NULL,
                    data JSONB NOT NULL CHECK (jsonb_typeof(data) = 'object'),
                    sequence BIGINT GENERATED ALWAYS AS IDENTITY,
                    saved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (user_id, draft_id)
                )
            """)
            connection.execute("""
                CREATE INDEX IF NOT EXISTS trips_user_recent
                ON trips (user_id, sequence DESC)
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS memory_imports (
                    user_id TEXT NOT NULL,
                    source_digest TEXT NOT NULL,
                    summary JSONB NOT NULL,
                    imported_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (user_id, source_digest)
                )
            """)
        # Official setup contains CREATE INDEX CONCURRENTLY and needs autocommit.
        PostgresSaver(connection).setup()
    from .conversations import setup_conversations
    setup_conversations(settings)
    from .chat_runs import setup_chat_runs
    setup_chat_runs(settings)
    from .resources import setup_resources
    setup_resources(settings)
    from .attachment_store import setup_attachments
    setup_attachments(settings)


class PostgresTravelMemory:
    def __init__(self, settings, identity: MemoryIdentity):
        self.settings = settings
        self.identity = identity
        self.user_id = identity.user_id

    @property
    def _scope(self):
        return (self.identity.user_id,)

    def read(self) -> dict:
        with memory_connection(self.settings) as connection, connection.transaction():
            trips = connection.execute(
                'SELECT data FROM trips WHERE user_id=%s ORDER BY sequence DESC LIMIT %s', (*self._scope, self.settings.memory_history_limit),
            ).fetchall()
        return {"trip_history": [row["data"] for row in trips]}

    def save_trip(self, draft: dict):
        if not isinstance(draft, dict) or not isinstance(draft.get("id"), str) or not draft["id"]:
            raise ValueError("A trip must have a nonempty string ID")
        with memory_connection(self.settings) as connection, connection.transaction():
            connection.execute(
                'INSERT INTO trips(user_id,draft_id,data) VALUES(%s,%s,%s) ON CONFLICT(user_id,draft_id) DO NOTHING',
                (*self._scope, draft["id"], Jsonb(draft)),
            )
            saved = connection.execute(
                'SELECT data FROM trips WHERE user_id=%s AND draft_id=%s',
                (*self._scope, draft["id"]),
            ).fetchone()
            if saved["data"] != draft:
                raise ValueError("A different trip already uses this ID; existing record was retained")


class ScopedPostgresSaver(PostgresSaver):
    """Reject cross-identity configurations before the official saver touches SQL."""

    def __init__(self, connection, identity: MemoryIdentity):
        super().__init__(connection)
        self.identity = identity

    def _check(self, config):
        if not isinstance(config, dict):
            raise PermissionError("An identity-scoped conversation is required")
        self.identity.check_thread(config.get("configurable", {}).get("thread_id"))

    def get_tuple(self, config):
        self._check(config)
        return super().get_tuple(config)

    def list(self, config, *, filter=None, before=None, limit=None):
        self._check(config)
        if before and "thread_id" in before.get("configurable", {}):
            self._check(before)
        return super().list(config, filter=filter, before=before, limit=limit)

    def put(self, config, checkpoint, metadata, new_versions):
        self._check(config)
        return super().put(config, checkpoint, metadata, new_versions)

    def put_writes(self, config, writes, task_id, task_path=""):
        self._check(config)
        return super().put_writes(config, writes, task_id, task_path)

    def delete_thread(self, thread_id):
        self.identity.check_thread(thread_id)
        return super().delete_thread(thread_id)

    def get_delta_channel_history(self, *, config, channels):
        self._check(config)
        return super().get_delta_channel_history(config=config, channels=channels)


@contextmanager
def open_checkpointer(settings, identity, connection=None):
    if connection is not None:
        # Reuse the conversation-lock session when the caller already owns one.
        # Session advisory locks and checkpoint writes then consume one pool
        # connection instead of two for the lifetime of a chat run.
        yield ScopedPostgresSaver(connection, identity)
        return
    with memory_connection(settings, lane="session") as connection:
        yield ScopedPostgresSaver(connection, identity)

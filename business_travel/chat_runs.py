"""Identity-scoped request ledger. Checkpoints remain the conversation source."""

import json
from hashlib import sha256

from .execution_events import progress_event
from .memory_pg import memory_connection


REVIEW_STATUSES = frozenset({"failed", "interrupted"})
FINISHED_STATUSES = frozenset({"completed", "awaiting_confirmation"})


def setup_chat_runs(settings):
    with memory_connection(settings) as connection, connection.transaction():
        connection.execute("""
            CREATE TABLE IF NOT EXISTS chat_runs (
                user_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                id TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN
                    ('running','completed','awaiting_confirmation','failed','interrupted')),
                stage TEXT NOT NULL DEFAULT 'accepted',
                base_checkpoint_id TEXT,
                checkpoint_id TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (user_id,conversation_id,id),
                FOREIGN KEY (user_id,conversation_id)
                    REFERENCES conversations(user_id,id),
                CHECK (status NOT IN ('completed','awaiting_confirmation')
                    OR checkpoint_id IS NOT NULL)
            )
        """)
        connection.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS chat_runs_one_running
            ON chat_runs(user_id,conversation_id) WHERE status='running'
        """)
        connection.execute("""
            CREATE INDEX IF NOT EXISTS chat_runs_recent
            ON chat_runs(user_id,conversation_id,created_at DESC,id DESC)
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS chat_recoveries (
                user_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                review_revision TEXT NOT NULL,
                source_checkpoint_id TEXT,
                target_conversation_id TEXT NOT NULL,
                target_checkpoint_id TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (user_id,conversation_id,run_id),
                UNIQUE (user_id,target_conversation_id),
                FOREIGN KEY (user_id,conversation_id,run_id)
                    REFERENCES chat_runs(user_id,conversation_id,id),
                FOREIGN KEY (user_id,target_conversation_id)
                    REFERENCES conversations(user_id,id)
            )
        """)


def request_digest(body):
    action = body.model_dump(exclude={"request_id"}, exclude_none=True)
    # Keep request IDs created before attachments existed replay-compatible.
    if not action.get("attachment_ids"):
        action.pop("attachment_ids", None)
    return sha256(json.dumps(action, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":")).encode("utf-8")).hexdigest()


def public_run(row):
    if row is None:
        return None
    return {
        "id": row["id"], "status": row["status"],
        "progress": progress_event(row["stage"]),
        "needs_review": row["status"] in REVIEW_STATUSES,
    }


class ChatRunStore:
    def __init__(self, settings, identity, conversation_id):
        self.settings = settings
        self.scope = (identity.user_id, conversation_id)

    def get(self, request_id):
        with memory_connection(self.settings) as connection:
            return connection.execute(
                'SELECT * FROM chat_runs WHERE user_id=%s AND conversation_id=%s AND id=%s', (*self.scope, request_id)).fetchone()

    def latest(self):
        with memory_connection(self.settings) as connection:
            return connection.execute(
                'SELECT * FROM chat_runs WHERE user_id=%s AND conversation_id=%s ORDER BY created_at DESC,id DESC LIMIT 1',
                self.scope).fetchone()

    def interrupt_orphans(self):
        """Caller MUST own the conversation advisory lock, not just observe a timeout."""
        with memory_connection(self.settings) as connection:
            connection.execute(
                "UPDATE chat_runs SET status='interrupted',updated_at=now() WHERE user_id=%s AND conversation_id=%s AND status='running'",
                self.scope)

    def start(self, request_id, digest, base_checkpoint_id):
        # The conversation lock serializes get/start; the PK is a second guard.
        with memory_connection(self.settings) as connection:
            return connection.execute(
                "INSERT INTO chat_runs(user_id,conversation_id,id,request_hash,status,base_checkpoint_id) VALUES(%s,%s,%s,%s,'running',%s) RETURNING *",
                (*self.scope, request_id, digest, base_checkpoint_id)).fetchone()

    def progress(self, request_id, stage):
        if progress_event(stage) is None:
            return
        with memory_connection(self.settings) as connection:
            connection.execute(
                "UPDATE chat_runs SET stage=%s,updated_at=now() WHERE user_id=%s AND conversation_id=%s AND id=%s AND status='running'",
                (stage, *self.scope, request_id))

    def finish(self, request_id, status, checkpoint_id=None):
        if status not in FINISHED_STATUSES | REVIEW_STATUSES:
            raise ValueError("Invalid run outcome")
        if status in FINISHED_STATUSES and not checkpoint_id:
            raise ValueError("A completed run requires a checkpoint")
        with memory_connection(self.settings) as connection:
            row = connection.execute(
                "UPDATE chat_runs SET status=%s,checkpoint_id=%s,updated_at=now() WHERE user_id=%s AND conversation_id=%s AND id=%s AND status='running' RETURNING *",
                (status, checkpoint_id, *self.scope, request_id)).fetchone()
        if row is None:
            raise RuntimeError("Run no longer owns an active execution")
        return row

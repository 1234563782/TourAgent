"""User-owned web conversation metadata; graph state stays in checkpoints."""

import re
from uuid import uuid4

from .memory_pg import memory_connection


def setup_conversations(settings):
    """Explicit, additive deployment step; no checkpoint or preference changes."""
    with memory_connection(settings) as connection, connection.transaction():
        connection.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                company_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                id TEXT NOT NULL,
                title TEXT NOT NULL,
                titled BOOLEAN NOT NULL DEFAULT false,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (company_id, user_id, id)
            )
        """)
        connection.execute("""
            CREATE INDEX IF NOT EXISTS conversations_user_recent
            ON conversations (company_id, user_id, updated_at DESC, id DESC)
        """)


class ConversationStore:
    def __init__(self, settings, identity):
        if identity.company_id != settings.memory_company_id:
            raise PermissionError("Company mismatch")
        self.settings = settings
        self.scope = (identity.company_id, identity.user_id)

    def ensure_legacy(self):
        # Keep the existing web thread address, including its pending interrupts.
        with memory_connection(self.settings) as connection:
            connection.execute(
                "INSERT INTO conversations(company_id,user_id,id,title,titled) "
                "VALUES(%s,%s,'web',%s,true) ON CONFLICT DO NOTHING",
                (*self.scope, "原有对话"))

    def list(self, offset=0, limit=50):
        self.ensure_legacy()
        with memory_connection(self.settings) as connection:
            rows = connection.execute(
                "SELECT id,title,created_at,updated_at FROM conversations "
                "WHERE company_id=%s AND user_id=%s "
                "ORDER BY updated_at DESC,id DESC LIMIT %s OFFSET %s",
                (*self.scope, limit + 1, offset)).fetchall()
        return {"items": rows[:limit],
                "next_offset": offset + limit if len(rows) > limit else None}

    def create(self):
        ident = uuid4().hex
        with memory_connection(self.settings) as connection:
            return connection.execute(
                "INSERT INTO conversations(company_id,user_id,id,title) VALUES(%s,%s,%s,%s) "
                "RETURNING id,title,created_at,updated_at",
                (*self.scope, ident, "新对话")).fetchone()

    def require(self, ident):
        if not re.fullmatch(r"(?:web|[0-9a-f]{32})", ident):
            raise LookupError("Conversation not found")
        if ident == "web":
            self.ensure_legacy()
        with memory_connection(self.settings) as connection:
            row = connection.execute(
                "SELECT id,title,created_at,updated_at FROM conversations "
                "WHERE company_id=%s AND user_id=%s AND id=%s",
                (*self.scope, ident)).fetchone()
        if row is None:
            raise LookupError("Conversation not found")
        return row

    def touch(self, ident, message=None):
        title = " ".join(message.split())[:60] if message else None
        with memory_connection(self.settings) as connection:
            connection.execute(
                "UPDATE conversations SET updated_at=now(), "
                "title=CASE WHEN NOT titled AND %s::text IS NOT NULL THEN %s ELSE title END, "
                "titled=titled OR %s::text IS NOT NULL "
                "WHERE company_id=%s AND user_id=%s AND id=%s",
                (title, title, title, *self.scope, ident))

"""School-local invoice registry, private original files, explicit schema setup."""

import json
from decimal import Decimal
from dataclasses import dataclass
from hashlib import sha256
from uuid import uuid4

from psycopg import sql
from psycopg.types.json import Jsonb

from .memory_pg import memory_connection


def invoice_fingerprint(invoice):
    values = invoice.model_dump(mode="json", exclude={
        "evidence", "signature_status", "verification_status"})
    # Equivalent decimals (100 / 100.00) must not create false content conflicts.
    for field in ("amount_net", "tax_amount", "amount_total"):
        if values[field] is not None:
            values[field] = str(Decimal(values[field]).normalize())
    for item in values["items"]:
        for field in ("quantity", "unit_price", "amount_net", "tax_amount", "amount_total"):
            if item[field] is not None:
                item[field] = str(Decimal(item[field]).normalize())
    return sha256(json.dumps(values, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class Registration:
    status: str
    invoice_id: str | None


def setup_invoices(settings):
    """Explicit administrator operation; never called on an upload."""
    with memory_connection(settings) as connection, connection.transaction():
        connection.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
            sql.Identifier(settings.memory_schema)))
        connection.execute("""
            CREATE TABLE IF NOT EXISTS invoice_registry (
                invoice_number TEXT NOT NULL,
                invoice_id TEXT NOT NULL, owner_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL, data JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY(invoice_number),
                UNIQUE(invoice_id)
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS invoice_files (
                user_id TEXT NOT NULL, file_hash TEXT NOT NULL,
                invoice_id TEXT, original BYTEA NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY(user_id, file_hash),
                FOREIGN KEY(invoice_id)
                    REFERENCES invoice_registry(invoice_id)
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS invoice_reviews (
                user_id TEXT NOT NULL, review_id TEXT NOT NULL,
                file_hash TEXT NOT NULL, data JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY(user_id, review_id),
                FOREIGN KEY(user_id, file_hash)
                    REFERENCES invoice_files(user_id, file_hash)
            )
        """)


class PostgresInvoiceStore:
    def __init__(self, settings, identity):
        self.settings, self.identity = settings, identity

    def save_review(self, data, result):
        user = self.identity.user_id
        ident = uuid4().hex
        payload = result.model_copy(update={"review_id": ident}).model_dump(mode="json")
        with memory_connection(self.settings) as conn, conn.transaction():
            conn.execute(
                'INSERT INTO invoice_files(user_id,file_hash,invoice_id,original) VALUES(%s,%s,%s,%s) ON CONFLICT(user_id,file_hash) DO NOTHING',
                (user, result.file_hash, result.invoice_id, data))
            conn.execute(
                'INSERT INTO invoice_reviews(user_id,review_id,file_hash,data) VALUES(%s,%s,%s,%s)',
                (user, ident, result.file_hash, Jsonb(payload)))
        return ident

    def register(self, invoice, digest, data):
        user = self.identity.user_id
        ident, fingerprint = uuid4().hex, invoice_fingerprint(invoice)
        # A unique key, not SELECT-then-INSERT, serializes concurrent imports of the same ticket.
        with memory_connection(self.settings) as conn, conn.transaction():
            inserted = conn.execute(
                'INSERT INTO invoice_registry(invoice_number,invoice_id,owner_id,fingerprint,data) VALUES(%s,%s,%s,%s,%s) ON CONFLICT(invoice_number) DO NOTHING RETURNING invoice_id',
                (invoice.number, ident, user, fingerprint, Jsonb(invoice.model_dump(mode="json"))),
            ).fetchone()
            row = conn.execute(
                'SELECT invoice_id,owner_id,fingerprint FROM invoice_registry WHERE invoice_number=%s FOR UPDATE',
                (invoice.number,),
            ).fetchone()
            if row["fingerprint"] != fingerprint:
                return Registration("conflict", None)
            if row["owner_id"] != user:
                # Never return another user's stored invoice, file, identity or private record ID.
                return Registration("same_invoice", None)
            saved = conn.execute(
                'INSERT INTO invoice_files(user_id,file_hash,invoice_id,original) VALUES(%s,%s,%s,%s) ON CONFLICT(user_id,file_hash) DO NOTHING RETURNING file_hash', (user, digest, row["invoice_id"], data),
            ).fetchone()
            return Registration("new" if inserted else "same_invoice" if saved else "same_file",
                                row["invoice_id"])

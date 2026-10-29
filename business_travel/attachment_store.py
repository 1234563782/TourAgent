"""Private, identity-scoped attachment storage for chat uploads."""

import base64
import binascii
import re
from hashlib import sha256
from uuid import uuid4

from psycopg import sql
from psycopg.types.json import Jsonb

from .attachment_contracts import AttachmentInspection, AttachmentReason, AttachmentUpload
from .invoice_xml import InvoiceParseError, parse_invoice_xml
from .memory_pg import memory_connection


class AttachmentInputError(ValueError):
    """Safe user-facing upload validation error, never a storage exception."""


def setup_attachments(settings):
    """Create attachment tables as an explicit administrator operation."""
    with memory_connection(settings) as connection, connection.transaction():
        connection.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
            sql.Identifier(settings.memory_schema)))
        connection.execute("""
            CREATE TABLE IF NOT EXISTS attachments (
                user_id TEXT NOT NULL,
                attachment_id TEXT NOT NULL,
                filename TEXT NOT NULL,
                file_hash TEXT NOT NULL,
                byte_size INTEGER NOT NULL,
                attachment_kind TEXT NOT NULL,
                attachment_status TEXT NOT NULL,
                data JSONB NOT NULL CHECK (jsonb_typeof(data) = 'object'),
                original BYTEA NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY(user_id, attachment_id),
                UNIQUE(user_id, file_hash)
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS attachment_links (
                user_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                request_id TEXT NOT NULL,
                attachment_id TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY(user_id, conversation_id, request_id, attachment_id),
                FOREIGN KEY(user_id, attachment_id)
                    REFERENCES attachments(user_id, attachment_id),
                FOREIGN KEY(user_id, conversation_id, request_id)
                    REFERENCES chat_runs(user_id, conversation_id, id)
            )
        """)
        connection.execute("""
            CREATE INDEX IF NOT EXISTS attachment_links_recent
            ON attachment_links(user_id, conversation_id, created_at DESC)
        """)


def _safe_summary(invoice):
    if invoice is None:
        return {}
    return {"invoice": {key: value for key, value in {
        "number": invoice.number,
        "issue_date": invoice.issue_date.isoformat() if invoice.issue_date else None,
        "amount_total": str(invoice.amount_total) if invoice.amount_total is not None else None,
        "buyer_name": invoice.buyer_name,
        "seller_name": invoice.seller_name,
        "invoice_type": invoice.invoice_type,
    }.items() if value is not None}}


def inspect_upload(upload: AttachmentUpload, *, settings, store):
    """Decode, classify and persist one upload without registering an invoice."""
    if len(upload.content_base64) > ((settings.attachment_max_file_bytes + 2) // 3) * 4:
        raise AttachmentInputError("文件超过单附件大小限制。")
    try:
        data = base64.b64decode(upload.content_base64, validate=True)
    except (ValueError, binascii.Error):
        raise AttachmentInputError("上传内容编码错误，请重新选择文件。") from None
    if not data or len(data) > settings.attachment_max_file_bytes:
        raise AttachmentInputError("文件为空或超过单附件大小限制。")

    digest = sha256(data).hexdigest()
    result = AttachmentInspection(filename=upload.filename, file_hash=digest, byte_size=len(data))
    if not data.removeprefix(b"\xef\xbb\xbf").lstrip().startswith(b"<"):
        result = result.model_copy(update={
            "attachment_status": "unsupported",
            "reasons": [AttachmentReason(code="unsupported_file_type",
                detail="P0 仅解析 XML；文件已保存为附件，但尚未解析。")],
        })
    else:
        try:
            invoice = parse_invoice_xml(data, max_bytes=settings.attachment_max_file_bytes)
            result = result.model_copy(update={
                "attachment_kind": "invoice_xml",
                "attachment_status": "parsed",
                "parsed_summary": _safe_summary(invoice),
                "invoice": invoice,
            })
        except InvoiceParseError as error:
            result = result.model_copy(update={
                "attachment_kind": error.attachment_kind,
                "attachment_status": error.attachment_status,
                "reasons": [AttachmentReason(code=error.code, detail=error.detail)],
            })
    return store.save(data, result)


class PostgresAttachmentStore:
    def __init__(self, settings, identity):
        self.settings, self.identity = settings, identity

    def save(self, data, result):
        attachment_id = uuid4().hex
        payload = result.model_copy(update={"attachment_id": attachment_id}).model_dump(mode="json")
        user = self.identity.user_id
        with memory_connection(self.settings) as connection, connection.transaction():
            row = connection.execute(
                'SELECT data FROM attachments WHERE user_id=%s AND file_hash=%s',
                (user, result.file_hash)).fetchone()
            if row is not None:
                existing = row["data"]
                if hasattr(existing, "value"):
                    existing = existing.value
                return AttachmentInspection.model_validate(existing)
            connection.execute(
                'INSERT INTO attachments(user_id,attachment_id,filename,file_hash,byte_size,attachment_kind,attachment_status,data,original) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(user_id,file_hash) DO NOTHING',
                (user, attachment_id, result.filename, result.file_hash,
                 result.byte_size, result.attachment_kind, result.attachment_status,
                 Jsonb(payload), data))
            row = connection.execute(
                'SELECT data FROM attachments WHERE user_id=%s AND file_hash=%s',
                (user, result.file_hash)).fetchone()
        existing = row["data"]
        if hasattr(existing, "value"):
            existing = existing.value
        return AttachmentInspection.model_validate(existing)

    def _validate_ids(self, attachment_ids):
        if not isinstance(attachment_ids, list) or len(attachment_ids) > self.settings.attachment_max_files:
            raise LookupError("附件数量超过限制")
        if any(not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{32}", item)
               for item in attachment_ids) or len(set(attachment_ids)) != len(attachment_ids):
            raise LookupError("附件标识无效")

    def get(self, attachment_id):
        self._validate_ids([attachment_id])
        with memory_connection(self.settings) as connection:
            row = connection.execute(
                'SELECT data FROM attachments WHERE user_id=%s AND attachment_id=%s',
                (self.identity.user_id, attachment_id)).fetchone()
        if row is None:
            raise LookupError("附件不存在或无权访问")
        return AttachmentInspection.model_validate(row["data"])

    def load_for_chat(self, attachment_ids):
        self._validate_ids(attachment_ids)
        if not attachment_ids:
            return []
        user = self.identity.user_id
        with memory_connection(self.settings) as connection:
            rows = connection.execute(
                'SELECT attachment_id,filename,attachment_kind,attachment_status FROM attachments WHERE user_id=%s AND attachment_id = ANY(%s)', (user, attachment_ids)).fetchall()
        by_id = {row["attachment_id"]: {
            "attachment_id": row["attachment_id"], "filename": row["filename"],
            "attachment_kind": row["attachment_kind"], "attachment_status": row["attachment_status"],
        } for row in rows}
        if len(by_id) != len(attachment_ids):
            raise LookupError("附件不存在或无权访问")
        return [by_id[item] for item in attachment_ids]

    def link_message(self, conversation_id, request_id, attachment_ids):
        self._validate_ids(attachment_ids)
        if not attachment_ids:
            return
        user = self.identity.user_id
        with memory_connection(self.settings) as connection, connection.transaction():
            for attachment_id in attachment_ids:
                connection.execute(
                    'INSERT INTO attachment_links(user_id,conversation_id,request_id,attachment_id) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                    (user, conversation_id, request_id, attachment_id))

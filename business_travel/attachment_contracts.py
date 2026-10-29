"""Contracts for private chat attachments."""

from typing import Literal

from pydantic import Field, field_validator

from .contracts import Contract
from .invoice_contracts import InvoiceDraft


class AttachmentUpload(Contract):
    filename: str = Field(min_length=1, max_length=200)
    content_base64: str = Field(min_length=1)

    @field_validator("filename")
    @classmethod
    def valid_filename(cls, value):
        if not value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError("Invalid filename")
        # Display names are never filesystem paths.
        return value.replace("\\", "/").split("/")[-1] or "attachment"


class AttachmentReason(Contract):
    code: str = Field(min_length=1, max_length=80)
    detail: str = Field(min_length=1, max_length=500)


class AttachmentInspection(Contract):
    attachment_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    filename: str
    file_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    byte_size: int = Field(ge=0)
    attachment_kind: Literal["invoice_xml", "xml_other", "unknown"] = "unknown"
    attachment_status: Literal["parsed", "unsupported", "invalid", "manual_review"] = "invalid"
    reasons: list[AttachmentReason] = Field(default_factory=list)
    # Owner-visible preview only; chat receives metadata, not parsed content.
    parsed_summary: dict = Field(default_factory=dict)
    # Private parsed evidence is available through owner-authenticated APIs,
    # never sent to the router or treated as facts supplied by the user.
    invoice: InvoiceDraft | None = None

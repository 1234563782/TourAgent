"""Invoice imports are evidence, not user quotes or verified reimbursements."""

from datetime import date
from decimal import Decimal
from typing import Any, Literal

from pydantic import Field

from .contracts import Contract
from .policy_rules import ExpenseFacts


class InvoiceEvidence(Contract):
    source: Literal["invoice"] = "invoice"
    file_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    xml_path: str
    raw_value: str


class InvoiceItem(Contract):
    name: str | None = None
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    amount_net: Decimal | None = None
    tax_amount: Decimal | None = None
    amount_total: Decimal | None = None
    tax_rate: str | None = None


class InvoiceDraft(Contract):
    number: str | None = None
    identifier: str | None = None
    issue_date: date | None = None
    issue_time: str | None = None
    buyer_name: str | None = None
    buyer_tax_id: str | None = None
    seller_name: str | None = None
    seller_tax_id: str | None = None
    amount_net: Decimal | None = None
    tax_amount: Decimal | None = None
    amount_total: Decimal | None = None
    blue_flag: str | None = None
    vat_type: str | None = None
    invoice_type: Literal["digital_vat"] = "digital_vat"
    carrier: Literal["xml"] = "xml"
    adapter: Literal["einvoice-0.32"] = "einvoice-0.32"
    extraction_method: Literal["xml"] = "xml"
    signature_status: Literal["present_not_verified", "absent"] = "absent"
    verification_status: Literal["not_checked"] = "not_checked"
    items: list[InvoiceItem] = Field(default_factory=list)
    evidence: dict[str, InvoiceEvidence] = Field(default_factory=dict)


class InvoiceReason(Contract):
    code: str
    detail: str
    field: str | None = None
    evidence: InvoiceEvidence | None = None


class InvoiceExpenseInput(Contract):
    # A voucher may support several explicitly supplied expense lines.
    line_id: str = Field(min_length=1, max_length=64)
    facts: ExpenseFacts


class InvoiceUpload(Contract):
    filename: str = Field(min_length=1, max_length=200)
    content_base64: str
    # Validate expense lines separately so one malformed line cannot reject the whole batch.
    expenses: list[Any] = Field(default_factory=list, max_length=20)


class InvoiceBatch(Contract):
    files: list[InvoiceUpload] = Field(min_length=1, max_length=20)


class InvoiceReview(Contract):
    filename: str
    file_hash: str | None = None
    invoice_id: str | None = None
    review_id: str | None = None
    verdict: Literal["local_checks_passed", "rejected", "manual_review", "duplicate", "unsupported", "failed"]
    # File format and business document kind are separate from the JEV intent.
    attachment_kind: Literal["invoice_xml", "xml_other", "unknown"] = "unknown"
    attachment_status: Literal["parsed", "unsupported", "invalid", "manual_review"] = "invalid"
    reasons: list[InvoiceReason] = Field(default_factory=list)
    invoice: InvoiceDraft | None = None
    policy_lines: list[dict] = Field(default_factory=list)
    verification_status: Literal["not_checked"] = "not_checked"
    reportable_amount: None = None
    approved: Literal[False] = False
    notice: str = "仅作本地预审，未官方查验或验签，不是报销批准；查重仅表示本系统曾接收。"

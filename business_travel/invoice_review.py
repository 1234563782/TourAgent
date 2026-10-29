"""Deterministic local checks. No LLM, tax-platform verification or approval."""

import base64
import binascii
import json
import logging
import re
from datetime import date
from decimal import Decimal
from hashlib import sha256

from pydantic import ValidationError

from .invoice_contracts import InvoiceExpenseInput, InvoiceReason, InvoiceReview
from .invoice_xml import InvoiceParseError, parse_invoice_xml
from .personnel import PersonnelProfile, apply_account_identity
from .policy_rules import ExpenseFacts
from .result_labels import policy_reason_label

LOG = logging.getLogger(__name__)


class InvoicePolicy:
    """Create a policy backend only when a line actually needs one."""
    def __init__(self, settings):
        self.settings = settings
        self.backend = None

    def estimate(self, facts):
        if self.backend is None:
            from .policy import PolicyStore
            self.backend = PolicyStore.from_settings(self.settings)
        return self.backend.estimate(facts)


def basic_review(invoice, *, buyer_name, buyer_tax_id):
    reasons = []
    rejected = False

    def add(code, detail, field=None, *, reject=False):
        nonlocal rejected
        rejected |= reject
        reasons.append(InvoiceReason(code=code, detail=detail, field=field,
                                     evidence=invoice.evidence.get(field)))

    for field in ("number", "identifier", "issue_date", "seller_name", "seller_tax_id",
                  "buyer_name", "buyer_tax_id", "amount_net", "tax_amount", "amount_total"):
        if getattr(invoice, field) in (None, ""):
            add("required_field", "票据缺少必要字段，请核对原始 XML。", field)
    for field in ("number", "identifier"):
        value = getattr(invoice, field)
        if value and not re.fullmatch(r"[0-9]{20}", value):
            add("invalid_number_format", "支持的数电票号码应为 20 位数字；不使用虚构的校验位。", field, reject=True)
    if invoice.number and invoice.identifier and invoice.number != invoice.identifier:
        add("invoice_identifier_mismatch", "发票号码与头部识别码不一致。", "number", reject=True)
    if invoice.issue_date and invoice.issue_date > date.today():
        add("future_issue_date", "开票日期晚于当前日期，需人工核实。", "issue_date")
    amounts = (invoice.amount_net, invoice.tax_amount, invoice.amount_total)
    if all(value is not None for value in amounts) and amounts[0] + amounts[1] != amounts[2]:
        add("amount_reconciliation", "不含税金额加税额不等于价税合计，请核对原件。", "amount_total", reject=True)
    if invoice.blue_flag not in {"Y", "N"}:
        add("unknown_issue_flag", "缺少可识别的蓝红字标志，需核实票据状态。", "blue_flag")
    if invoice.blue_flag == "N" or any(value is not None and value < 0 for value in amounts):
        add("red_invoice_manual_review", "红字或负数票据须人工关联原票；本版不自动冲抵，也不当作普通重复票拒绝。", "blue_flag")
    if not buyer_name or not buyer_tax_id:
        add("buyer_configuration_required", "管理员尚未配置单位抬头及税号，购买方匹配待核实。", "buyer_name")
    for field, expected in (("buyer_name", buyer_name), ("buyer_tax_id", buyer_tax_id)):
        actual = getattr(invoice, field)
        if actual and expected and actual.strip() != expected.strip():
            add("buyer_mismatch", "购买方与已配置的本单位信息不符，请更换正确票据或人工核实。", field, reject=True)
    if invoice.vat_type not in {"01", "02"}:
        add("vat_type_manual_review", "本版仅核对普通/专用增值税数电票，其他类型需适配。", "vat_type")
    if not invoice.items:
        add("items_required", "未读取到开票明细，请补充原件。", "items")
    else:
        if any(not item.name for item in invoice.items):
            add("item_name_required", "开票明细缺少项目名称，请核对原件。", "items")
        for field, total in (("amount_net", invoice.amount_net), ("tax_amount", invoice.tax_amount),
                             ("amount_total", invoice.amount_total)):
            values = [getattr(item, field) for item in invoice.items]
            if total is not None and all(value is not None for value in values) and sum(values, Decimal(0)) != total:
                add("item_reconciliation", "项目明细合计与票面汇总不一致，需人工核实。", field)
    if invoice.signature_status == "absent":
        add("signature_absent", "未读取到签名节点，请核实原件；本版不执行验签。")
    return ("rejected" if rejected else "manual_review" if reasons else "local_checks_passed"), reasons


def review_expense_lines(invoice, expenses, *, policy, profile):
    lines = []
    seen = set()
    for index, raw in enumerate(expenses):
        try:
            expense = InvoiceExpenseInput.model_validate(raw)
        except ValidationError:
            lines.append({"line_id": str(index + 1), "status": "needs_clarification",
                          "reason": "invalid_expense_facts", "detail": "费用字段不正确，请核对日期、金额和科目。"})
            continue
        ident = expense.line_id
        if ident in seen:
            lines.append({"line_id": ident, "status": "needs_clarification", "reason": "duplicate_line_id"})
            continue
        seen.add(ident)
        values, notes = apply_account_identity(expense.facts.model_dump(), profile or PersonnelProfile())
        # Account absence is not a license to accept caller-supplied personnel ranks.
        for field in ("employee_grade", "transport_standard", "roommate_grade"):
            values[field] = None
        values.update(date_verified=None, expense_scenario="actual")
        facts = ExpenseFacts.model_validate(values)
        if facts.kind not in {"hotel", "transport"}:
            lines.append({"line_id": ident, "status": "needs_clarification",
                          "reason": "voucher_not_allowance_basis"})
            continue
        if facts.kind == "hotel" and facts.occupants != 1:
            lines.append({"line_id": ident, "status": "needs_clarification",
                          "reason": "personal_nightly_cost_required",
                          "detail": "首版只测算明确单人入住的完整票额；合住请补个人逐晚承担额并人工核实。"})
            continue
        # Only an explicit full-invoice link is checked in v1. Never allocate a voucher silently.
        if len(expenses) != 1:
            lines.append({"line_id": ident, "status": "needs_clarification",
                          "reason": "invoice_allocation_requires_review"})
            continue
        submitted = facts.amount
        if facts.kind == "hotel" and facts.amount_scope == "uniform_nightly":
            submitted = (facts.amount * (facts.check_out - facts.expense_date).days
                         if facts.amount is not None and facts.check_out else None)
        elif facts.kind == "hotel" and facts.nightly_amounts:
            submitted = sum((item.amount for item in facts.nightly_amounts), Decimal(0))
        if (submitted is None or submitted != invoice.amount_total
                or facts.currency not in {None, "CNY"} or facts.taxes_confirmed is False):
            lines.append({"line_id": ident, "status": "needs_clarification",
                          "reason": "invoice_expense_amount_mismatch",
                          "detail": "费用行必须明确对应本张票的完整含税金额；首版不分摊、多币种换算或替换金额。"})
            continue
        try:
            # Projection only. No proof that these dates match a voucher or that no double claim exists.
            result = policy.estimate(facts)
            missing = list(result.get("missing_fields", []))
            for item in (result.get("estimate") or {}).get("items", []):
                missing.extend(item.get("missing_fields", []))
            lines.append({"line_id": ident, "facts": facts.model_dump(mode="json"),
                          "status": result.get("status", "insufficient_information"),
                          "reason": result.get("reason"), "policy_result": result,
                          "detail": policy_reason_label(result.get("reason", "")),
                          "missing_fields": list(dict.fromkeys(missing)),
                          "input_source": "user_form_not_compared_with_voucher",
                          "identity_notes": notes, "approved": False,
                          "notice": "差标仅按补充的费用信息测算；开票日期未用作费用日期，未完成跨凭证一致性或报销核验。"})
        except Exception:
            # A policy backend failure must not erase the successful XML/base checks.
            lines.append({"line_id": ident, "status": "failed", "reason": "policy_service_unavailable"})
    return lines


def review_upload(upload, *, settings, registry, policy, profile):
    result = InvoiceReview(filename=upload.filename, verdict="failed")
    data = None
    try:
        if len(json.dumps(upload.expenses, ensure_ascii=False).encode()) > 60000:
            raise InvoiceParseError("expense_metadata_limit", "补充费用信息过多，请拆分后提交。")
        if len(upload.content_base64) > ((settings.invoice_max_file_bytes + 2) // 3) * 4:
            raise InvoiceParseError("file_size", "文件超过单文件大小限制。")
        try:
            data = base64.b64decode(upload.content_base64, validate=True)
        except (ValueError, binascii.Error):
            raise InvoiceParseError("invalid_base64", "上传内容编码错误，请重新选择文件。") from None
        result.file_hash = sha256(data).hexdigest()
        invoice = parse_invoice_xml(data, max_bytes=settings.invoice_max_file_bytes)
        result.invoice = invoice
        result.attachment_kind = "invoice_xml"
        result.attachment_status = "parsed"
        result.verdict, result.reasons = basic_review(
            invoice, buyer_name=settings.invoice_buyer_name, buyer_tax_id=settings.invoice_buyer_tax_id)
        if result.verdict == "rejected":
            return result
        # Do not reserve an invoice key based on incomplete or inconsistent business fields.
        blocked = {"required_field", "unknown_issue_flag", "item_reconciliation", "items_required",
                   "vat_type_manual_review", "item_name_required"}
        if not any(reason.code in blocked for reason in result.reasons):
            registration = registry.register(invoice, result.file_hash, data)
            result.invoice_id = registration.invoice_id
            if registration.status == "conflict":
                result.verdict = "manual_review"
                result.reasons.append(InvoiceReason(code="invoice_content_conflict",
                    detail="同一发票号码已有不同内容，未覆盖历史数据，需人工核实。"))
                return result
            if registration.status in {"same_file", "same_invoice"}:
                if result.verdict == "local_checks_passed":
                    result.verdict = "duplicate"
                result.reasons.append(InvoiceReason(code=registration.status,
                    detail="本系统已接收过相同文件或票号；不代表已报销，不重复创建票据。"))
        if upload.expenses and not any(r.code in blocked | {"red_invoice_manual_review"} for r in result.reasons):
            result.policy_lines = review_expense_lines(invoice, upload.expenses, policy=policy, profile=profile)
        elif not upload.expenses:
            result.reasons.append(InvoiceReason(code="expense_facts_required",
                detail="基础预审已执行；差标待补费用科目、实际日期、城市及金额口径，住宿多晚还需逐晚明细/水单。"))
    except InvoiceParseError as error:
        result.attachment_kind = error.attachment_kind
        result.attachment_status = error.attachment_status
        result.verdict = "unsupported" if error.attachment_status == "unsupported" else "manual_review"
        result.reasons.append(InvoiceReason(code=error.code, detail=error.detail, field=error.field, evidence=error.evidence))
    except (ValidationError, ValueError):
        result.verdict = "manual_review"
        result.reasons.append(InvoiceReason(code="invalid_expense_facts", detail="补充费用字段不正确，请核对后重试。"))
    except Exception as error:
        LOG.warning("invoice_review_failed reason=storage_unavailable type=%s", type(error).__name__)
        result.verdict = "failed"
        result.reasons.append(InvoiceReason(code="invoice_storage_unavailable",
            detail="票据存储暂不可用或尚未初始化；请管理员检查连接并运行 init-invoices 后重试。"))
    finally:
        if data and len(data) <= settings.invoice_max_file_bytes:
            try:
                result.review_id = registry.save_review(data, result)
            except Exception as error:
                LOG.warning("invoice_review_failed reason=record_not_saved type=%s", type(error).__name__)
                result.verdict = "failed"
                if not any(r.code == "invoice_storage_unavailable" for r in result.reasons):
                    result.reasons.append(InvoiceReason(code="invoice_review_not_saved",
                        detail="预审记录未能保存，请管理员检查票据存储后重试；其他票据不受影响。"))
    return result

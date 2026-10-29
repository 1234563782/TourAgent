"""Bounded adapter for the EInvoice 0.32 digital VAT XML layout."""

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from xml.etree import ElementTree as ET

from .invoice_contracts import InvoiceDraft, InvoiceEvidence, InvoiceItem


class InvoiceParseError(ValueError):
    def __init__(self, code, detail, field=None, evidence=None, *,
                 attachment_kind="unknown", attachment_status="invalid"):
        super().__init__(detail)
        self.code, self.detail, self.field, self.evidence = code, detail, field, evidence
        self.attachment_kind = attachment_kind
        self.attachment_status = attachment_status


def parse_invoice_xml(data: bytes, *, max_bytes: int, max_nodes=20000, max_depth=40):
    if not data or len(data) > max_bytes:
        raise InvoiceParseError("file_size", "文件为空或超过单文件大小限制。")
    digest = sha256(data).hexdigest()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeError:
        raise InvoiceParseError("unsupported_encoding", "首版仅支持 UTF-8 XML 原件。",
                                attachment_status="unsupported") from None
    # Reject declarations BEFORE the XML parser can expand entities. UTF-16 is not accepted.
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)", text, re.I):
        raise InvoiceParseError("unsafe_xml", "不接受包含 DTD 或实体声明的 XML。",
                                attachment_status="unsupported")
    declaration = re.match(r"<\?xml[^?]*encoding=['\"]([^'\"]+)", text, re.I)
    if declaration and declaration[1].lower() not in {"utf-8", "utf8"}:
        raise InvoiceParseError("unsupported_encoding", "首版仅支持 UTF-8 XML 原件。",
                                attachment_status="unsupported")
    try:
        parser = ET.XMLPullParser(events=("start", "end"))
        depth = count = 0
        root = None
        for offset in range(0, len(text), 8192):
            parser.feed(text[offset:offset + 8192])
            for event, element in parser.read_events():
                if event == "start":
                    if root is None:
                        root = element
                    count += 1
                    depth += 1
                    if count > max_nodes or depth > max_depth:
                        raise InvoiceParseError("xml_complexity", "XML 节点数量或嵌套层级超过限制。")
                else:
                    depth -= 1
        parser.close()
    except ET.ParseError:
        raise InvoiceParseError("invalid_xml", "XML 格式错误，请上传原始 XML 文件。") from None
    if root is None or root.tag.split("}")[-1] != "EInvoice":
        raise InvoiceParseError(
            "unsupported_layout",
            "这不是当前支持的数电票 XML（根节点不是 EInvoice），不会按发票登记或查重。",
            attachment_kind="xml_other",
            attachment_status="unsupported",
        )
    namespace = root.tag[:-len("EInvoice")]
    evidence = {}
    for path in ("Header", "EInvoiceData", "TaxSupervisionInfo",
                 "EInvoiceData/BasicInformation", "EInvoiceData/BuyerInformation",
                 "EInvoiceData/SellerInformation", "Header/InherentLabel"):
        if len(root.findall("/".join(namespace + part for part in path.split("/")))) > 1:
            raise InvoiceParseError("ambiguous_structure", "发现重复的业务信息节点，不能拼接为同一张票据。")

    def read(path, field, kind="text"):
        nodes = root.findall("/".join(namespace + part for part in path.split("/")))
        if len(nodes) > 1:
            raise InvoiceParseError("ambiguous_field", "同一字段出现多个节点，需人工核对。", field)
        if not nodes:
            return None
        node = nodes[0]
        if len(node):
            raise InvoiceParseError("invalid_field", "字段不应包含嵌套节点。", field)
        value = (node.text or "").strip()
        if not value:
            return None
        proof = InvoiceEvidence(file_hash=digest, xml_path="/EInvoice/" + path, raw_value=value)
        evidence[field] = proof
        if len(value) > 2000:
            raise InvoiceParseError("invalid_field", "字段结构或长度不符合支持的格式。", field, proof)
        try:
            if kind == "money" or kind == "decimal":
                if not re.fullmatch(r"-?[0-9]{1,16}(?:\.[0-9]{1,8})?", value):
                    raise ValueError
                result = Decimal(value)
                if kind == "money" and result != result.quantize(Decimal("0.01")):
                    raise ValueError
                return result
            if kind == "date":
                # Full ISO date/time only: never take a partial string and invent a date.
                return datetime.fromisoformat(value).date()
        except (ValueError, InvalidOperation):
            raise InvoiceParseError("invalid_field", "字段金额或日期格式不正确，需核对原件。", field, proof) from None
        return value

    version = read("Header/Version", "version")
    electronic = read("Header/InherentLabel/EInvoiceType/LabelCode", "electronic_type")
    if version != "0.32" or electronic != "01":
        raise InvoiceParseError(
            "unsupported_version_or_type",
            "文件看起来是 EInvoice，但版本或票种暂不支持；不会按发票登记或查重。",
            attachment_kind="invoice_xml",
            attachment_status="unsupported",
        )
    for node in root.findall(namespace + "EInvoiceData/" + namespace + "SpecificInformation"):
        if len(node) or (node.text or "").strip():
            raise InvoiceParseError(
                "unsupported_specific_invoice",
                "文件是 EInvoice，但包含当前版本未适配的专用业务要素；不会按发票登记或查重。",
                attachment_kind="invoice_xml",
                attachment_status="unsupported",
            )
    basic = "EInvoiceData/BasicInformation/"
    issue_date = read("TaxSupervisionInfo/IssueTime", "issue_date", "date")
    values = {
        "number": read("TaxSupervisionInfo/InvoiceNumber", "number"),
        "identifier": read("Header/EIid", "identifier"),
        "issue_date": issue_date,
        "issue_time": (datetime.fromisoformat(evidence["issue_date"].raw_value).isoformat()
                       if issue_date else None),
        "buyer_name": read("EInvoiceData/BuyerInformation/BuyerName", "buyer_name"),
        "buyer_tax_id": read("EInvoiceData/BuyerInformation/BuyerIdNum", "buyer_tax_id"),
        "seller_name": read("EInvoiceData/SellerInformation/SellerName", "seller_name"),
        "seller_tax_id": read("EInvoiceData/SellerInformation/SellerIdNum", "seller_tax_id"),
        "amount_net": read(basic + "TotalAmWithoutTax", "amount_net", "money"),
        "tax_amount": read(basic + "TotalTaxAm", "tax_amount", "money"),
        "amount_total": read(basic + "TotalTax-includedAmount", "amount_total", "money"),
        "blue_flag": read("Header/InherentLabel/InIssuType/LabelCode", "blue_flag"),
        "vat_type": read("Header/InherentLabel/GeneralOrSpecialVAT/LabelCode", "vat_type"),
    }
    items = []
    for index, _ in enumerate(root.findall(namespace + "EInvoiceData/" + namespace + "IssuItemInformation"), 1):
        prefix = f"EInvoiceData/IssuItemInformation[{index}]/"
        fields = {}
        for name, tag, kind in (("name", "ItemName", "text"), ("quantity", "Quantity", "decimal"),
                                ("unit_price", "UnPrice", "decimal"), ("amount_net", "Amount", "money"),
                                ("tax_amount", "ComTaxAm", "money"),
                                ("amount_total", "TotaltaxIncludedAmount", "money"),
                                ("tax_rate", "TaxRate", "text")):
            fields[name] = read(prefix + tag, f"items.{index - 1}.{name}", kind)
        items.append(InvoiceItem(**fields))
    signature = read("TaxBureauSignature/SignatureValue", "signature")
    # Do not persist the signature blob as a business field; presence is NOT verification.
    evidence.pop("signature", None)
    return InvoiceDraft(**values, items=items, evidence=evidence,
                        signature_status="present_not_verified" if signature else "absent")

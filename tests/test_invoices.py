"""Synthetic EInvoice layout tests; no real tickets, accounts, network or database."""

import base64
import copy
import unittest
from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from business_travel.auth import AuthSettings
from business_travel.invoice_contracts import InvoiceUpload
from business_travel.invoice_review import basic_review, review_upload
from business_travel.invoice_review import InvoicePolicy
from business_travel.invoice_store import Registration, PostgresInvoiceStore, invoice_fingerprint, setup_invoices
from business_travel.invoice_xml import InvoiceParseError, parse_invoice_xml
from business_travel.memory_pg import MemoryIdentity
from business_travel.personnel import PersonnelProfile
from business_travel.settings import Settings
from business_travel.web import create_app


def settings(**changes):
    return replace(Settings("", "", "", "", "", Path("unused"), Path("unused"),
        memory_database_url="postgresql://unused", invoice_buyer_name="测试大学", invoice_buyer_tax_id="TEST-BUYER"), **changes)


def xml(number="26000000000000000001", *, total="1060.00", net="1000.00", tax="60.00", flag="Y"):
    # Field names/layout from a public field-by-field description, with invented test values.
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<EInvoice><Header><EIid>{number}</EIid><EInvoiceTag>SWEI3400</EInvoiceTag><Version>0.32</Version>
<InherentLabel><InIssuType><LabelCode>{flag}</LabelCode></InIssuType>
<EInvoiceType><LabelCode>01</LabelCode></EInvoiceType>
<GeneralOrSpecialVAT><LabelCode>02</LabelCode></GeneralOrSpecialVAT></InherentLabel></Header>
<EInvoiceData><SellerInformation><SellerIdNum>TEST-SELLER</SellerIdNum><SellerName>测试宾馆</SellerName></SellerInformation>
<BuyerInformation><BuyerIdNum>TEST-BUYER</BuyerIdNum><BuyerName>测试大学</BuyerName></BuyerInformation>
<BasicInformation><TotalAmWithoutTax>{net}</TotalAmWithoutTax><TotalTaxAm>{tax}</TotalTaxAm>
<TotalTax-includedAmount>{total}</TotalTax-includedAmount></BasicInformation>
<IssuItemInformation><ItemName>住宿服务</ItemName><Quantity>2</Quantity><UnPrice>500.00</UnPrice>
<Amount>{net}</Amount><TaxRate>0.06</TaxRate><ComTaxAm>{tax}</ComTaxAm>
<TotaltaxIncludedAmount>{total}</TotaltaxIncludedAmount></IssuItemInformation><SpecificInformation/></EInvoiceData>
<TaxSupervisionInfo><InvoiceNumber>{number}</InvoiceNumber><IssueTime>2026-09-20 10:30:00</IssueTime></TaxSupervisionInfo>
<TaxBureauSignature><SignatureValue>synthetic-not-a-signature</SignatureValue></TaxBureauSignature></EInvoice>'''.encode()


def upload(data=None, expenses=None, filename="synthetic.xml"):
    return InvoiceUpload(filename=filename, content_base64=base64.b64encode(data or xml()).decode(),
                         expenses=expenses or [])


class Registry:
    """Test double only; production always uses the PostgreSQL registry."""
    def __init__(self):
        self.invoices, self.files, self.reviews = {}, set(), []

    def register(self, invoice, digest, data):
        fingerprint = invoice_fingerprint(invoice)
        if invoice.number in self.invoices:
            old, ident = self.invoices[invoice.number]
            if old != fingerprint:
                return Registration("conflict", None)
            status = "same_file" if digest in self.files else "same_invoice"
        else:
            ident = str(len(self.invoices) + 1)
            self.invoices[invoice.number] = fingerprint, ident
            status = "new"
        self.files.add(digest)
        return Registration(status, ident)

    def save_review(self, data, result):
        assert sha256(data).hexdigest() == result.file_hash
        self.reviews.append(copy.deepcopy(result))
        return str(len(self.reviews))


class InvoiceTests(unittest.TestCase):
    def setUp(self):
        self.registry, self.policy = Registry(), Mock()
        self.profile = PersonnelProfile(personnel_type="staff", lodging_standard="其他人员")

    def review(self, data=None, expenses=None, **changes):
        return review_upload(upload(data, expenses), settings=settings(**changes), registry=self.registry,
                             policy=self.policy, profile=self.profile)

    def test_parse_fields_and_server_generated_evidence(self):
        data = xml()
        invoice = parse_invoice_xml(data, max_bytes=10000)
        self.assertEqual(invoice.amount_total, Decimal("1060"))
        self.assertEqual(invoice.issue_date.isoformat(), "2026-09-20")
        self.assertEqual(invoice.items[0].quantity, 2)
        proof = invoice.evidence["amount_total"]
        self.assertEqual(proof.source, "invoice")
        self.assertEqual(proof.file_hash, sha256(data).hexdigest())
        self.assertEqual(proof.xml_path, "/EInvoice/EInvoiceData/BasicInformation/TotalTax-includedAmount")
        self.assertEqual(proof.raw_value, "1060.00")
        self.assertEqual(invoice.signature_status, "present_not_verified")
        self.assertEqual(invoice.verification_status, "not_checked")

    def test_namespace_and_utf8_bom(self):
        data = b"\xef\xbb\xbf" + xml().replace(b"<EInvoice>", b'<EInvoice xmlns="urn:synthetic:einvoice">')
        self.assertEqual(parse_invoice_xml(data, max_bytes=10000).items[0].name, "住宿服务")

    def test_exact_decimal_reconciliation_not_float(self):
        result = self.review(xml(total="0.30", net="0.10", tax="0.20"))
        self.assertEqual(result.verdict, "local_checks_passed")
        self.assertFalse(result.approved)
        self.assertIsNone(result.reportable_amount)
        self.assertEqual(result.verification_status, "not_checked")

    def test_missing_fields_stay_missing_not_zero(self):
        result = self.review(xml().replace(b"<TotalTaxAm>60.00</TotalTaxAm>", b""))
        self.assertIsNone(result.invoice.tax_amount)
        self.assertEqual(result.verdict, "manual_review")
        self.assertIn("tax_amount", [r.field for r in result.reasons if r.code == "required_field"])
        self.assertFalse(self.registry.invoices)

    def test_mismatch_buyers_and_totals_rejected(self):
        for data, code in [(xml(total="1061"), "amount_reconciliation"),
            (xml().replace("测试大学".encode(), "其他单位".encode()), "buyer_mismatch"),
            (xml().replace(b"TEST-BUYER", b"DIFFERENT"), "buyer_mismatch")]:
            with self.subTest(code=code):
                result = self.review(data)
                self.assertEqual(result.verdict, "rejected")
                self.assertIn(code, [r.code for r in result.reasons])

    def test_unconfigured_buyer_is_not_passed(self):
        result = self.review(invoice_buyer_name="", invoice_buyer_tax_id="")
        self.assertEqual(result.verdict, "manual_review")
        self.assertIn("buyer_configuration_required", [r.code for r in result.reasons])

    def test_idempotent_file_same_invoice_and_conflict(self):
        first = self.review()
        second = self.review()
        different_file = self.review(xml().replace(b"><", b">\n<"))
        conflict = self.review(xml(net="2000", tax="120", total="2120"))
        self.assertEqual(first.verdict, "local_checks_passed")
        self.assertEqual(second.verdict, "duplicate")
        self.assertEqual(first.invoice_id, second.invoice_id)
        self.assertEqual(different_file.verdict, "duplicate")
        self.assertIn("same_invoice", [r.code for r in different_file.reasons])
        self.assertIn("invoice_content_conflict", [r.code for r in conflict.reasons])
        self.assertEqual(len(self.registry.invoices), 1)
        self.assertEqual(len(self.registry.reviews), 4)

    def test_equivalent_decimal_representation_not_content_conflict(self):
        self.review()
        result = self.review(xml(total="1060", net="1000", tax="60"))
        self.assertEqual(result.verdict, "duplicate")

    def test_red_negative_invoice_requires_manual_and_independent_number(self):
        self.review()
        red = xml("26000000000000000002", net="-1000", tax="-60", total="-1060", flag="N")
        result = self.review(red)
        self.assertEqual(result.verdict, "manual_review")
        self.assertIn("red_invoice_manual_review", [r.code for r in result.reasons])
        self.assertEqual(len(self.registry.invoices), 2)
        self.assertFalse(result.policy_lines)

    def test_unknown_and_unsafe_xmls(self):
        examples = [ (b"<Invoice/>", "unsupported_layout"), (b"<EInvoice>", "invalid_xml"),
            (xml().replace(b"0.32", b"0.99"), "unsupported_version_or_type"),
            (xml().replace(b"<SpecificInformation/>", b"<SpecificInformation><Railway/></SpecificInformation>"), "unsupported_specific_invoice"),
            (b'<!DOCTYPE EInvoice [<!ENTITY x SYSTEM "file:///private">]>' + xml(), "unsafe_xml"),
            (xml().decode().encode("utf-16"), "unsupported_encoding"),
            (b"<EInvoice>" + b"<a>" * 50 + b"</a>" * 50 + b"</EInvoice>", "xml_complexity"),
            (xml().replace(b"<TotalTaxAm>60.00", b"<TotalTaxAm>60.00</TotalTaxAm><TotalTaxAm>60.00"), "ambiguous_field"),
            (xml().replace(b">1060.00<", b">NaN<"), "invalid_field") ]
        for data, code in examples:
            with self.subTest(code=code):
                result = self.review(data)
                self.assertIn(code, [r.code for r in result.reasons])
                self.assertNotEqual(result.verdict, "local_checks_passed")

    def test_unrelated_xml_is_not_registered_as_an_invoice(self):
        result = self.review(b"<?xml version=\"1.0\"?><TravelOrder><total>1060</total></TravelOrder>")
        self.assertEqual(result.verdict, "unsupported")
        self.assertEqual(result.attachment_kind, "xml_other")
        self.assertEqual(result.attachment_status, "unsupported")
        self.assertIsNone(result.invoice)
        self.assertIsNone(result.invoice_id)
        self.assertEqual(len(self.registry.invoices), 0)
        self.assertFalse(result.policy_lines)

    def test_invoice_shaped_xml_keeps_invoice_kind_when_version_is_unsupported(self):
        result = self.review(xml().replace(b"0.32", b"0.99"))
        self.assertEqual(result.attachment_kind, "invoice_xml")
        self.assertEqual(result.attachment_status, "unsupported")
        self.assertIsNone(result.invoice)
        self.assertEqual(len(self.registry.invoices), 0)

    def test_size_limit_and_invalid_base64(self):
        result = self.review(b"x" * 1100, invoice_max_file_bytes=1024)
        self.assertIn("file_size", [r.code for r in result.reasons])
        result = review_upload(InvoiceUpload(filename="bad.xml", content_base64="!invalid"),
            settings=settings(), registry=self.registry, policy=self.policy, profile=self.profile)
        self.assertIn("invalid_base64", [r.code for r in result.reasons])

    def facts(self, **changes):
        return {"line_id": "hotel-1", "facts": {"kind": "hotel", "expense_date": "2026-09-16",
            "destination": "北京", "destination_province": "北京", "amount": "1060", "currency": "CNY",
            "basis": "room_night", "occupants": 1, "check_out": "2026-09-18",
            "amount_scope": "stay_total", **changes}}

    def test_issue_date_never_used_as_expense_date_or_verified(self):
        self.policy.estimate.return_value = {"status": "insufficient_information", "reason": "nightly_amounts_required"}
        result = self.review(expenses=[self.facts(date_verified=True, lodging_standard="部级")])
        sent = self.policy.estimate.call_args.args[0]
        self.assertIsNone(sent.date_verified)
        self.assertEqual(sent.expense_date.isoformat(), "2026-09-16")
        self.assertEqual(sent.lodging_standard, "其他人员")
        self.assertEqual(sent.amount_scope, "stay_total")
        self.assertIsNone(sent.nightly_amounts)
        self.assertEqual(result.policy_lines[0]["reason"], "nightly_amounts_required")

    def test_bad_expense_does_not_erase_base_review_or_other_lines(self):
        result = self.review(expenses=[{"bad": "input"}])
        self.assertEqual(result.verdict, "local_checks_passed")
        self.assertEqual(result.policy_lines[0]["reason"], "invalid_expense_facts")
        self.policy.estimate.assert_not_called()

    def test_policy_failure_does_not_erase_base_result(self):
        self.policy.estimate.side_effect = RuntimeError("private db info")
        result = self.review(expenses=[self.facts()])
        self.assertEqual(result.verdict, "local_checks_passed")
        self.assertEqual(result.policy_lines[0]["reason"], "policy_service_unavailable")
        self.assertNotIn("private", result.model_dump_json())

    def test_line_invoice_amount_must_match(self):
        result = self.review(expenses=[self.facts(amount="999")])
        self.assertEqual(result.policy_lines[0]["reason"], "invoice_expense_amount_mismatch")
        self.policy.estimate.assert_not_called()

    def test_multi_allocation_not_silently_double_counted(self):
        result = self.review(expenses=[self.facts(), self.facts()])
        self.assertEqual(result.policy_lines[0]["reason"], "invoice_allocation_requires_review")
        self.assertEqual(result.policy_lines[1]["reason"], "duplicate_line_id")
        self.policy.estimate.assert_not_called()

    def test_storage_failure_not_reported_as_success_or_leaked(self):
        self.registry.register = Mock(side_effect=RuntimeError("secret://password"))
        result = self.review()
        self.assertEqual(result.verdict, "failed")
        self.assertIn("invoice_storage_unavailable", [r.code for r in result.reasons])
        self.assertNotIn("password", result.model_dump_json())

    def test_real_institutional_engine_preserves_total_and_requires_nightly_detail(self):
        from business_travel.policy_package import prepare_package
        from business_travel.policy_lodging import LodgingRow
        from business_travel.university_policy import UniversityRules, check_university_expense
        from business_travel.reimbursement import estimate_expense
        root = Path(__file__).resolve().parents[1]
        bundle = prepare_package(root / "policies/hfut-2026/manifest.json")
        rules = UniversityRules.model_validate(bundle.rules)
        rows = [LodgingRow.model_validate(row) for row in bundle.lodging_rows]
        self.policy.estimate.side_effect = lambda facts: estimate_expense(facts,
            lambda unit: check_university_expense(unit, rules, rows, projection=True),
            max_nights=366, currency=rules.currency, effective_from=bundle.effective_from,
            effective_to=bundle.effective_to)
        data = xml(total="1000", net="943.40", tax="56.60")
        first = self.review(data, expenses=[self.facts(amount="1000")])
        self.assertIsNone(first.policy_lines[0]["policy_result"]["estimate"]["reference_amount"])
        facts = self.facts(amount="1000", nightly_amounts=[
            {"date": "2026-09-16", "amount": "500"}, {"date": "2026-09-17", "amount": "500"}])
        second = self.review(data, expenses=[facts])
        estimate = second.policy_lines[0]["policy_result"]["estimate"]
        self.assertEqual(Decimal(estimate["reference_amount"]), 1000)
        self.assertEqual(second.policy_lines[0]["facts"]["lodging_standard"], "其他人员")
        self.assertFalse(second.approved)
        self.assertIsNone(second.reportable_amount)


    def test_transport_date_cannot_become_verified_from_a_generic_invoice(self):
        from business_travel.policy_rules import ExpenseFacts
        self.policy.estimate.return_value = {"status": "insufficient_information", "reason": "date_verification_required"}
        facts = {"line_id": "ticket", "facts": {"kind": "transport", "expense_date": "2026-09-16",
            "destination": "北京", "amount": "1060", "currency": "CNY", "taxes_confirmed": True,
            "date_verified": True, "basis": "ticket", "transport_mode": "flight", "seat_class": "经济舱"}}
        result = self.review(expenses=[facts])
        self.assertIsNone(self.policy.estimate.call_args.args[0].date_verified)
        self.assertEqual(result.policy_lines[0]["reason"], "date_verification_required")

    def test_absent_account_never_accepts_caller_rank(self):
        self.profile = None
        self.policy.estimate.return_value = {"status": "insufficient_information", "reason": "lodging_standard_required"}
        self.review(expenses=[self.facts(lodging_standard="部级", academic_title="院士")])
        facts = self.policy.estimate.call_args.args[0]
        self.assertIsNone(facts.lodging_standard)
        self.assertIsNone(facts.academic_title)


class InvoiceWebTests(unittest.TestCase):
    def setUp(self):
        self.auth = Mock()
        self.auth.current_user.return_value = {"user_id": "alice", "username": "alice"}
        self.auth.get_personnel.return_value = PersonnelProfile(lodging_standard="其他人员")
        self.registry = Registry()
        self.factory = Mock(return_value=self.registry)
        self.client = TestClient(create_app(settings(), AuthSettings(), self.auth,
            invoice_store_factory=self.factory), base_url="http://127.0.0.1:8000")
        self.headers = {"Origin": "http://127.0.0.1:8000"}

    def post(self, files):
        return self.client.post("/api/invoices/review", headers=self.headers,
                                json={"files": [f.model_dump(mode="json") for f in files]})

    def test_batch_bad_good_and_rejected_all_have_results(self):
        response = self.post([upload(b"<broken>"), upload(), upload(xml(total="999"))])
        self.assertEqual(response.status_code, 200)
        result = response.json()["results"]
        self.assertEqual([r["verdict"] for r in result], ["manual_review", "local_checks_passed", "rejected"])
        self.factory.assert_called_once_with(settings(), MemoryIdentity("alice"))
        self.assertTrue(all(r["review_id"] for r in result))

    def test_auth_origin_identity_and_body_limits(self):
        self.auth.current_user.return_value = None
        self.assertEqual(self.post([upload()]).status_code, 401)
        self.assertEqual(self.client.get("/api/invoices/config").status_code, 401)
        self.auth.current_user.return_value = {"user_id": "alice", }
        self.assertEqual(self.client.post("/api/invoices/review", json={"files": []}).status_code, 403)
        body = {"files": [upload().model_dump()], "user_id": "victim"}
        self.assertEqual(self.client.post("/api/invoices/review", headers=self.headers, json=body).status_code, 422)
        self.assertEqual(self.client.post("/api/invoices/review?user_id=victim", headers=self.headers,
            json={"files": [upload().model_dump()]}).status_code, 400)
        self.assertEqual(self.client.post("/api/auth/login", headers=self.headers, content=b"x" * 4097).status_code, 413)

    def test_config_and_batch_boundaries(self):
        self.assertEqual(self.client.get("/api/invoices/config").json()["max_batch_files"], 10)
        self.assertEqual(self.post([upload()] * 11).status_code, 413)
        self.factory.assert_not_called()

    def test_invalid_expense_does_not_reject_other_files(self):
        response = self.post([upload(expenses=[{"garbage": "data"}]), upload(xml("26000000000000000003"))])
        self.assertEqual(response.status_code, 200)
        results = response.json()["results"]
        self.assertEqual(results[0]["policy_lines"][0]["reason"], "invalid_expense_facts")
        self.assertEqual(results[1]["verdict"], "local_checks_passed")


class InvoiceSQLTests(unittest.TestCase):
    def setUp(self):
        self.conn = Mock()
        self.conn.transaction.return_value.__enter__ = Mock()
        self.conn.transaction.return_value.__exit__ = Mock(return_value=False)
        @contextmanager
        def connection(_):
            yield self.conn
        self.patcher = patch("business_travel.invoice_store.memory_connection", connection)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.registry = PostgresInvoiceStore(settings(), MemoryIdentity("alice"))
        self.invoice = parse_invoice_xml(xml(), max_bytes=10000)

    def test_company_number_atomic_unique_key_and_no_request_ddl(self):
        self.conn.execute.return_value.fetchone.side_effect = [
            {"invoice_id": "id"}, {"invoice_id": "id", "owner_id": "alice",
                                  "fingerprint": invoice_fingerprint(self.invoice)}, {"file_hash": "f"}]
        self.assertEqual(self.registry.register(self.invoice, sha256(xml()).hexdigest(), xml()).status, "new")
        calls = self.conn.execute.call_args_list
        self.assertIn("ON CONFLICT(invoice_number) DO NOTHING", calls[0].args[0])
        self.assertIn("FOR UPDATE", calls[1].args[0])
        self.assertEqual(calls[1].args[1], (self.invoice.number,))
        self.assertFalse(any("CREATE" in c.args[0] for c in calls))

    def test_cross_user_duplicate_returns_no_private_id_or_stored_data(self):
        self.conn.execute.return_value.fetchone.side_effect = [None,
            {"invoice_id": "private-id", "owner_id": "bob", "fingerprint": invoice_fingerprint(self.invoice)}]
        result = self.registry.register(self.invoice, "f", xml())
        self.assertEqual(result.status, "same_invoice")
        self.assertIsNone(result.invoice_id)
        self.assertEqual(self.conn.execute.call_count, 2)


if __name__ == "__main__":
    unittest.main()

"""P0 attachment lifecycle tests; no real database or uploaded documents."""

import base64
import json
import unittest
from contextlib import nullcontext
from dataclasses import replace
from hashlib import sha256
from unittest.mock import Mock, patch
from uuid import uuid4

from langchain_core.messages import HumanMessage
from pydantic import ValidationError

from fastapi.testclient import TestClient
from business_travel.attachment_contracts import AttachmentInspection, AttachmentUpload
from business_travel.attachment_store import PostgresAttachmentStore, inspect_upload, setup_attachments
from business_travel.chat_runs import request_digest
from business_travel.web import ChatBody, chat_snapshot, create_app
from business_travel.auth import AuthSettings
from business_travel.memory_pg import MemoryIdentity
from tests.test_invoices import settings, xml
from tests import test_web_chat as chat_fixtures
from tests.test_web_chat import state


class FakeAttachmentStore:
    def save(self, data, result):
        return result.model_copy(update={"attachment_id": "a" * 32})


def upload(data, filename="record.xml"):
    return AttachmentUpload(filename=filename,
                             content_base64=base64.b64encode(data).decode())


class AttachmentContractTests(unittest.TestCase):
    def test_unrelated_xml_is_saved_as_private_non_invoice_attachment(self):
        settings = Mock(attachment_max_file_bytes=1024)
        result = inspect_upload(upload(b"<?xml version='1.0'?><TravelOrder><total>1</total></TravelOrder>"),
                                settings=settings, store=FakeAttachmentStore())
        self.assertEqual(result.attachment_id, "a" * 32)
        self.assertEqual(result.attachment_kind, "xml_other")
        self.assertEqual(result.attachment_status, "unsupported")
        self.assertEqual(result.reasons[0].code, "unsupported_layout")
        self.assertEqual(result.parsed_summary, {})

    def test_non_xml_is_stored_but_not_claimed_to_be_parsed(self):
        settings = Mock(attachment_max_file_bytes=1024)
        result = inspect_upload(upload(b"plain text", "note.txt"),
                                settings=settings, store=FakeAttachmentStore())
        self.assertEqual(result.attachment_status, "unsupported")
        self.assertEqual(result.reasons[0].code, "unsupported_file_type")

    def test_supported_invoice_with_bom_retains_parsed_evidence_but_no_registry_side_effects(self):
        storage = Mock()
        storage.save.side_effect = lambda data, result: result.model_copy(update={"attachment_id": "a" * 32})
        data = b"\xef\xbb\xbf" + xml()
        result = inspect_upload(upload(data), settings=settings(), store=storage)
        self.assertEqual(result.attachment_status, "parsed")
        self.assertEqual(result.invoice.amount_total, 1060)
        self.assertEqual(result.invoice.evidence["amount_total"].file_hash, sha256(data).hexdigest())
        self.assertEqual(result.file_hash, sha256(data).hexdigest())
        storage.register.assert_not_called()
        self.assertEqual(storage.save.call_args.args[0], data)

    def test_invalid_and_unsafe_xml_get_ids_and_specific_status_without_content_in_metadata(self):
        for data, reason in [(b"<EInvoice>", "invalid_xml"),
                             (b'<!DOCTYPE x [<!ENTITY x "test">]><x/>', "unsafe_xml")]:
            with self.subTest(reason=reason):
                result = inspect_upload(upload(data), settings=settings(), store=FakeAttachmentStore())
                self.assertEqual(result.reasons[0].code, reason)
                self.assertIsNotNone(result.attachment_id)
                self.assertIsNone(result.invoice)

    def test_bad_encoding_and_size_fail_before_storage(self):
        settings = Mock(attachment_max_file_bytes=3)
        with self.assertRaises(ValueError):
            inspect_upload(AttachmentUpload(filename="x.xml", content_base64="%%%"),
                           settings=settings, store=Mock())
        with self.assertRaises(ValueError):
            inspect_upload(upload(b"1234"), settings=settings, store=Mock())

    def test_chat_body_validates_ids_and_disallows_them_on_resume(self):
        body = ChatBody(request_id=uuid4().hex, message="核对", attachment_ids=["a" * 32])
        self.assertEqual(body.attachment_ids, ["a" * 32])
        with self.assertRaises(ValidationError):
            ChatBody(request_id=uuid4().hex, message="核对", attachment_ids=["bad"])
        with self.assertRaises(ValidationError):
            ChatBody(request_id=uuid4().hex, resume=True, revision="v1",
                     attachment_ids=["a" * 32])

    def test_empty_attachment_list_keeps_old_request_digest(self):
        old = ChatBody(request_id=uuid4().hex, message="hello")
        explicit_empty = ChatBody(request_id=uuid4().hex, message="hello", attachment_ids=[])
        self.assertEqual(request_digest(old), request_digest(explicit_empty))
        self.assertEqual(request_digest(old), sha256(json.dumps({"message": "hello"},
            sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest())
        self.assertNotEqual(request_digest(old), request_digest(ChatBody(
            request_id=uuid4().hex, message="hello", attachment_ids=["a" * 32])))

    def test_snapshot_keeps_safe_attachment_metadata_without_changing_user_text(self):
        checkpoint = state([HumanMessage(content="请核对", additional_kwargs={
            "attachments": [{"attachment_id": "a" * 32, "filename": "x.xml",
                             "attachment_kind": "xml_other", "attachment_status": "unsupported",
                             "original": "secret", "parsed_summary": {"private": "value"}}]
        })])
        result = chat_snapshot(checkpoint)
        self.assertEqual(result["messages"][0]["text"], "请核对")
        self.assertEqual(result["messages"][0]["attachments"][0]["filename"], "x.xml")
        self.assertNotIn("secret", str(result))
        self.assertNotIn("private", str(result))


class AttachmentStoreTests(unittest.TestCase):
    def test_detail_query_requires_both_company_and_user(self):
        conn = Mock()
        original = AttachmentInspection(attachment_id="a" * 32, filename="owner.xml", byte_size=3)
        conn.execute.return_value.fetchone.return_value = {"data": original.model_dump(mode="json")}
        store = PostgresAttachmentStore(settings(), MemoryIdentity("alice"))
        with patch("business_travel.attachment_store.memory_connection", return_value=nullcontext(conn)):
            self.assertEqual(store.get("a" * 32), original)
        query, params = conn.execute.call_args.args
        self.assertIn("user_id=%s AND attachment_id=%s", query)
        self.assertEqual(params, ("alice", "a" * 32))

    def test_save_persists_original_bytes_hash_and_private_parsed_result(self):
        data = b"\xef\xbb\xbf" + xml()
        parsed = inspect_upload(upload(data), settings=settings(), store=FakeAttachmentStore())
        conn = Mock()
        conn.transaction.return_value = nullcontext()
        conn.execute.side_effect = [Mock(fetchone=lambda: None), Mock(),
            Mock(fetchone=lambda: {"data": parsed.model_dump(mode="json")})]
        with patch("business_travel.attachment_store.memory_connection", return_value=nullcontext(conn)):
            PostgresAttachmentStore(settings(), MemoryIdentity("alice")).save(data, parsed)
        query, params = conn.execute.call_args_list[1].args
        self.assertIn("INSERT INTO attachments", query)
        self.assertEqual(params[:1], ("alice",))
        self.assertEqual(params[3], sha256(data).hexdigest())
        self.assertEqual(params[-1], data)
        self.assertEqual(params[-2].obj["invoice"]["amount_total"], "1060.00")

    def test_load_queries_both_company_and_user_and_preserves_requested_order(self):
        conn = Mock()
        ids = ["a" * 32, "b" * 32]
        rows = [{"attachment_id": item, "filename": item + ".xml", "attachment_kind": "xml_other",
                 "attachment_status": "unsupported"} for item in reversed(ids)]
        conn.execute.return_value.fetchall.return_value = rows
        store = PostgresAttachmentStore(settings(), MemoryIdentity("alice"))
        with patch("business_travel.attachment_store.memory_connection", return_value=nullcontext(conn)):
            result = store.load_for_chat(ids)
        query, params = conn.execute.call_args.args
        self.assertIn("user_id=%s", query)
        self.assertEqual(params, ("alice", ids))
        self.assertEqual([item["attachment_id"] for item in result], ids)
        self.assertNotIn("original", query)

    def test_missing_or_foreign_id_is_rejected_not_silently_dropped(self):
        conn = Mock()
        conn.execute.return_value.fetchall.return_value = []
        store = PostgresAttachmentStore(settings(), MemoryIdentity("alice"))
        with patch("business_travel.attachment_store.memory_connection", return_value=nullcontext(conn)):
            with self.assertRaises(LookupError):
                store.load_for_chat(["a" * 32])

    def test_save_concurrent_duplicate_reloads_winning_row(self):
        conn = Mock()
        conn.transaction.return_value = nullcontext()
        original = AttachmentInspection(attachment_id="b" * 32, filename="original.xml",
            file_hash="c" * 64, byte_size=3)
        conn.execute.side_effect = [Mock(fetchone=lambda: None), Mock(),
                                    Mock(fetchone=lambda: {"data": original.model_dump(mode="json")})]
        with patch("business_travel.attachment_store.memory_connection", return_value=nullcontext(conn)):
            result = PostgresAttachmentStore(settings(), MemoryIdentity("alice")).save(b"xml", original)
        self.assertEqual(result.attachment_id, "b" * 32)
        self.assertIn("ON CONFLICT(user_id,file_hash) DO NOTHING", conn.execute.call_args_list[1].args[0])

    def test_setup_is_additive_and_links_are_owner_scoped(self):
        conn = Mock()
        conn.transaction.return_value = nullcontext()
        with patch("business_travel.attachment_store.memory_connection", return_value=nullcontext(conn)):
            setup_attachments(settings())
        queries = " ".join(str(call.args[0]) for call in conn.execute.call_args_list)
        self.assertIn("CREATE TABLE IF NOT EXISTS attachments", queries)
        self.assertIn("FOREIGN KEY(user_id, attachment_id)", queries)
        self.assertIn("REFERENCES chat_runs", queries)
        self.assertNotIn("DROP", queries)


class AttachmentHttpTests(unittest.TestCase):
    def setUp(self):
        chat_fixtures.ChatTests.setUp(self)
        self.attachments = Mock()
        self.factory = Mock(return_value=self.attachments)
        self.client.close()
        self.client = TestClient(create_app(settings(), AuthSettings(), self.auth,
                                attachment_store_factory=self.factory), base_url="http://127.0.0.1:8000")
        self.addCleanup(self.client.close)
        self.origin = {"Origin": "http://127.0.0.1:8000"}
        self.metadata = [{"attachment_id": "a" * 32, "filename": "test.xml",
            "attachment_kind": "invoice_xml", "attachment_status": "parsed"}]
        self.attachments.load_for_chat.return_value = self.metadata

    post = chat_fixtures.ChatTests.post

    def test_inspect_and_detail_use_session_owner_not_client_input(self):
        self.attachments.save.side_effect = lambda data, value: value.model_copy(update={"attachment_id": "a" * 32})
        response = self.client.post("/api/attachments/inspect", headers=self.origin,
                                    json=upload(xml()).model_dump())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["attachment_status"], "parsed")
        self.assertEqual(self.factory.call_args.args[1], MemoryIdentity("verified"))
        self.attachments.get.return_value = AttachmentInspection.model_validate(response.json())
        detail = self.client.get("/api/attachments/" + "a" * 32)
        self.assertEqual(detail.json()["file_hash"], response.json()["file_hash"])
        self.attachments.register.assert_not_called()
        forged = upload(xml()).model_dump() | {"user_id": "victim"}
        self.assertEqual(self.client.post("/api/attachments/inspect", headers=self.origin, json=forged).status_code, 422)

    def test_unauthenticated_inspect_and_detail_are_rejected(self):
        self.auth.current_user.return_value = None
        self.assertEqual(self.client.post("/api/attachments/inspect", headers=self.origin,
                                         json=upload(xml()).model_dump()).status_code, 401)
        self.assertEqual(self.client.get("/api/attachments/" + "a" * 32).status_code, 401)
        self.attachments.save.assert_not_called()

    def test_foreign_attachment_blocks_all_chat_entrypoints_before_execution(self):
        self.attachments.load_for_chat.side_effect = LookupError("private owner must not leak")
        for path in ["/api/chat", "/api/chat/stream", "/api/conversations/web/chat", "/api/conversations/web/chat/stream"]:
            response = self.client.post(path, headers=self.origin, json={"request_id": uuid4().hex,
                "message": "核对", "attachment_ids": ["a" * 32]})
            self.assertEqual(response.status_code, 404, response.text)
            self.assertNotIn("private owner", response.text)
        self.graph.stream.assert_not_called()
        self.runs.start.assert_not_called()

    def test_message_keeps_attachment_metadata_and_followup_does_not_auto_attach_it(self):
        self.assertEqual(self.post({"message": "核对", "attachment_ids": ["a" * 32]}).status_code, 200)
        message = self.graph.stream.call_args.args[0]["messages"][0]
        self.assertEqual(message.content, "核对")
        self.assertEqual(message.additional_kwargs["attachments"], self.metadata)
        self.attachments.link_message.assert_called_once()
        self.assertEqual(self.post({"message": "补日期"}).status_code, 200)
        message = self.graph.stream.call_args.args[0]["messages"][0]
        self.assertEqual(message.additional_kwargs["attachments"], [])
        self.attachments.link_message.assert_called_once()

    def test_storage_errors_are_redacted_and_config_is_authenticated(self):
        self.assertEqual(self.client.get("/api/attachments/config").json()["max_files"], 10)
        self.attachments.save.side_effect = RuntimeError("secret-connection-token")
        response = self.client.post("/api/attachments/inspect", headers=self.origin,
                                   json=upload(xml()).model_dump())
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("secret-connection", response.text)
        self.attachments.get.side_effect = LookupError("belongs to alice")
        self.assertEqual(self.client.get("/api/attachments/" + "a" * 32).status_code, 404)

    def test_attachment_ids_are_in_request_hash_and_replay_does_not_link_or_execute_again(self):
        request_id = uuid4().hex
        body = ChatBody(request_id=request_id, message="核对", attachment_ids=["a" * 32])
        self.runs.get.return_value = {"id": request_id, "status": "completed", "stage": "assistant",
            "checkpoint_id": "v1", "request_hash": request_digest(body)}
        response = self.client.post("/api/chat", headers=self.origin, json=body.model_dump(exclude_none=True))
        self.assertEqual(response.status_code, 200)
        self.graph.stream.assert_not_called()
        self.attachments.link_message.assert_not_called()
        changed = body.model_copy(update={"attachment_ids": ["b" * 32]})
        self.assertEqual(self.client.post("/api/chat", headers=self.origin,
            json=changed.model_dump(exclude_none=True)).status_code, 409)

    def test_deployment_mismatch_and_count_limit_block_before_execution(self):
        self.auth.current_user.return_value = {"user_id": "verified", "username": "alice"}
        self.graph.stream.assert_not_called()
        self.auth.current_user.return_value = {"user_id": "verified", "username": "alice"}
        ids = [format(index, "032x") for index in range(11)]
        self.assertEqual(self.post({"message": "核对", "attachment_ids": ids}).status_code, 422)

    def test_duplicate_ids_and_large_upload_are_rejected_and_do_not_echo_content(self):
        self.assertEqual(self.post({"message": "核对", "attachment_ids": ["a" * 32, "a" * 32]}).status_code, 422)
        payload = {"filename": "x.xml", "content_base64": "A" * (settings().attachment_request_bytes + 1)}
        response = self.client.post("/api/attachments/inspect", headers=self.origin, json=payload)
        self.assertEqual(response.status_code, 413)
        self.assertLess(len(response.content), 200)
        self.attachments.save.assert_not_called()

    def test_bad_upload_encoding_returns_422_not_raw_input_and_does_not_save(self):
        response = self.client.post("/api/attachments/inspect", headers=self.origin,
                                   json={"filename": "x.xml", "content_base64": "secret%%%"})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("secret", response.text)
        self.attachments.save.assert_not_called()

    def test_attachment_lookup_outage_blocks_before_run_start_and_model_call(self):
        self.attachments.load_for_chat.side_effect = RuntimeError("private-db-token")
        response = self.post({"message": "核对", "attachment_ids": ["a" * 32]})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private-db-token", response.text)
        self.runs.start.assert_not_called()
        self.graph.stream.assert_not_called()

    def test_attachment_link_failure_marks_run_failed_without_model_call(self):
        self.attachments.link_message.side_effect = RuntimeError("private-db-token")
        response = self.post({"message": "核对", "attachment_ids": ["a" * 32]})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private-db-token", response.text)
        self.runs.start.assert_called_once()
        self.assertEqual(self.runs.finish.call_args.args[1], "failed")
        self.graph.stream.assert_not_called()

    def test_plain_message_never_opens_attachment_storage(self):
        self.assertEqual(self.post({"message": "只发文字"}).status_code, 200)
        self.factory.assert_not_called()
        self.graph.stream.assert_called_once()

    def test_attachment_config_requires_authentication(self):
        self.auth.current_user.return_value = None
        self.assertEqual(self.client.get("/api/attachments/config").status_code, 401)


class AttachmentSettingsTests(unittest.TestCase):
    def test_attachment_limits_validate_deployment_configuration(self):
        for value in (0, 21):
            with self.assertRaisesRegex(ValueError, "TRAVEL_ATTACHMENT_MAX_FILES"):
                replace(settings(), attachment_max_files=value).validate_invoices()
        for value in (1023, 5 * 1024 * 1024 + 1):
            with self.assertRaisesRegex(ValueError, "TRAVEL_ATTACHMENT_MAX_FILE_BYTES"):
                replace(settings(), attachment_max_file_bytes=value).validate_invoices()


if __name__ == "__main__":
    unittest.main()

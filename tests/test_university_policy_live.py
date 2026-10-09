"""Opt-in real PostgreSQL tests in an isolated random schema; synthetic vectors."""

import os
import json
import unittest
import uuid
from dataclasses import replace
from datetime import date
from unittest.mock import Mock

import psycopg
from psycopg import sql

from business_travel.policy_lodging import LodgingRow
from business_travel.policy_package import prepare_package
from business_travel.policy_pg import PostgresPolicyStore, PolicyError
from business_travel.policy_rules import ExpenseFacts
from business_travel.policy_rag import explain_policy
from business_travel.contracts import Candidate, HotelRequest, TransportRequest
from business_travel.settings import Settings
from test_university_policy import MANIFEST
from test_policy_hybrid import FakeModel, tool_response


@unittest.skipUnless(os.environ.get("TRAVEL_TEST_POSTGRES") == "1", "PostgreSQL integration is opt-in")
class UniversityPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = replace(Settings.from_env(), policy_key="hfut_test", policy_company_id="school_test",
                               embedding_model="synthetic", embedding_dimensions=3)
        if cls.settings.missing_policy_config(embedding=False):
            raise RuntimeError("Policy database is not configured")
        cls.schema = "university_policy_test_" + uuid.uuid4().hex
        with psycopg.connect(cls.settings.policy_database_url) as conn:
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(cls.schema)))
        cls.addClassCleanup(cls.cleanup_schema)

        def connect(dsn, **kwargs):
            conn = psycopg.connect(dsn, **kwargs)
            conn.execute(sql.SQL("SET search_path TO {},public").format(sql.Identifier(cls.schema)))
            conn.commit()
            return conn

        cls.connect = staticmethod(connect)
        cls.embedder = Mock(space="synthetic-test-space")
        cls.embedder.embed.side_effect = lambda texts: [[1.0, 0.0, 0.0] for _ in texts]
        cls.store = PostgresPolicyStore(cls.settings, connect=connect, embedder=cls.embedder)
        cls.store.initialize()
        bundle = prepare_package(MANIFEST, company_id="school_test")
        bundle.policy_key = "hfut_test"
        cls.bundle = bundle
        cls.staged = cls.store.stage(bundle)
        cls.store.publish(cls.staged["document_id"], bundle.digest())

    @classmethod
    def cleanup_schema(cls):
        if not cls.schema.startswith("university_policy_test_") or len(cls.schema) != 55:
            raise AssertionError("Refusing to drop an unexpected schema")
        with psycopg.connect(cls.settings.policy_database_url) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(cls.schema)))

    def facts(self, **changes):
        return ExpenseFacts.model_validate(dict(
            kind="hotel", expense_date="2026-09-16", destination="青岛", destination_province="山东",
            mainland_business_trip=True, lodging_standard="其他人员", amount="451", currency="CNY",
            basis="person_day", taxes_confirmed=True, date_verified=True, **changes))

    def test_source_and_lodging_counts_and_initialization_idempotent(self):
        self.store.initialize()
        with self.store.connection() as conn:
            for table, count in (("policy_sources", 2), ("policy_lodging_limits", 86), ("policy_chunks", 137)):
                actual = conn.execute(sql.SQL("SELECT count(*) AS n FROM {} WHERE document_id=%s")
                                      .format(sql.Identifier(table)), (self.staged["document_id"],)).fetchone()
                self.assertEqual(actual["n"], count)

    def test_restage_same_bundle_unchanged(self):
        self.assertEqual(self.store.stage(self.bundle)["status"], "unchanged")

    def test_check_reads_season_price_and_both_file_sources(self):
        result = self.store.check(self.facts())
        self.assertEqual(result["status"], "over_standard")
        self.assertEqual(float(result["cap"]), 450)
        self.assertEqual({s["source"] for s in result["sources"]},
                         {s.source for s in self.bundle.sources})
        self.assertEqual(len({s["source_sha256"] for s in result["sources"]}), 1)
        self.assertEqual(len({s["file_sha256"] for s in result["sources"]}), 2)
        table = next(s for s in result["sources"] if s["sheet"])
        self.assertEqual(table["sheet"], "Sheet1")
        self.assertEqual(table["cell_range"], "B44:K44")

    def test_exact_rows_supplement_retrieval_even_if_vector_rank_misses_city(self):
        result = self.store.search_for_facts("住宿标准", self.facts().model_dump(mode="json"))
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["context_incomplete"])
        self.assertEqual(result["sources"][0]["chunk"], "lodging-r44")
        self.assertEqual(len({s["source_sha256"] for s in result["sources"]}), 1)
        self.assertTrue(any(s["clause"] == "第十二条" for s in result["sources"]))
        self.assertEqual(result["lodging_lookup"]["row_ids"], ["lodging-r44"])
        self.assertEqual(float(result["lodging_lookup"]["cap"]), 450)
        self.assertEqual(result["lodging_lookup"]["date"], "2026-09-16")
        changed = self.store.search_for_facts("住宿标准",
            self.facts().model_copy(update={"lodging_standard": "其他人员档",
                                           "expense_date": date(2026, 10, 1)}).model_dump(mode="json"))
        self.assertEqual(float(changed["lodging_lookup"]["cap"]), 380)
        self.assertFalse(changed["lodging_lookup"]["seasonal"])

    def test_business_date_does_not_override_administrators_current_policy(self):
        for day in (date(2025, 12, 31), date(2027, 1, 1)):
            result = self.store.check(self.facts().model_copy(update={"expense_date": day}))
            self.assertEqual(result["status"], "over_standard")
            self.assertEqual(float(result["cap"]), 380)
            self.assertEqual(result["policy_version"], self.bundle.version)

    def test_undated_stay_keeps_exact_rows_but_cannot_select_season(self):
        facts = self.facts().model_dump(mode="json")
        facts.pop("expense_date")
        result = self.store.search_for_facts("住宿标准", facts)
        self.assertEqual(result["lodging_lookup"]["missing_fields"], ["expense_date"])
        self.assertNotIn("cap", result["lodging_lookup"])
        self.assertEqual(result["sources"][0]["chunk"], "lodging-r44")
        self.assertNotIn("as_of", result)

    def test_published_lookup_drives_clarification_without_policy_models(self):
        cases = [
            ({"kind": "hotel"}, ["destination", "lodging_standard"]),
            ({"kind": "hotel", "destination": "三亚", "destination_province": "海南"},
             ["lodging_standard"]),
            ({"kind": "hotel", "destination": "三亚", "destination_province": "海南",
              "lodging_standard": "其他人员"}, ["expense_date"]),
            ({"kind": "hotel", "destination": "洛阳", "destination_province": "河南",
              "lodging_standard": "其他人员"}, ["expense_date"]),
        ]
        for facts, expected in cases:
            with self.subTest(facts=facts):
                reference_only = expected == ["lodging_standard"]
                if reference_only:
                    def draft(messages):
                        payload = json.loads(messages[-1].content)
                        evidence = payload["sources"][0]
                        return tool_response("PolicyAnswer", {
                            "decision": "partial",
                            "claims": [{"text": evidence["text"],
                                        "citations": [{"source_id": evidence["source_id"]}]}],
                            "limitations": [{
                                "topic": "个人住宿档次", "reason": "applicability_unknown",
                                "detail": "尚不能确定个人适用档次。", "next_step": "请向学校财务核实档次。",
                            }],
                        })
                    models = {
                        "policy": FakeModel([draft]),
                        "policy_reviewer": FakeModel([
                            tool_response("PolicyAnswerReview", {"verdict": "accept"})]),
                    }
                    factory = Mock(side_effect=lambda role: models[role])
                else:
                    factory = Mock(side_effect=AssertionError("Policy model must not be constructed"))
                data = explain_policy(self.store, factory, self.settings, "请核对个人住宿标准",
                    facts=facts, policy_query={"category": "hotel", "mode": "personal_limit"})
                if reference_only:
                    self.assertEqual(data["rag"]["status"], "partial")
                    self.assertTrue(data["requirements"]["reference_only"])
                    self.assertEqual(data["requirements"]["missing_fields"], expected)
                    self.assertTrue(data["rag"]["proposal"]["claims"])
                    self.assertEqual(len(models["policy_reviewer"].messages), 1)
                else:
                    self.assertEqual(data["rag"]["status"], "clarify")
                    self.assertEqual(data["rag"]["proposal"]["requested_fields"], expected)
                    self.assertEqual(data["rag"]["proposal"]["claims"], [])
                    factory.assert_not_called()
                self.assertTrue(data["sources"])
                self.assertEqual({s["document_id"] for s in data["sources"]},
                                 {self.staged["document_id"]})

    def test_another_organization_and_policy_cannot_read_package(self):
        for changes in ({"policy_company_id": "another_school"}, {"policy_key": "company_old"}):
            other = PostgresPolicyStore(replace(self.settings, **changes), connect=self.connect,
                                        embedder=self.embedder)
            self.assertEqual(other.check(self.facts())["reason"], "no_published_policy")
            self.assertEqual(other.search("标准")["sources"], [])

    def test_latest_publication_wins_without_selecting_draft_or_other_scope(self):
        with self.store.connection() as conn:
            # Roll back all synthetic versions even if an assertion fails.
            with conn.transaction(force_rollback=True):
                for version, company, key, status in (
                    ("new-published", "school_test", "hfut_test", "published"),
                    ("new-draft", "school_test", "hfut_test", "draft"),
                    ("other-company", "other_school", "hfut_test", "published"),
                    ("other-key", "school_test", "other_key", "published"),
                ):
                    row = conn.execute(
                        "INSERT INTO policy_documents "
                        "(company_id,policy_key,version,effective_from,effective_to,status,source,"
                        "source_sha256,bundle_sha256,embedding_space,embedding_model,embedding_dimensions,"
                        "tokenizer,published_at) "
                        "SELECT %s,%s,%s,'2000-01-01','2000-12-31',%s,source,source_sha256,bundle_sha256,"
                        "embedding_space,embedding_model,embedding_dimensions,tokenizer,now() "
                        "FROM policy_documents WHERE id=%s RETURNING id",
                        (company, key, version, status, self.staged["document_id"])).fetchone()
                    if version == "new-published":
                        expected = row["id"]
                active = self.store._active(conn)
                self.assertEqual(active["id"], expected)
                self.assertEqual(active["version"], "new-published")

    def test_mutated_bundle_rejected_before_embedding(self):
        other = self.bundle.model_copy(deep=True)
        other.lodging_rows[0]["caps"]["其他人员"] = "1"
        previous = self.embedder.embed.call_count
        with self.assertRaises(ValueError):
            self.store.stage(other)
        self.assertEqual(self.embedder.embed.call_count, previous)

    def test_rule_and_attachment_share_document_fk(self):
        with self.store.connection() as conn:
            rows = conn.execute("SELECT l.data,s.source_sha256 FROM policy_lodging_limits l "
                                "JOIN policy_sources s ON s.document_id=l.document_id AND s.source_id=l.source_id "
                                "WHERE l.document_id=%s", (self.staged["document_id"],)).fetchall()
            self.assertEqual(len(rows), 86)
            self.assertTrue(all(LodgingRow.model_validate(r["data"]) for r in rows))

    def test_planning_candidate_preserves_school_identity_and_quote_uncertainty(self):
        trip = HotelRequest(query_scope="hotel", destination="青岛", destination_province="山东",
                            check_in="2026-09-16", check_out="2026-09-17",
                            mainland_business_trip=True, personnel_type="staff",
                            lodging_standard="其他人员", room_occupants=1)
        candidate = Candidate(id="school-hotel", kind="hotel", label="测试住宿",
                              source="fixture", observed_at="2026-09-16", request_date="2026-09-16",
                              date_verified=True, quote={"amount": "451", "currency": "CNY",
                                                         "basis": "night", "taxes_confirmed": True})
        result = self.store.check_candidate(candidate, trip)
        self.assertEqual(result["status"], "over_standard")
        self.assertEqual(float(result["cap"]), 450)
        self.assertEqual(result["policy_profile"], "university")
        result = self.store.check_candidate(candidate.model_copy(update={"date_verified": False}), trip)
        self.assertEqual(result["status"], "insufficient_information")
        self.assertFalse(result["approved"])

    def test_school_transport_candidate_does_not_require_company_grade(self):
        trip = TransportRequest(query_scope="transport", journey="one_way", origin="合肥",
                            destination="南京", departure_date="2026-09-16",
                            mainland_business_trip=True, technical_grade=5)
        candidate = Candidate(id="school-train", kind="train", label="测试列车",
                              source="fixture", observed_at="2026-09-16", request_date="2026-09-16",
                              transport_mode="high_speed_train", seat_class="一等座")
        result = self.store.check_candidate(candidate, trip)
        self.assertEqual(result["status"], "within_checked_rules")
        self.assertEqual(result["transport_standard"], "二类")
        self.assertFalse(result["approved"])


if __name__ == "__main__":
    unittest.main()

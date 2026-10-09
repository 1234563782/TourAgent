"""Scoped PostgreSQL policy repository, exact vectors and Chinese lexical RRF."""

import hashlib
import json
import logging
import math
import re
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .policy_documents import PolicyBundle
from .policy_rules import ExpenseFacts, ReviewedRules, check_expense
from .policy_lodging import LodgingRow, lookup_lodging
from .university_policy import UniversityRules, check_university_expense, load_reviewed_rules


class PolicyError(ValueError):
    """Only fixed, non-sensitive error codes cross the tool boundary."""


def validate_vector(value, dimensions):
    if not isinstance(value, list) or len(value) != dimensions:
        raise PolicyError("embedding_dimension_mismatch")
    if any(type(v) not in (float, int) or not math.isfinite(v) for v in value):
        raise PolicyError("embedding_invalid_values")
    if not any(value):
        raise PolicyError("embedding_zero_vector")
    return [float(v) for v in value]


def vector_literal(value):
    return "[" + ",".join(str(v) for v in value) + "]"


class EmbeddingClient:
    def __init__(self, settings, *, transport=None):
        self.settings = settings
        self.transport = transport

    @property
    def space(self):
        s = self.settings
        return hashlib.sha256(json.dumps(
            [s.embedding_base_url.rstrip("/"), s.embedding_model, s.embedding_dimensions, s.embedding_revision]
        ).encode()).hexdigest()

    def embed(self, texts):
        s = self.settings
        if not all((s.embedding_base_url, s.embedding_api_key, s.embedding_model, s.embedding_dimensions)):
            raise PolicyError("embedding_configuration_required")
        endpoint = urlsplit(s.embedding_base_url)
        if (endpoint.scheme != "https" or not endpoint.hostname or endpoint.username or endpoint.password
                or endpoint.query or endpoint.fragment):
            raise PolicyError("embedding_requires_trusted_https_base_url")
        if not texts or len(texts) > 64 or any(not text or len(text) > 30000 for text in texts):
            raise PolicyError("embedding_input_limit")
        try:
            with httpx.Client(timeout=s.timeout, trust_env=False, follow_redirects=False,
                              transport=self.transport) as client:
                response = client.post(
                    s.embedding_base_url.rstrip("/") + "/embeddings",
                    headers={"Authorization": f"Bearer {s.embedding_api_key}"},
                    json={"model": s.embedding_model, "input": texts, "encoding_format": "float"})
                if response.status_code != 200:
                    raise PolicyError("embedding_request_rejected")
                if len(response.content) > 16 * 1024 * 1024:
                    raise PolicyError("embedding_response_too_large")
                data = response.json()["data"]
                if not isinstance(data, list) or len(data) != len(texts):
                    raise PolicyError("embedding_response_count_mismatch")
                by_index = {}
                for item in data:
                    index = item["index"]
                    if type(index) is not int or index in by_index or not 0 <= index < len(texts):
                        raise PolicyError("embedding_invalid_indices")
                    by_index[index] = validate_vector(item["embedding"], s.embedding_dimensions)
                return [by_index[index] for index in range(len(texts))]
        except PolicyError:
            raise
        except Exception:
            raise PolicyError("embedding_request_failed_check_configuration") from None


class ChineseTokenizer:
    def __init__(self, dictionary_path=None):
        import jieba
        jieba.setLogLevel(logging.ERROR)
        self.tokenizer = jieba.Tokenizer()
        self.signature = "jieba-0.42.1-search"
        if dictionary_path:
            if not dictionary_path.is_file():
                raise PolicyError("policy_dictionary_missing")
            self.tokenizer.load_userdict(str(dictionary_path))
            self.signature += ":" + hashlib.sha256(dictionary_path.read_bytes()).hexdigest()

    def tokenize(self, text):
        tokens = self.tokenizer.cut_for_search(text.lower())
        return " ".join(dict.fromkeys(token for token in tokens
                                     if re.fullmatch(r"[a-z0-9_\u4e00-\u9fff]+", token)))


def reciprocal_rank_fusion(vector_ids, keyword_ids, *, k=60, limit=5):
    scores = {}
    for ids in (vector_ids, keyword_ids):
        for rank, key in enumerate(dict.fromkeys(ids), start=1):
            scores[key] = scores.get(key, 0) + 1 / (k + rank)
    return sorted(scores, key=lambda key: (-scores[key], key))[:limit]


class PostgresPolicyStore:
    rules = None

    def __init__(self, settings, *, connect=None, embedder=None, tokenizer=None):
        self.settings = settings
        self._connect = connect
        self.embedder = embedder or EmbeddingClient(settings)
        self.tokenizer = tokenizer or ChineseTokenizer(settings.policy_dictionary_path)

    @contextmanager
    def connection(self):
        s = self.settings
        if s.missing_policy_config(embedding=False):
            raise PolicyError("policy_database_configuration_required")
        if (not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", s.policy_company_id)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", s.policy_key)):
            raise PolicyError("invalid_policy_scope")
        try:
            from .db_pool import pooled_connection
            context = (self._connect(s.policy_database_url, connect_timeout=max(1, math.ceil(s.timeout)),
                                    row_factory=dict_row) if self._connect else
                       pooled_connection(s, s.policy_database_url))
            with context as conn, conn.transaction():
                if not self._connect:
                    conn.execute("SET LOCAL search_path TO public")
                conn.execute("SELECT set_config('statement_timeout', %s, true)", (str(int(s.timeout * 1000)),))
                yield conn
        except PolicyError:
            raise
        except Exception:
            raise PolicyError("policy_database_operation_failed_check_service_schema_permissions") from None

    def initialize(self):
        schema = (Path(__file__).parent / "sql" / "policy_schema.sql").read_text(encoding="utf-8")
        with self.connection() as conn:
            conn.execute(schema)
        return {"status": "ok", "schema_version": 1}

    def stage(self, bundle: PolicyBundle):
        bundle = PolicyBundle.model_validate(bundle.model_dump(mode="json"))
        s = self.settings
        if (bundle.company_id, bundle.policy_key) != (s.policy_company_id, s.policy_key):
            raise PolicyError("bundle_scope_mismatch")
        if bundle.rules:
            reviewed = load_reviewed_rules(bundle.rules)
            if isinstance(reviewed, UniversityRules) and (bundle.schema_version != 2 or not bundle.lodging_rows):
                raise PolicyError("university_policy_package_required")
            clauses = {chunk.clause for chunk in bundle.chunks if chunk.clause}
            if any(clause not in clauses for group in reviewed.source_clauses.values() for clause in group):
                raise PolicyError("rule_source_clause_missing")
        digest = bundle.digest()
        with self.connection() as conn:
            existing = conn.execute(
                "SELECT id, bundle_sha256, embedding_space, tokenizer FROM policy_documents "
                "WHERE company_id=%s AND policy_key=%s AND version=%s",
                (s.policy_company_id, s.policy_key, bundle.version)).fetchone()
            if existing:
                if (existing["bundle_sha256"] != digest or existing["embedding_space"] != self.embedder.space
                        or existing["tokenizer"] != self.tokenizer.signature):
                    raise PolicyError("version_exists_with_different_content_or_embedding_create_new_version")
                return {"status": "unchanged", "document_id": existing["id"], "bundle_sha256": digest}
        vectors = []
        texts = [f"{chunk.section}\n{chunk.text}" for chunk in bundle.chunks]
        batch_size = s.embedding_batch_size
        if type(batch_size) is not int or not 1 <= batch_size <= 64:
            raise PolicyError("embedding_invalid_batch_size")
        for start in range(0, len(texts), batch_size):
            vectors.extend(self.embedder.embed(texts[start:start + batch_size]))
        with self.connection() as conn:
            row = conn.execute(
                "INSERT INTO policy_documents (company_id,policy_key,version,effective_from,effective_to,"
                "source,source_sha256,bundle_sha256,embedding_space,embedding_model,embedding_dimensions,"
                "tokenizer,warnings) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (s.policy_company_id, s.policy_key, bundle.version, bundle.effective_from, bundle.effective_to,
                 bundle.source, bundle.source_sha256, digest, self.embedder.space, s.embedding_model,
                 s.embedding_dimensions, self.tokenizer.signature, Jsonb(bundle.warnings))).fetchone()
            document_id = row["id"]
            for chunk, vector in zip(bundle.chunks, vectors, strict=True):
                vector = validate_vector(vector, s.embedding_dimensions)
                related = list(chunk.related_ids)
                if bundle.rules and bundle.schema_version == 1:
                    for family, clauses in reviewed.source_clauses.items():
                        if family != "scope" and chunk.clause in clauses:
                            target_clauses = set(clauses + reviewed.source_clauses["scope"])
                            related.extend(c.id for c in bundle.chunks
                                           if c.clause in target_clauses and c.id != chunk.id)
                related = list(dict.fromkeys(related))
                conn.execute(
                    "INSERT INTO policy_chunks (document_id,chunk_id,section,clause,content,table_data,related_ids,"
                    "embedding,embedding_dimensions,keywords) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s::vector,%s,to_tsvector('simple',%s))",
                    (document_id, chunk.id, chunk.section, chunk.clause, chunk.text, Jsonb(chunk.table),
                     related, vector_literal(vector), s.embedding_dimensions,
                     self.tokenizer.tokenize(f"{chunk.section} {chunk.text}")))
            if bundle.rules:
                conn.execute("INSERT INTO policy_rules (document_id,rules) VALUES (%s,%s)",
                             (document_id, Jsonb(bundle.rules)))
            for source in bundle.sources:
                conn.execute(
                    "INSERT INTO policy_sources (document_id,source_id,source,source_sha256,role,adoption_note) "
                    "VALUES (%s,%s,%s,%s,%s,%s)",
                    (document_id, source.id, source.source, source.sha256, source.role, bundle.adoption_note))
            for limit in bundle.lodging_rows:
                conn.execute(
                    "INSERT INTO policy_lodging_limits (document_id,row_id,source_id,data) VALUES (%s,%s,%s,%s)",
                    (document_id, limit["id"], limit["source_id"], Jsonb(limit)))
        return {"status": "draft", "document_id": document_id, "bundle_sha256": digest,
                "chunks": len(bundle.chunks), "warnings": bundle.warnings}

    def publish(self, document_id, reviewed_sha256, *, supersede=False):
        s = self.settings
        with self.connection() as conn:
            # Serialize version publication for one company/policy, including concurrent CLI invocations.
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                         (f"{s.policy_company_id}:{s.policy_key}",))
            doc = conn.execute(
                "SELECT * FROM policy_documents WHERE id=%s AND company_id=%s AND policy_key=%s FOR UPDATE",
                (document_id, s.policy_company_id, s.policy_key)).fetchone()
            if not doc or doc["bundle_sha256"] != reviewed_sha256:
                raise PolicyError("review_digest_or_document_mismatch")
            if doc["status"] == "published":
                return {"status": "unchanged", "document_id": document_id}
            overlap = conn.execute(
                "SELECT id,effective_from FROM policy_documents WHERE company_id=%s AND policy_key=%s "
                "AND status='published' AND daterange(effective_from,effective_to,'[]') && "
                "daterange(%s::date,%s::date,'[]')",
                (s.policy_company_id, s.policy_key, doc["effective_from"], doc["effective_to"])).fetchall()
            for old in overlap:
                if not supersede or old["effective_from"] >= doc["effective_from"]:
                    raise PolicyError("published_period_overlap_use_explicit_supersede_for_older_version")
                conn.execute("UPDATE policy_documents SET effective_to=%s WHERE id=%s",
                             (doc["effective_from"] - timedelta(days=1), old["id"]))
            conn.execute("UPDATE policy_documents SET status='published',published_at=now() WHERE id=%s",
                         (document_id,))
        return {"status": "published", "document_id": document_id}

    def _active(self, conn):
        s = self.settings
        rows = conn.execute(
            "SELECT d.*, r.rules->'source_clauses'->'scope' AS scope_clauses, "
            "r.rules->>'profile' AS policy_profile "
            "FROM policy_documents d LEFT JOIN policy_rules r ON r.document_id=d.id "
            "WHERE d.company_id=%s AND d.policy_key=%s AND d.status='published' "
            "ORDER BY d.published_at DESC NULLS LAST, d.id DESC LIMIT 1",
            (s.policy_company_id, s.policy_key)).fetchall()
        if len(rows) > 1:
            raise PolicyError("ambiguous_policy_versions")
        return rows[0] if rows else None

    @staticmethod
    def _source(row, doc):
        provenance = (row.get("table_data") or {}).get("_provenance", {})
        return {"source": provenance.get("source", doc["source"]), "version": doc["version"], "document_id": doc["id"],
                "source_sha256": doc["source_sha256"], "chunk": row["chunk_id"], "clause": row["clause"],
                "section": row["section"], "text": row["content"], "table": row.get("table_data"),
                "file_sha256": provenance.get("sha256", doc["source_sha256"]),
                "sheet": provenance.get("sheet"), "cell_range": provenance.get("cell_range"),
                "adoption_note": provenance.get("adoption_note")}

    def search_for_facts(self, question, facts):
        return self.search(question, _facts=facts)

    def search(self, question, *, _facts=None):
        if not isinstance(question, str) or not question.strip() or len(question) > 4000:
            return {"status": "error", "reason": "invalid_policy_query", "sources": []}
        try:
            with self.connection() as conn:
                doc = self._active(conn)
                if not doc:
                    return {"status": "empty", "reason": "no_published_policy", "sources": []}
                if doc["embedding_space"] != self.embedder.space or doc["tokenizer"] != self.tokenizer.signature:
                    raise PolicyError("policy_embedding_or_tokenizer_changed_reimport_required")
                vector = validate_vector(self.embedder.embed([question])[0], self.settings.embedding_dimensions)
                # Materialize the scoped rows before ranking: no cross-company ANN post-filter loss.
                vector_rows = conn.execute(
                    "WITH scoped AS MATERIALIZED (SELECT * FROM policy_chunks WHERE document_id=%s) "
                    "SELECT chunk_id FROM scoped ORDER BY embedding <=> %s::vector,chunk_id LIMIT %s",
                    (doc["id"], vector_literal(vector), self.settings.policy_recall_k)).fetchall()
                terms = self.tokenizer.tokenize(question).split()
                keyword_rows = []
                if terms:
                    keyword_rows = conn.execute(
                        "SELECT chunk_id FROM policy_chunks WHERE document_id=%s "
                        "AND keywords @@ to_tsquery('simple',%s) "
                        "ORDER BY ts_rank_cd(keywords,to_tsquery('simple',%s)) DESC,chunk_id LIMIT %s",
                        (doc["id"], " | ".join(terms), " | ".join(terms), self.settings.policy_recall_k)).fetchall()
                ids = reciprocal_rank_fusion([r["chunk_id"] for r in vector_rows],
                                             [r["chunk_id"] for r in keyword_rows],
                                             k=self.settings.policy_rrf_k, limit=self.settings.policy_top_k)
                facts = _facts or {}
                exact = None
                if doc.get("policy_profile") == "university" and facts.get("kind") == "hotel":
                    limits = conn.execute("SELECT data FROM policy_lodging_limits WHERE document_id=%s",
                                          (doc["id"],)).fetchall()
                    exact = lookup_lodging([LodgingRow.model_validate(r["data"]) for r in limits],
                                           destination=facts.get("destination"),
                                           province=facts.get("destination_province"),
                                           standard=facts.get("lodging_standard"),
                                           day=date.fromisoformat(facts["expense_date"])
                                           if facts.get("expense_date") else None)
                    if exact.get("row_ids"):
                        ids = list(dict.fromkeys(exact["row_ids"] + ids))
                rows = conn.execute("SELECT * FROM policy_chunks WHERE document_id=%s AND chunk_id=ANY(%s)",
                                    (doc["id"], ids)).fetchall()
                lookup = {row["chunk_id"]: row for row in rows}
                rows = [lookup[key] for key in ids]
                incomplete = len(rows) > self.settings.policy_context_limit
                rows = rows[:self.settings.policy_context_limit]
                # Reviewed scope applies to every hit, including clauses without family links.
                scope_clauses = doc.get("scope_clauses") or []
                if rows and scope_clauses:
                    available = max(0, self.settings.policy_context_limit - len(rows))
                    scope_rows = conn.execute(
                        "SELECT * FROM policy_chunks WHERE document_id=%s AND clause=ANY(%s) "
                        "AND NOT (chunk_id=ANY(%s)) ORDER BY chunk_id LIMIT %s",
                        (doc["id"], scope_clauses, ids, available + 1)).fetchall()
                    incomplete = incomplete or len(scope_rows) > available
                    rows.extend(scope_rows[:available])
                # Follow parent and referenced-clause links to closure, bounded by the
                # same context budget. Cycles and missing rows must never look complete.
                attempted = {row["chunk_id"] for row in rows}
                while True:
                    related = list(dict.fromkeys(key for row in rows for key in row["related_ids"]
                                                 if key not in attempted))
                    if not related:
                        break
                    available = max(0, self.settings.policy_context_limit - len(rows))
                    incomplete = incomplete or len(related) > available
                    if not available:
                        break
                    requested = related[:available]
                    linked = conn.execute(
                        "SELECT * FROM policy_chunks WHERE document_id=%s AND chunk_id=ANY(%s) ORDER BY chunk_id",
                        (doc["id"], requested)).fetchall()
                    incomplete = incomplete or {r["chunk_id"] for r in linked} != set(requested)
                    attempted.update(requested)
                    rows.extend(linked)
                return {"status": "ok" if rows else "empty",
                        "policy_profile": doc.get("policy_profile"),
                        "sources": [self._source(row, doc) for row in rows],
                        "context_incomplete": incomplete,
                        "lodging_lookup": exact,
                        "warning": "Evidence only, not approval. Missing linked clauses require further retrieval."}
        except PolicyError as error:
            return {"status": "unavailable", "reason": str(error), "sources": []}

    def check(self, facts: ExpenseFacts):
        try:
            with self.connection() as conn:
                doc = self._active(conn)
                if not doc:
                    return {"status": "insufficient_information", "reason": "no_published_policy"}
                row = conn.execute("SELECT rules FROM policy_rules WHERE document_id=%s", (doc["id"],)).fetchone()
                if not row:
                    return {"status": "insufficient_information", "reason": "reviewed_rules_unavailable"}
                reviewed = load_reviewed_rules(row["rules"])
                if isinstance(reviewed, UniversityRules):
                    limits = conn.execute("SELECT data FROM policy_lodging_limits WHERE document_id=%s",
                                          (doc["id"],)).fetchall()
                    result = check_university_expense(
                        facts, reviewed, [LodgingRow.model_validate(r["data"]) for r in limits])
                else:
                    result = check_expense(facts, reviewed)
                sources = conn.execute(
                    "SELECT * FROM policy_chunks WHERE document_id=%s AND clause=ANY(%s) ORDER BY chunk_id",
                    (doc["id"], result["source_clauses"])).fetchall()
                if result.get("row_ids"):
                    sources += conn.execute(
                        "SELECT * FROM policy_chunks WHERE document_id=%s AND chunk_id=ANY(%s) ORDER BY chunk_id",
                        (doc["id"], result["row_ids"])).fetchall()
                result.update(policy_version=doc["version"], sources=[self._source(r, doc) for r in sources])
                # No names, locations, user text or API credentials in the audit log.
                conn.execute(
                    "INSERT INTO policy_check_logs (document_id,expense_kind,expense_date,result) VALUES (%s,%s,%s,%s)",
                    (doc["id"], facts.kind, facts.expense_date, Jsonb({
                        key: result[key] for key in ("status", "reason", "source_clauses")})))
                return result
        except PolicyError as error:
            return {"status": "insufficient_information", "reason": str(error)}

    def check_candidate(self, candidate, trip):
        identity = {name: getattr(trip, name, None) for name in (
            "personnel_type", "academic_title", "technical_grade", "management_grade",
            "transport_standard", "lodging_standard", "destination_province", "student_activity_eligible")}
        if candidate.kind != "hotel":
            if not candidate.transport_mode or not candidate.seat_class:
                return {"candidate_id": candidate.id, "status": "insufficient_information",
                        "reason": "confirmed_transport_class_required",
                        "missing_fields": ["transport_mode", "seat_class"], "approved": False}
            facts = ExpenseFacts(
                kind="transport", expense_date=candidate.request_date,
                destination=trip.origin if candidate.details.get("inbound") else trip.destination,
                employee_grade=trip.employee_grade, mainland_business_trip=trip.mainland_business_trip,
                long_assignment_days=trip.long_assignment_days, special_approval=trip.special_approval,
                transport_mode=candidate.transport_mode, seat_class=candidate.seat_class, **identity)
            return {"candidate_id": candidate.id, **self.check(facts)}
        facts = ExpenseFacts(
            kind="hotel", expense_date=candidate.request_date, destination=trip.destination,
            employee_grade=trip.employee_grade, mainland_business_trip=trip.mainland_business_trip,
            long_assignment_days=trip.long_assignment_days, special_approval=trip.special_approval,
            amount=candidate.quote.amount, currency=candidate.quote.currency,
            basis="room_night" if candidate.quote.basis == "night" else "unknown",
            taxes_confirmed=candidate.quote.taxes_confirmed, date_verified=candidate.date_verified,
            occupants=trip.room_occupants, roommate_grade=trip.roommate_grade,
            voluntary_twin_share=trip.voluntary_twin_share, **identity)
        # Search listings do not establish a price for every night of a multi-night stay.
        if trip.check_in and trip.check_out and (trip.check_out - trip.check_in).days > 1:
            return {"candidate_id": candidate.id, "status": "insufficient_information",
                    "reason": "each_night_requires_individual_verified_quote"}
        return {"candidate_id": candidate.id, **self.check(facts)}

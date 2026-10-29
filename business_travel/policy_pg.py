"""Scoped PostgreSQL policy repository, exact vectors and Chinese lexical RRF."""

import hashlib
import json
import logging
import math
import re
from time import perf_counter
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .policy_documents import PolicyBundle
from .policy_rules import ExpenseFacts, ReviewedRules, check_expense
from .policy_lodging import LodgingRow, list_lodging, lookup_lodging
from .policy_hnsw import hnsw_index_name, render_index_ddl, validate_hnsw_parameters
from .policy_rerank import QwenReranker, RerankError
from .reimbursement import estimate_expense, bind_estimate_sources
from .observability import observation
from .university_policy import (UniversityRules, check_university_expense, load_reviewed_rules,
                                planning_standards_for, known_transport_classes,
                                transport_category, lodging_standard_for)


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


def _safe_vector_error_code(error):
    """Keep provider failure metadata to a fixed ASCII error code only."""
    candidate = str(error)
    known = {
        "embedding_configuration_required", "embedding_requires_trusted_https_base_url",
        "embedding_input_limit", "embedding_response_too_large",
        "embedding_response_count_mismatch", "embedding_invalid_indices",
        "embedding_request_failed_check_configuration", "embedding_dimension_mismatch",
        "embedding_invalid_values", "embedding_zero_vector", "invalid_hnsw_configuration",
    }
    if candidate in known or re.fullmatch(
            r"embedding_request_rejected_http_[0-9]{3}(?:_[A-Za-z0-9_.-]{1,64})?", candidate):
        return candidate
    return "embedding_request_failed_check_configuration"


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
                    json={"model": s.embedding_model, "input": texts,
                          "encoding_format": "float",
                          **({"dimensions": s.embedding_dimensions}
                             if s.embedding_model in {"text-embedding-v3", "text-embedding-v4"}
                             else {})})
                if response.status_code != 200:
                    code = ""
                    try:
                        body = response.json()
                        error = body.get("error") if isinstance(body, dict) else None
                        candidate = error.get("code") if isinstance(error, dict) else None
                        if (isinstance(candidate, str) and 1 <= len(candidate) <= 64
                                and re.fullmatch(r"[A-Za-z0-9_.-]+", candidate)):
                            code = "_" + candidate
                    except (TypeError, ValueError):
                        pass
                    raise PolicyError(f"embedding_request_rejected_http_{response.status_code}{code}")
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

    def __init__(self, settings, *, connect=None, embedder=None, tokenizer=None, reranker=None):
        self.settings = settings
        self._connect = connect
        self.embedder = embedder or EmbeddingClient(settings)
        self.tokenizer = tokenizer or ChineseTokenizer(settings.policy_dictionary_path)
        self.reranker = reranker or QwenReranker(settings)
        self._planning_rules_version = None
        self._planning_rules = None

    @contextmanager
    def connection(self):
        s = self.settings
        if s.missing_policy_config(embedding=False):
            raise PolicyError("policy_database_configuration_required")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", s.policy_key):
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
            index_status = self._ensure_hnsw_index(conn)
        return {"status": "ok", "schema_version": 1, "vector_index": index_status}

    def _ensure_hnsw_index(self, conn):
        s = self.settings
        try:
            validate_hnsw_parameters(
                s.embedding_dimensions, m=s.policy_hnsw_m,
                ef_construction=s.policy_hnsw_ef_construction,
                ef_search=s.policy_hnsw_ef_search)
        except ValueError:
            raise PolicyError("invalid_hnsw_configuration") from None
        name = hnsw_index_name(s.embedding_dimensions)
        row = conn.execute("SELECT to_regclass(%s) IS NOT NULL AS available", (name,)).fetchone()
        available = bool(row and (row.get("available") if hasattr(row, "get") else row[0]))
        if available:
            return {"name": name, "status": "present"}
        conn.execute(render_index_ddl(
            s.embedding_dimensions, m=s.policy_hnsw_m,
            ef_construction=s.policy_hnsw_ef_construction))
        return {"name": name, "status": "created"}

    def migrate_hnsw(self):
        """Create the configured global HNSW index for an existing database."""
        with self.connection() as conn:
            return {"status": "ok", "vector_index": self._ensure_hnsw_index(conn)}

    def _vector_search(self, conn, document_id, vector):
        s = self.settings
        try:
            validate_hnsw_parameters(
                s.embedding_dimensions, m=s.policy_hnsw_m,
                ef_construction=s.policy_hnsw_ef_construction,
                ef_search=s.policy_hnsw_ef_search)
        except ValueError:
            raise PolicyError("invalid_hnsw_configuration") from None
        name = hnsw_index_name(s.embedding_dimensions)
        row = conn.execute("SELECT to_regclass(%s) IS NOT NULL AS available", (name,)).fetchone()
        available = bool(row and (row.get("available") if hasattr(row, "get") else row[0]))
        vector_text = vector_literal(vector)
        if available:
            # Keep the document scope in the same query. A larger ef_search limits
            # recall loss from the post-filter when multiple policy documents exist.
            conn.execute("SELECT set_config('hnsw.ef_search', %s, true)", (str(s.policy_hnsw_ef_search),))
            rows = conn.execute(
                f"SELECT chunk_id FROM policy_chunks WHERE document_id=%s "
                f"ORDER BY embedding::vector({s.embedding_dimensions}) <=> "
                f"%s::vector({s.embedding_dimensions}),chunk_id LIMIT %s",
                (document_id, vector_text, s.policy_recall_k)).fetchall()
            return rows, "hnsw"
        # The exact path is intentionally retained for controlled migration and
        # incident recovery when the configured index is absent.
        rows = conn.execute(
            f"WITH scoped AS MATERIALIZED (SELECT * FROM policy_chunks WHERE document_id=%s) "
            f"SELECT chunk_id FROM scoped ORDER BY embedding::vector({s.embedding_dimensions}) <=> "
            f"%s::vector({s.embedding_dimensions}),chunk_id LIMIT %s",
            (document_id, vector_text, s.policy_recall_k)).fetchall()
        return rows, "exact_fallback"

    def stage(self, bundle: PolicyBundle):
        bundle = PolicyBundle.model_validate(bundle.model_dump(mode="json"))
        s = self.settings
        if bundle.policy_key != s.policy_key:
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
                'SELECT id, bundle_sha256, embedding_space, tokenizer FROM policy_documents WHERE policy_key=%s AND version=%s',
                (s.policy_key, bundle.version)).fetchone()
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
                'INSERT INTO policy_documents (policy_key,version,effective_from,effective_to,source,source_sha256,bundle_sha256,embedding_space,embedding_model,embedding_dimensions,tokenizer,warnings) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id',
                (s.policy_key, bundle.version, bundle.effective_from, bundle.effective_to,
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
            self._ensure_hnsw_index(conn)
        return {"status": "draft", "document_id": document_id, "bundle_sha256": digest,
                "chunks": len(bundle.chunks), "warnings": bundle.warnings}

    def publish(self, document_id, reviewed_sha256, *, supersede=False):
        s = self.settings
        with self.connection() as conn:
            # Serialize version publication for the school policy, including concurrent CLI invocations.
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                         (s.policy_key,))
            doc = conn.execute(
                'SELECT * FROM policy_documents WHERE id=%s AND policy_key=%s FOR UPDATE',
                (document_id, s.policy_key)).fetchone()
            if not doc or doc["bundle_sha256"] != reviewed_sha256:
                raise PolicyError("review_digest_or_document_mismatch")
            if doc["status"] == "published":
                return {"status": "unchanged", "document_id": document_id}
            overlap = conn.execute(
                "SELECT id,effective_from FROM policy_documents WHERE policy_key=%s AND status='published' AND daterange(effective_from,effective_to,'[]') && daterange(%s::date,%s::date,'[]')",
                (s.policy_key, doc["effective_from"], doc["effective_to"])).fetchall()
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
            "SELECT d.*, r.rules->'source_clauses'->'scope' AS scope_clauses, r.rules->>'profile' AS policy_profile FROM policy_documents d LEFT JOIN policy_rules r ON r.document_id=d.id WHERE d.policy_key=%s AND d.status='published' ORDER BY d.published_at DESC NULLS LAST, d.id DESC LIMIT 1",
            (s.policy_key,)).fetchall()
        if len(rows) > 1:
            raise PolicyError("ambiguous_policy_versions")
        return rows[0] if rows else None

    def resolve_planning_standards(self, trip):
        """Derive canonical standards once from the currently published policy."""
        try:
            with self.connection() as conn:
                doc = self._active(conn)
                if not doc:
                    return trip
                if self._planning_rules_version != doc["version"]:
                    row = conn.execute("SELECT rules FROM policy_rules WHERE document_id=%s",
                                       (doc["id"],)).fetchone()
                    self._planning_rules = load_reviewed_rules(row["rules"]) if row else None
                    self._planning_rules_version = doc["version"]
                rules = self._planning_rules
            if not isinstance(rules, UniversityRules):
                return trip
            category, lodging = planning_standards_for(trip, rules)
            updates = {}
            if category and not trip.transport_standard:
                updates["transport_standard"] = category
            if lodging and not trip.lodging_standard:
                updates["lodging_standard"] = lodging
            return trip.model_copy(update=updates) if updates else trip
        except PolicyError:
            return trip

    def recommendation_profile(self, trip):
        """Expose reviewed policy facts for deterministic candidate ranking."""
        profile = {
            "status": "unavailable",
            "reason": "planning_policy_unavailable",
            "transport_standard": trip.transport_standard,
            "lodging_standard": trip.lodging_standard,
            "allowed_transport": {},
        }
        try:
            with self.connection() as conn:
                doc = self._active(conn)
                if not doc:
                    profile["reason"] = "no_published_policy"
                    return profile
                row = conn.execute("SELECT rules FROM policy_rules WHERE document_id=%s",
                                   (doc["id"],)).fetchone()
                rules = load_reviewed_rules(row["rules"]) if row else None
                if not isinstance(rules, UniversityRules):
                    profile["reason"] = "reviewed_rules_unavailable"
                    return profile
                category, lodging = planning_standards_for(trip, rules)
                profile.update(
                    status="ok", transport_standard=category,
                    lodging_standard=lodging,
                    allowed_transport=rules.transport_classes.get(category, {}) if category else {},
                    known_transport_classes=known_transport_classes(rules),
                    policy_version=doc["version"],
                    policy_document_id=doc["id"], policy_source_sha256=doc["source_sha256"],
                    citation_status="unavailable",
                )
                limit = {}
                if trip.query_scope != "transport" and trip.destination and lodging:
                    limits = conn.execute(
                        "SELECT data FROM policy_lodging_limits WHERE document_id=%s",
                        (doc["id"],)).fetchall()
                    limit = lookup_lodging(
                        [LodgingRow.model_validate(item["data"]) for item in limits],
                        destination=trip.destination, province=trip.destination_province,
                        standard=lodging, day=trip.check_in,
                    )
                    if limit.get("status") == "ok":
                        profile.update(
                            lodging_cap=limit["cap"], lodging_currency=limit["currency"],
                            lodging_unit=limit["unit"], lodging_seasonal=limit["seasonal"],
                            lodging_date=limit.get("date"),
                        )
                    else:
                        profile["lodging_limit_status"] = limit.get("status", "unknown")
                        profile["lodging_limit_reason"] = limit.get("reason", "lodging_limit_unavailable")
                self._recommendation_evidence(conn, doc, row["rules"], rules, trip, profile, limit)
                return profile
        except PolicyError as error:
            profile["reason"] = str(error)
            return profile
        except Exception:
            return profile

    def _recommendation_evidence(self, conn, doc, raw_rules, rules, trip, profile, limit):
        """Attach same-publication evidence without another retrieval or model call."""
        clauses = []
        if trip.query_scope != "hotel" or trip.personnel_type == "student":
            clauses = list(dict.fromkeys(rules.source_clauses["scope"] + rules.source_clauses["transport"]))
        row_ids = limit.get("row_ids", []) if limit.get("status") == "ok" else []
        rows = conn.execute(
            "SELECT * FROM policy_chunks WHERE document_id=%s "
            "AND (clause=ANY(%s) OR chunk_id=ANY(%s)) ORDER BY chunk_id",
            (doc["id"], clauses, row_ids)).fetchall()
        config = {
            "依据类型": "项目已审核规则配置（非制度原文）",
            "本次推荐交通类别": profile.get("transport_standard"),
            "本次推荐住宿档次": profile.get("lodging_standard"),
            "采用的规则配置": {key: raw_rules[key] for key in (
                "title_categories", "category_order", "transport_classes",
                "senior_post_max_grade", "senior_post_category", "student_category") if key in raw_rules},
            "说明": "交通身份只确定交通类别；住宿身份单独确定住宿档次。两者不能互相推导。住宿金额以附表为据。",
        }
        config_source = {
            "source_id": "P1", "source": "项目已审核推荐标准配置", "source_type": "reviewed_rule",
            "document_id": doc["id"], "version": doc["version"], "source_sha256": doc["source_sha256"],
            "chunk": "reviewed-recommendation-standards", "clause": "", "section": "推荐标准配置",
            "text": json.dumps(config, ensure_ascii=False, indent=2),
            "file_sha256": hashlib.sha256(json.dumps(raw_rules, ensure_ascii=False, sort_keys=True)
                                           .encode("utf-8")).hexdigest(),
        }
        sources = [config_source]
        for row in rows:
            item = self._source(row, doc)
            item.pop("table", None)
            sources.append({**item, "source_id": f"P{len(sources) + 1}"})
        transport_ids = ["P1"] + [s["source_id"] for s in sources[1:] if s["clause"] in clauses]
        cap_ids = [s["source_id"] for s in sources[1:] if s["chunk"] in row_ids]
        complete_cap = bool(row_ids) and {s["chunk"] for s in sources[1:] if s["chunk"] in row_ids} == set(row_ids)
        complete_transport = set(clauses) <= {s["clause"] for s in sources[1:]}
        profile.update(sources=sources, standard_citations={
            "transport_standard": transport_ids if profile.get("transport_standard") else [],
            "allowed_transport": transport_ids if profile.get("allowed_transport") else [],
            "lodging_standard": ["P1"] if profile.get("lodging_standard") else [],
            "lodging_cap": cap_ids if complete_cap else [],
        }, citation_status="available" if complete_transport and (not row_ids or complete_cap) else "partial")
        from .recommendation_evidence import attach_fragments
        attach_fragments(profile, rows, rules, raw_rules, trip)
        profile["citation_mode"] = self.settings.policy_citation_mode

    @staticmethod
    def _source(row, doc):
        provenance = (row.get("table_data") or {}).get("_provenance", {})
        return {"source": provenance.get("source", doc["source"]), "version": doc["version"], "document_id": doc["id"],
                "source_sha256": doc["source_sha256"], "chunk": row["chunk_id"], "clause": row["clause"],
                "section": row["section"], "text": row["content"], "table": row.get("table_data"),
                "file_sha256": provenance.get("sha256", doc["source_sha256"]),
                "table_row": (row.get("table_data") or {}).get("row"),
                "sheet": provenance.get("sheet"), "cell_range": provenance.get("cell_range"),
                "adoption_note": provenance.get("adoption_note")}

    def search_for_facts(self, question, facts, *, policy_query=None, include_rankings=False):
        return self.search(question, _facts=facts, _policy_query=policy_query,
                           _include_rankings=include_rankings)

    def _explanation_standards(self, conn, doc, facts):
        fields = ("academic_title", "employee_grade", "technical_grade", "management_grade", "personnel_type",
                  "student_activity_eligible", "transport_standard", "lodging_standard")
        if (doc.get("policy_profile") != "university"
                or not any(facts.get(key) is not None for key in fields)):
            return {}, None
        row = conn.execute("SELECT rules FROM policy_rules WHERE document_id=%s", (doc["id"],)).fetchone()
        rules = load_reviewed_rules(row["rules"]) if row else None
        if not isinstance(rules, UniversityRules):
            return {}, None
        identity = SimpleNamespace(**{key: facts.get(key) for key in fields})
        category = transport_category(identity, rules)
        lodging = lodging_standard_for(identity, rules)
        if not category and not lodging:
            return {}, None
        chunk = "reviewed-identity-standards"
        resolved = {"transport_standard": category, "lodging_standard": lodging, "source_chunk": chunk}
        text = json.dumps({
            "依据类型": "项目已审核规则配置（非制度原文）",
            "用户已提供的身份条件": {key: facts[key] for key in fields if facts.get(key) is not None},
            "适用交通类别": category, "住宿人员档次": lodging,
            "职称分类配置": rules.title_categories,
            "高级岗位最高等级": rules.senior_post_max_grade, "高级岗位交通类别": rules.senior_post_category,
            "多身份类别优先顺序": rules.category_order, "学生交通类别": rules.student_category,
            "学生适用条件": "学生仅在明确属于规定相关活动时确定个人适用类别",
        }, ensure_ascii=False)
        source = {"source": "项目已审核身份标准映射", "source_type": "reviewed_rule",
                  "version": doc["version"], "document_id": doc["id"],
                  "source_sha256": doc["source_sha256"], "chunk": chunk,
                  "section": "身份与差旅标准映射", "clause": "", "text": text,
                  "file_sha256": hashlib.sha256(json.dumps(
                      row["rules"], ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()}
        return resolved, source

    def _rerank(self, conn, doc, question, candidate_ids, baseline_ids):
        s = self.settings
        info = {"status": "disabled", "model": s.rerank_model,
                "candidate_ids": candidate_ids, "selected_ids": baseline_ids,
                "scores": [], "elapsed_ms": 0, "usage": {}}
        if s.policy_rerank_mode == "rrf" or not candidate_ids:
            return baseline_ids, info
        started = perf_counter()
        with observation("policy_rerank", kind="retriever", count=len(candidate_ids),
                         model_ref=hashlib.sha256(s.rerank_model.encode()).hexdigest()) as span:
            reason = self.reranker.configuration_error()
            if not reason:
                candidate_rows = conn.execute(
                    "SELECT * FROM policy_chunks WHERE document_id=%s AND chunk_id=ANY(%s)",
                    (doc["id"], candidate_ids)).fetchall()
                lookup = {row["chunk_id"]: row for row in candidate_rows}
                # Rank exact stored text; citation identity and content stay untouched.
                if set(lookup) != set(candidate_ids):
                    reason = "rerank_candidate_missing"
                else:
                    documents = [lookup[key]["content"] for key in candidate_ids]
                    span.content(input=lambda: {"query": question, "candidate_ids": candidate_ids,
                                                "documents": documents, "model": s.rerank_model})
                    try:
                        ranked = self.reranker.rank(question, documents)
                        ordered = [candidate_ids[item["index"]] for item in ranked["results"]]
                        info.update(status="ok", selected_ids=ordered[:s.policy_top_k],
                                    scores=[{"chunk": candidate_ids[item["index"]], "score": item["score"]}
                                            for item in ranked["results"]], usage=ranked["usage"])
                    except RerankError as error:
                        reason = str(error)
            if reason:
                info.update(status="fallback", reason=reason)
            info["elapsed_ms"] = round((perf_counter() - started) * 1000)
            span.update(status="ok" if info["status"] == "ok" else "unavailable",
                        error_code=reason or "", elapsed_ms=info["elapsed_ms"],
                        usage_details=info["usage"], **info["usage"])
            span.content(output=lambda: info)
        return info["selected_ids"], info

    def search(self, question, *, _facts=None, _policy_query=None, _include_rankings=False):
        if not isinstance(question, str) or not question.strip() or len(question) > 4000:
            return {"status": "error", "reason": "invalid_policy_query", "sources": []}
        try:
            with self.connection() as conn:
                doc = self._active(conn)
                if not doc:
                    return {"status": "empty", "reason": "no_published_policy", "sources": []}
                if doc["embedding_space"] != self.embedder.space or doc["tokenizer"] != self.tokenizer.signature:
                    raise PolicyError("policy_embedding_or_tokenizer_changed_reimport_required")
                vector_ids = []
                vector_search_mode = "embedding_unavailable"
                vector_error_code = None
                try:
                    vector = validate_vector(self.embedder.embed([question])[0], self.settings.embedding_dimensions)
                    vector_rows, vector_search_mode = self._vector_search(conn, doc["id"], vector)
                    vector_ids = [r["chunk_id"] for r in vector_rows]
                except PolicyError as error:
                    # Embedding is an accelerator for hybrid retrieval. Keep the
                    # lexical path usable when the provider rejects a request,
                    # while exposing only the fixed, secret-free error code.
                    vector_error_code = _safe_vector_error_code(error)
                except Exception:
                    # Custom embedders and provider adapters must not be able to
                    # leak response bodies or credentials through the tool result.
                    vector_error_code = "embedding_request_failed_check_configuration"
                terms = self.tokenizer.tokenize(question).split()
                keyword_rows = []
                if terms:
                    keyword_rows = conn.execute(
                        "SELECT chunk_id FROM policy_chunks WHERE document_id=%s "
                        "AND keywords @@ to_tsquery('simple',%s) "
                        "ORDER BY ts_rank_cd(keywords,to_tsquery('simple',%s)) DESC,chunk_id LIMIT %s",
                        (doc["id"], " | ".join(terms), " | ".join(terms), self.settings.policy_recall_k)).fetchall()
                keyword_ids = [r["chunk_id"] for r in keyword_rows]
                ids = reciprocal_rank_fusion(vector_ids, keyword_ids,
                                             k=self.settings.policy_rrf_k, limit=self.settings.policy_top_k)
                rrf_ids = list(ids)
                candidate_ids = reciprocal_rank_fusion(
                    vector_ids, keyword_ids, k=self.settings.policy_rrf_k,
                    limit=max(self.settings.policy_top_k, self.settings.rerank_candidates))
                ids, rerank_info = self._rerank(conn, doc, question, candidate_ids, rrf_ids)
                facts = _facts or {}
                coverage = (_policy_query or {}).get("coverage", "target")
                resolved, mapping_source = self._explanation_standards(conn, doc, facts)
                exact = None
                if doc.get("policy_profile") == "university" and facts.get("kind") == "hotel":
                    limits = conn.execute("SELECT data FROM policy_lodging_limits WHERE document_id=%s",
                                          (doc["id"],)).fetchall()
                    lodging_rows = [LodgingRow.model_validate(r["data"]) for r in limits]
                    standard = resolved.get("lodging_standard") or facts.get("lodging_standard")
                    if coverage == "all_regions":
                        lodging_table = list_lodging(
                            lodging_rows, standard=standard,
                            day=date.fromisoformat(facts["expense_date"])
                            if facts.get("expense_date") else None)
                        table_ids = lodging_table.get("row_ids", [])
                        table_db_rows = conn.execute(
                            "SELECT * FROM policy_chunks WHERE document_id=%s AND chunk_id=ANY(%s)",
                            (doc["id"], table_ids)).fetchall() if table_ids else []
                        table_lookup = {row["chunk_id"]: row for row in table_db_rows}
                        table_sources = [self._source(table_lookup[row_id], doc)
                                         for row_id in table_ids if row_id in table_lookup]
                        lodging_table["source_chunks"] = [item["chunk"] for item in table_sources]
                        sources = table_sources + ([mapping_source] if mapping_source else [])
                        result = {"status": "ok" if lodging_table.get("status") == "ok" else "empty",
                                  "policy_profile": doc.get("policy_profile"), "sources": sources,
                                  "context_incomplete": False, "lodging_lookup": None,
                                  "lodging_table": lodging_table,
                                  "resolved_standards": resolved if mapping_source else {},
                                  "warning": "Evidence only, not approval."}
                        if vector_error_code:
                            result.update(degraded=True, vector_error_code=vector_error_code)
                        if _include_rankings:
                            result["rankings"] = {"vector_ids": vector_ids, "keyword_ids": keyword_ids,
                                                  "rrf_ids": rrf_ids, "token_terms": terms,
                                                  "vector_search_mode": vector_search_mode,
                                                  "selected_ids": rerank_info["selected_ids"],
                                                  "rerank": rerank_info}
                            if vector_error_code:
                                result["rankings"]["vector_error_code"] = vector_error_code
                        return result
                    exact = lookup_lodging(lodging_rows,
                                           destination=facts.get("destination"),
                                           province=facts.get("destination_province"),
                                           standard=standard,
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
                sources = [self._source(row, doc) for row in rows]
                if sources and mapping_source:
                    sources.append(mapping_source)
                result = {"status": "ok" if rows else "empty",
                        "policy_profile": doc.get("policy_profile"),
                        "sources": sources,
                        "context_incomplete": incomplete or len(sources) > self.settings.policy_context_limit,
                        "lodging_lookup": exact,
                        "warning": "Evidence only, not approval. Missing linked clauses require further retrieval."}
                if vector_error_code:
                    result.update(degraded=True, vector_error_code=vector_error_code)
                if mapping_source:
                    result["resolved_standards"] = resolved
                if _include_rankings:
                    result["rankings"] = {"vector_ids": vector_ids, "keyword_ids": keyword_ids,
                                          "rrf_ids": rrf_ids, "token_terms": terms,
                                          "vector_search_mode": vector_search_mode,
                                          "selected_ids": rerank_info["selected_ids"],
                                          "rerank": rerank_info}
                    if vector_error_code:
                        result["rankings"]["vector_error_code"] = vector_error_code
                return result
        except PolicyError as error:
            return {"status": "unavailable", "reason": str(error), "sources": []}

    def estimate(self, facts: ExpenseFacts):
        return self._check(facts, estimate=True)

    def check(self, facts: ExpenseFacts):
        return self._check(facts, estimate=False)

    def _check(self, facts: ExpenseFacts, *, estimate):
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
                    lodging_rows = [LodgingRow.model_validate(r["data"]) for r in limits]
                    checker = lambda unit: check_university_expense(unit, reviewed, lodging_rows,
                                                                     projection=estimate)
                else:
                    checker = lambda unit: check_expense(unit, reviewed)
                if estimate:
                    result = estimate_expense(
                        facts, checker, max_nights=self.settings.reimbursement_max_nights,
                        currency=reviewed.currency,
                        effective_from=doc.get("effective_from"), effective_to=doc.get("effective_to"))
                elif (facts.expense_scenario != "actual" or facts.amount_scope != "single"
                      or facts.nightly_amounts or facts.check_out is not None):
                    result = {"status": "insufficient_information", "reason": "estimate_mode_required",
                              "source_clauses": [], "approved": False}
                else:
                    result = checker(facts)
                sources = conn.execute(
                    "SELECT * FROM policy_chunks WHERE document_id=%s AND clause=ANY(%s) ORDER BY chunk_id",
                    (doc["id"], result["source_clauses"])).fetchall()
                if result.get("row_ids"):
                    sources += conn.execute(
                        "SELECT * FROM policy_chunks WHERE document_id=%s AND chunk_id=ANY(%s) ORDER BY chunk_id",
                        (doc["id"], result["row_ids"])).fetchall()
                result.update(policy_version=doc["version"], sources=[self._source(r, doc) for r in sources])
                if estimate:
                    bind_estimate_sources(result, result["sources"], doc)
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

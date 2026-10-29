"""Offline, transactional removal of the retired organization namespace.

This is a deployment tool, never called by an HTTP request or graph node.
No records are selected for deletion or merged. Ambiguous legacy data aborts.
"""

import re
from collections import defaultdict

from psycopg import sql

from .memory import validate_id


class SchoolMigrationError(ValueError):
    """Fixed diagnostics only: never include credentials or stored payloads."""


MEMORY_TABLES = frozenset({
    "trips", "memory_imports", "auth_users", "auth_sessions", "auth_login_limits", "user_preferences",
    "auth_personnel", "conversations", "chat_runs", "chat_recoveries",
    "resource_daily_usage", "attachments", "attachment_links",
    "invoice_registry", "invoice_files", "invoice_reviews",
})
CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")


def user_thread(value):
    parts = value.split(":")
    if len(parts) != 3:
        raise SchoolMigrationError("legacy_thread_format_invalid")
    try:
        for part in parts:
            validate_id(part)
    except ValueError:
        raise SchoolMigrationError("legacy_thread_format_invalid") from None
    return parts[0], ":".join(parts[1:])


def without_namespace(definition):
    """Only remove a leading identifier from SQL column lists, never literals."""
    return re.sub(r'(\(\s*)"?company_id"?\s*,\s*', r'\1', definition)


def _table(schema, name):
    return sql.Identifier(schema, name)


def _inventory(connection, schema, kind):
    allowed = MEMORY_TABLES if kind == "memory" else {"policy_documents"}
    columns = connection.execute(
        "SELECT table_name,column_name FROM information_schema.columns "
        "WHERE table_schema=%s", (schema,)).fetchall()
    tables = defaultdict(set)
    for row in columns:
        tables[row["table_name"]].add(row["column_name"])
    legacy = {name for name, cols in tables.items() if "company_id" in cols}
    if legacy - allowed:
        raise SchoolMigrationError("unknown_legacy_tables_require_review")
    counts, namespaces = {}, set()
    for name in sorted(legacy):
        rows = connection.execute(sql.SQL(
            "SELECT company_id,count(*) AS n FROM {} GROUP BY company_id").format(
                _table(schema, name))).fetchall()
        counts[name] = sum(row["n"] for row in rows)
        for row in rows:
            try:
                validate_id(row["company_id"])
            except (TypeError, ValueError):
                raise SchoolMigrationError("legacy_namespace_invalid") from None
            namespaces.add(row["company_id"])
    mappings = set()
    if kind == "memory":
        for name in CHECKPOINT_TABLES:
            if name not in tables:
                continue
            threads = connection.execute(sql.SQL("SELECT DISTINCT thread_id FROM {}").format(
                _table(schema, name))).fetchall()
            for row in threads:
                parts = row["thread_id"].split(":")
                if len(parts) == 2 and not legacy:
                    try:
                        for part in parts: validate_id(part)
                    except ValueError:
                        raise SchoolMigrationError("thread_format_invalid") from None
                    continue
                namespace, target = user_thread(row["thread_id"])
                namespaces.add(namespace)
                mappings.add((row["thread_id"], target))
        if "chat_runs" in tables:
            active = connection.execute(sql.SQL(
                "SELECT count(*) AS n FROM {} WHERE status='running'").format(
                    _table(schema, "chat_runs"))).fetchone()["n"]
            if active:
                raise SchoolMigrationError("running_requests_stop_service_and_review_first")
    if len(namespaces) > 1:
        raise SchoolMigrationError("multiple_legacy_namespaces_require_manual_resolution")
    return tables, legacy, counts, mappings


def migrate_schema(connection, schema, *, kind="memory", apply=False, digest_mappings=()):
    """Caller supplies one connection; all changes in this target commit together.

    Memory and policy databases can differ, so each is an explicit independent
    maintenance step. The service must stay stopped until both have completed.
    """
    if kind not in {"memory", "policy"}:
        raise ValueError("kind must be memory or policy")
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema) or schema.startswith("pg_"):
        raise ValueError("Invalid migration schema")
    with connection.transaction():
        if not apply:
            connection.execute("SET TRANSACTION READ ONLY")
        connection.execute("SET LOCAL lock_timeout='5s'")
        connection.execute("SET LOCAL statement_timeout='120s'")
        connection.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(schema)))
        tables, legacy, counts, mappings = _inventory(connection, schema, kind)
        if apply:
            # Lock all application tables in this schema, including checkpoint
            # blobs and dependent tables, before repeating the preflight.
            names = sorted((set(tables) & MEMORY_TABLES) | (set(tables) & set(CHECKPOINT_TABLES))) if kind == "memory" else sorted(set(tables) & {"policy_documents", "policy_chunks", "policy_rules", "policy_sources", "policy_lodging_limits", "policy_check_logs"})
            for name in names:
                connection.execute(sql.SQL("LOCK TABLE {} IN ACCESS EXCLUSIVE MODE").format(_table(schema,name)))
            tables, legacy, counts, mappings = _inventory(connection, schema, kind)
        report = {"kind": kind, "schema": schema, "tables": counts,
                  "remapped_threads": len(mappings), "status": "ready" if legacy or mappings else "already_migrated"}
        digest_updates=[]
        if kind=="policy" and legacy:
            by_old={}
            for item in digest_mappings:
                if (set(item)!={"policy_key","version","old_digest","new_digest"} or
                        any(not isinstance(item[k],str) or not re.fullmatch(r"[a-f0-9]{64}",item[k])
                            for k in ("old_digest","new_digest")) or item['old_digest'] in by_old):
                    raise SchoolMigrationError("invalid_policy_digest_mapping")
                by_old[item['old_digest']]=item
            rows=connection.execute(sql.SQL("SELECT id,policy_key,version,bundle_sha256 FROM {}").format(
                _table(schema,"policy_documents"))).fetchall()
            for row in rows:
                item=by_old.get(row['bundle_sha256'])
                if not item or (item['policy_key'],item['version'])!=(row['policy_key'],row['version']):
                    raise SchoolMigrationError("policy_digest_mapping_required_convert_all_existing_bundles")
                digest_updates.append((item['new_digest'],row['id']))
        if not apply or not legacy and not mappings:
            return report

        constraints = connection.execute(
            "SELECT c.conname,c.contype,t.relname AS table_name,"
            "n.nspname AS schema_name,pg_get_constraintdef(c.oid) AS definition "
            "FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid "
            "JOIN pg_namespace n ON n.oid=t.relnamespace "
            "WHERE n.nspname=%s OR c.confrelid IN "
            "(SELECT oid FROM pg_class WHERE relnamespace=%s::regnamespace)",
            (schema, schema)).fetchall()
        affected = []
        for row in constraints:
            if "company_id" in row["definition"]:
                # PostgreSQL 18 exposes NOT NULL as a separate constraint;
                # removing its column removes this constraint automatically.
                if row["contype"] == "n":
                    continue
                if row["schema_name"] != schema or row["table_name"] not in legacy:
                    raise SchoolMigrationError("external_constraints_require_review")
                if row["contype"] not in {"p","u","f"}:
                    raise SchoolMigrationError("unsupported_namespace_constraint")
                affected.append(row)
        indexes = connection.execute(
            "SELECT indexname,indexdef FROM pg_indexes WHERE schemaname=%s",
            (schema,)).fetchall()
        key_names = {row["conname"] for row in affected}
        indexes = [r for r in indexes if "company_id" in r["indexdef"] and r["indexname"] not in key_names]

        # Drop referencing constraints first; recreate keys before foreign keys.
        for row in sorted(affected,key=lambda r:r["contype"] != "f"):
            connection.execute(sql.SQL("ALTER TABLE {} DROP CONSTRAINT {}").format(
                _table(schema,row["table_name"]), sql.Identifier(row["conname"])))
        for row in indexes:
            connection.execute(sql.SQL("DROP INDEX {}").format(sql.Identifier(schema,row["indexname"])))
        for name in sorted(legacy):
            connection.execute(sql.SQL("ALTER TABLE {} DROP COLUMN company_id").format(_table(schema,name)))
        for row in sorted(affected,key=lambda r:r["contype"] == "f"):
            connection.execute(sql.SQL("ALTER TABLE {} ADD CONSTRAINT {} ").format(
                _table(schema,row["table_name"]), sql.Identifier(row["conname"])) + sql.SQL(without_namespace(row["definition"])))
        for row in indexes:
            connection.execute(sql.SQL(without_namespace(row["indexdef"])))
        for digest,ident in digest_updates:
            connection.execute(sql.SQL("UPDATE {} SET bundle_sha256=%s WHERE id=%s").format(
                _table(schema,"policy_documents")), (digest,ident))

        for old,new in sorted(mappings):
            for name in CHECKPOINT_TABLES:
                if name in tables:
                    connection.execute(sql.SQL("UPDATE {} SET thread_id=%s WHERE thread_id=%s").format(
                        _table(schema,name)), (new,old))
            if "checkpoints" in tables:
                connection.execute(sql.SQL(
                    "UPDATE {} SET metadata=jsonb_set(metadata,'{{thread_id}}',to_jsonb(%s::text)) "
                    "WHERE thread_id=%s AND metadata->>'thread_id'=%s").format(
                        _table(schema,"checkpoints")), (new,new,old))
        if kind == "memory" and "resource_daily_usage" in legacy:
            checks = [r for r in constraints if r["table_name"] == "resource_daily_usage" and r["contype"] == "c" and "scope" in r["definition"]]
            for row in checks:
                connection.execute(sql.SQL("ALTER TABLE {} DROP CONSTRAINT {}").format(
                    _table(schema,"resource_daily_usage"),sql.Identifier(row["conname"])))
            connection.execute(sql.SQL("UPDATE {} SET scope='school',subject_id='school' WHERE scope='company'").format(_table(schema,"resource_daily_usage")))
            connection.execute(sql.SQL("ALTER TABLE {} ADD CONSTRAINT resource_daily_usage_scope_check CHECK(scope IN ('school','user'))").format(_table(schema,"resource_daily_usage")))
        for name,count in counts.items():
            actual=connection.execute(sql.SQL("SELECT count(*) AS n FROM {}").format(_table(schema,name))).fetchone()["n"]
            if actual != count:
                raise SchoolMigrationError("row_count_verification_failed")
        report["status"]="migrated"
        return report


"""Explicit, read-only SQLite/JSON import into an initialized memory schema."""

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver
from psycopg.types.json import Jsonb

from .memory import validate_id
from .memory_pg import MemoryIdentity, ScopedPostgresSaver, memory_connection


class MigrationError(ValueError):
    """Only fixed, credential-free migration diagnostics may use this type."""


@contextmanager
def readonly_sqlite(path):
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        connection.execute("BEGIN")
        yield connection
    finally:
        connection.close()


def legacy_thread(thread_id: str) -> tuple[str, str]:
    parts = thread_id.split(":")
    if len(parts) != 2:
        raise MigrationError("旧会话标识格式不支持，需要人工确认用户与会话映射")
    return validate_id(parts[0]), validate_id(parts[1])


def _sqlite_tables(connection):
    return {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def inventory(directory: Path, legacy_directory: Path) -> dict:
    users = set()
    counts = {"trips": 0, "checkpoints": 0, "legacy_files": 0}
    path = directory / "memory.sqlite"
    if path.exists():
        with readonly_sqlite(path) as connection:
            if "trips" in _sqlite_tables(connection):
                rows = connection.execute("SELECT user_id FROM trips").fetchall()
                counts["trips"] = len(rows)
                users.update(validate_id(row[0]) for row in rows)
    path = directory / "checkpoints.sqlite"
    if path.exists():
        with readonly_sqlite(path) as connection:
            rows = connection.execute("SELECT thread_id FROM checkpoints").fetchall()
            counts["checkpoints"] = len(rows)
            users.update(legacy_thread(row[0])[0] for row in rows)
    for path in legacy_directory.glob("*.json"):
        users.add(validate_id(path.stem))
        counts["legacy_files"] += 1
    return {"users": sorted(users), **counts}


@dataclass
class ImportSnapshot:
    user_id: str
    trips: list[dict]
    checkpoints: list = field(default_factory=list)
    writes: list = field(default_factory=list)
    digest: str = ""

    def summary(self):
        return {"trips": len(self.trips), "checkpoints": len(self.checkpoints),
                "pending_writes": len(self.writes)}


def read_snapshot(directory: Path, legacy_directory: Path, user_id: str) -> ImportSnapshot:
    validate_id(user_id)
    trips = {}
    evidence = []
    legacy_path = legacy_directory / f"{user_id}.json"
    if legacy_path.exists():
        text = legacy_path.read_text(encoding="utf-8")
        evidence.append(text)
        legacy = json.loads(text)
        if not isinstance(legacy, dict):
            raise MigrationError("旧 JSON 记忆必须为对象，请先修复源文件副本")
        for trip in legacy.get("trip_history", []):
            if not isinstance(trip, dict) or not isinstance(trip.get("id"), str) or not trip["id"]:
                raise MigrationError("旧行程缺少 ID，需要人工映射；没有丢弃记录")
            if trip["id"] in trips and trips[trip["id"]] != trip:
                raise MigrationError("旧 JSON 中存在同 ID 不同内容的行程，请人工核对")
            trips[trip["id"]] = trip
    path = directory / "memory.sqlite"
    if path.exists():
        with readonly_sqlite(path) as connection:
            tables = _sqlite_tables(connection)
            if "users" in tables:
                row = connection.execute("SELECT data FROM users WHERE id=?", (user_id,)).fetchone()
                if row:
                    evidence.append(row)
                    # Parse retired preference payloads and discard them. A bad blob must not block trips.
                    try:
                        json.loads(row[0])
                    except (json.JSONDecodeError, TypeError, ValueError):
                        pass
            rows = [] if "trips" not in tables else connection.execute(
                "SELECT draft_id,data FROM trips WHERE user_id=? ORDER BY rowid", (user_id,),
            ).fetchall()
            evidence.extend(rows)
            for draft_id, data in rows:
                trip = json.loads(data)
                if not isinstance(trip, dict) or not draft_id or trip.get("id") != draft_id:
                    raise MigrationError("SQLite 行程 ID 与内容不一致，请人工核对")
                if draft_id in trips and trips[draft_id] != trip:
                    raise MigrationError("SQLite 与旧 JSON 的同 ID 行程内容冲突，请人工核对")
                trips.pop(draft_id, None)
                trips[draft_id] = trip
    snapshot = ImportSnapshot(user_id, list(trips.values()))
    path = directory / "checkpoints.sqlite"
    if path.exists():
        with readonly_sqlite(path) as connection:
            saver = SqliteSaver(connection)
            # Never call SQLite setup: it would attempt DDL and change journal mode.
            saver.is_setup = True
            threads = [row[0] for row in connection.execute("SELECT DISTINCT thread_id FROM checkpoints")
                       if legacy_thread(row[0])[0] == user_id]
            for thread in sorted(threads):
                items = list(saver.list({"configurable": {"thread_id": thread}}))
                for item in items:
                    if item.checkpoint.get("v") != 4:
                        raise MigrationError("仅验证了 v4 检查点迁移；其他版本需单独核验，未写入目标")
                snapshot.checkpoints.extend(reversed(items))
                raw = connection.execute(
                    "SELECT thread_id,checkpoint_ns,checkpoint_id,parent_checkpoint_id,type,checkpoint,metadata "
                    "FROM checkpoints WHERE thread_id=? ORDER BY checkpoint_ns,checkpoint_id", (thread,),
                ).fetchall()
                evidence.extend(raw)
                writes = connection.execute(
                    "SELECT thread_id,checkpoint_ns,checkpoint_id,task_id,idx,channel,type,value "
                    "FROM writes WHERE thread_id=? ORDER BY checkpoint_ns,checkpoint_id,task_id,idx", (thread,),
                ).fetchall()
                snapshot.writes.extend(writes)
                evidence.extend(writes)
    keys = {(item.config["configurable"]["thread_id"], item.config["configurable"]["checkpoint_ns"],
             item.checkpoint["id"]) for item in snapshot.checkpoints}
    for item in snapshot.checkpoints:
        if item.parent_config:
            parent = item.parent_config["configurable"]
            if (parent["thread_id"], parent["checkpoint_ns"], parent["checkpoint_id"]) not in keys:
                raise MigrationError("旧检查点缺失父记录，请检查源数据库完整性")
    if any(tuple(write[:3]) not in keys for write in snapshot.writes):
        raise MigrationError("存在没有检查点的中间写入，需要人工核对")
    serialized = json.dumps([user_id, evidence], ensure_ascii=False, sort_keys=True,
                            default=lambda value: {"bytes": value.hex()})
    snapshot.digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return snapshot


def import_snapshot(settings, snapshot: ImportSnapshot) -> dict:
    identity = MemoryIdentity(snapshot.user_id)
    scope = (identity.user_id,)

    def remap(config):
        values = dict(config["configurable"])
        user, session = legacy_thread(values["thread_id"])
        if user != identity.user_id:
            raise PermissionError("Source checkpoint belongs to another user")
        values["thread_id"] = identity.thread_id(session)
        return {"configurable": values}

    with memory_connection(settings) as connection, connection.transaction():
        # Migration is an offline maintenance operation. Also block concurrent SQL writers.
        connection.execute("LOCK TABLE trips,memory_imports,checkpoints,"
                           "checkpoint_blobs,checkpoint_writes IN SHARE ROW EXCLUSIVE MODE")
        done = connection.execute(
            'SELECT summary FROM memory_imports WHERE user_id=%s AND source_digest=%s',
            (*scope, snapshot.digest),
        ).fetchone()
        if done:
            return {"status": "already_imported", **done["summary"]}
        for trip in snapshot.trips:
            row = connection.execute(
                'SELECT data FROM trips WHERE user_id=%s AND draft_id=%s',
                (*scope, trip["id"]),
            ).fetchone()
            if row and row["data"] != trip:
                raise MigrationError("目标同 ID 行程内容冲突，已回滚本用户导入；请人工核对")
            connection.execute(
                'INSERT INTO trips(user_id,draft_id,data) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING',
                (*scope, trip["id"], Jsonb(trip)),
            )
        saver = ScopedPostgresSaver(connection, identity)
        threads = {remap(item.config)["configurable"]["thread_id"] for item in snapshot.checkpoints}
        for thread in threads:
            if connection.execute("SELECT 1 FROM checkpoints WHERE thread_id=%s LIMIT 1", (thread,)).fetchone():
                raise MigrationError("目标会话已存在，已回滚本用户导入；请勿覆盖正在使用的会话")
        for item in snapshot.checkpoints:
            parent = remap(item.parent_config) if item.parent_config else remap(item.config)
            if not item.parent_config:
                parent["configurable"].pop("checkpoint_id", None)
            saved = saver.put(parent, item.checkpoint, item.metadata, item.checkpoint["channel_versions"])
            # PostgresSaver drops deprecated metadata.writes; retain archival metadata verbatim.
            connection.execute(
                "UPDATE checkpoints SET metadata=%s WHERE thread_id=%s AND checkpoint_ns=%s AND checkpoint_id=%s",
                (Jsonb(item.metadata), saved["configurable"]["thread_id"],
                 saved["configurable"]["checkpoint_ns"], saved["configurable"]["checkpoint_id"]),
            )
        for thread, namespace, checkpoint, task, idx, channel, kind, value in snapshot.writes:
            user, session = legacy_thread(thread)
            if user != identity.user_id:
                raise PermissionError("Source writes belong to another user")
            # Preserve original indices, including negative interrupt/resume indices.
            # Both pinned savers use the same JsonPlus typed serialization; verify below.
            connection.execute(
                "INSERT INTO checkpoint_writes(thread_id,checkpoint_ns,checkpoint_id,task_id,"
                "idx,channel,type,blob,task_path) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (identity.thread_id(session), namespace, checkpoint, task, idx, channel, kind, value, ""),
            )
        for item in snapshot.checkpoints:
            actual = saver.get_tuple(remap(item.config))
            expected_parent = remap(item.parent_config) if item.parent_config else None
            if (actual is None or actual.checkpoint != item.checkpoint
                    or actual.parent_config != expected_parent or actual.metadata != item.metadata
                    or actual.pending_writes != item.pending_writes):
                raise MigrationError("检查点、父关系或中间写入核验不一致，已回滚本用户全部导入")
        for trip in snapshot.trips:
            saved = connection.execute(
                'SELECT data FROM trips WHERE user_id=%s AND draft_id=%s',
                (*scope, trip["id"]),
            ).fetchone()
            if not saved or saved["data"] != trip:
                raise MigrationError("行程回读核验失败，已回滚本用户全部导入")
        summary = snapshot.summary()
        connection.execute(
            'INSERT INTO memory_imports(user_id,source_digest,summary) VALUES(%s,%s,%s)',
            (*scope, snapshot.digest, Jsonb(summary)),
        )
    return {"status": "imported", **summary}

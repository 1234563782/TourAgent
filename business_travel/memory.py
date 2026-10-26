"""Memory interface and legacy SQLite adapter for offline tests and migration."""

import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol


class MemoryStore(Protocol):
    def read(self) -> dict: ...
    def save_trip(self, draft: dict): ...


def validate_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise ValueError("User/session ID must contain 1-64 ASCII letters, numbers, underscores or hyphens")
    return value


class TravelMemory:
    def __init__(self, directory: Path, legacy_directory: Path, user_id: str):
        self.user_id = validate_id(user_id)
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "memory.sqlite"
        self.legacy_path = legacy_directory / f"{self.user_id}.json"
        with self._connect() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS trips (user_id TEXT, draft_id TEXT, data TEXT NOT NULL,"
                               " PRIMARY KEY(user_id, draft_id))")

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _legacy(self):
        if not self.legacy_path.exists():
            return {}
        # Fail visibly on damaged files instead of silently replacing existing memories.
        data = json.loads(self.legacy_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Legacy memory must contain a JSON object")
        return data

    def read(self) -> dict:
        legacy = self._legacy()
        with self._connect() as connection:
            trips = connection.execute("SELECT data FROM trips WHERE user_id=? ORDER BY rowid DESC LIMIT 10",
                                       (self.user_id,)).fetchall()
        return {"trip_history": [json.loads(row[0]) for row in trips] + legacy.get("trip_history", [])[-10:]}

    def save_trip(self, draft: dict):
        with self._connect() as connection:
            connection.execute("INSERT OR IGNORE INTO trips(user_id, draft_id, data) VALUES(?,?,?)",
                               (self.user_id, draft["id"], json.dumps(draft, ensure_ascii=False)))

"""评审会话持久化：SQLite（标准库），记录输入、状态与最终报告。"""
from __future__ import annotations

import json
import sqlite3
import time
import threading
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    input TEXT NOT NULL,
    status TEXT NOT NULL,
    report TEXT NOT NULL DEFAULT ''
);
"""


class SessionStore:
    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def create(self, session_id: str, input_json: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions (id, created_at, input, status) VALUES (?, ?, ?, 'running')",
                (session_id, time.time(), input_json),
            )
            self._conn.commit()

    def finish(self, session_id: str, status: str, report_json: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET status = ?, report = ? WHERE id = ?",
                (status, report_json, session_id),
            )
            self._conn.commit()

    def get(self, session_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, created_at, input, status, report FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "created_at": row[1],
            "input": json.loads(row[2]) if row[2] else {},
            "status": row[3],
            "report": json.loads(row[4]) if row[4] else None,
        }

    def list_recent(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, created_at, status FROM sessions ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [{"id": row[0], "created_at": row[1], "status": row[2]} for row in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()

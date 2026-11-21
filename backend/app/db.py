"""SQLite persistence for chat sessions (stdlib sqlite3, no external services)."""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,            -- 'user' or 'assistant'
    content TEXT NOT NULL,
    metadata TEXT,                 -- JSON string, assistant messages only
    created_at TEXT NOT NULL       -- server time, ISO-8601
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages (session_id, id);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    system_prompt TEXT,            -- per-session override, NULL = server default
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sqlite_path_from_url(url: str) -> str:
    """`sqlite:////data/app.db` -> `/data/app.db`, `sqlite:///x.db` -> `x.db`."""
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        raise ValueError("DATABASE_URL must look like sqlite:////data/app.db")
    return url[len(prefix):]


class Database:
    def __init__(self, url: str) -> None:
        self.path = sqlite_path_from_url(url)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            with conn:  # commit on success, rollback on error
                yield conn
        finally:
            conn.close()

    def init(self) -> None:
        """Create the database file, folders and schema (idempotent)."""
        folder = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(folder, exist_ok=True)
        with self._connect() as conn:
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError:
                pass  # e.g. filesystems without shared-memory support
            conn.executescript(SCHEMA)

    def ping(self) -> None:
        with self._connect() as conn:
            conn.execute("SELECT 1").fetchone()

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT session_id, system_prompt FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_history(self, session_id: str, limit: int | None = None) -> list[dict[str, str]]:
        """Oldest-first {role, content} pairs, optionally only the last `limit`."""
        query = "SELECT id, role, content FROM messages WHERE session_id = ?"
        params: list[Any] = [session_id]
        if limit:
            query = f"SELECT * FROM ({query} ORDER BY id DESC LIMIT ?) ORDER BY id ASC"
            params.append(limit)
        else:
            query += " ORDER BY id ASC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in rows]

    def save_exchange(
        self,
        session_id: str,
        system_prompt: str | None,
        user_message: str,
        assistant_message: str,
        metadata: dict[str, Any],
    ) -> None:
        """Atomically store one successful user/assistant turn."""
        ts = now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (session_id, system_prompt, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    system_prompt = COALESCE(excluded.system_prompt, sessions.system_prompt),
                    updated_at = excluded.updated_at
                """,
                (session_id, system_prompt, ts, ts),
            )
            conn.execute(
                "INSERT INTO messages (session_id, role, content, metadata, created_at)"
                " VALUES (?, 'user', ?, NULL, ?)",
                (session_id, user_message, ts),
            )
            conn.execute(
                "INSERT INTO messages (session_id, role, content, metadata, created_at)"
                " VALUES (?, 'assistant', ?, ?, ?)",
                (session_id, assistant_message, json.dumps(metadata), now_iso()),
            )

    def list_messages(self, session_id: str) -> list[dict[str, Any]] | None:
        """Full stored history (oldest first) or None if the session is unknown."""
        if self.get_session(session_id) is None:
            return None
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT role, content, metadata FROM messages WHERE session_id = ? ORDER BY id ASC",
                (session_id,),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            item: dict[str, Any] = {"role": row["role"], "content": row["content"]}
            if row["role"] == "assistant" and row["metadata"]:
                item["metadata"] = json.loads(row["metadata"])
            out.append(item)
        return out

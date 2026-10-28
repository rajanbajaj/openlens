"""File-backed cache backend on stdlib sqlite3 (no extra dependencies)."""

from __future__ import annotations

import os
import sqlite3
import threading
import time

from openlens.cache.base import CacheProvider

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cache_entries (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    expires_at REAL NOT NULL
)
"""


class SqliteCacheProvider(CacheProvider):
    """One-row-per-key table; expiry enforced on read, swept on write."""

    def __init__(self, path: str) -> None:
        if not isinstance(path, str) or not path:
            raise ValueError("SqliteCacheProvider 'path' must be a non-empty string.")
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        self._path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        with self._lock:
            self._conn.execute(_SCHEMA)
            self._conn.commit()

    def get(self, key: str) -> str | None:
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT value, expires_at FROM cache_entries WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                return None
            value, expires_at = row
            if expires_at <= now:
                self._conn.execute("DELETE FROM cache_entries WHERE key = ?", (key,))
                self._conn.commit()
                return None
            return str(value)

    def set(self, key: str, value: str, ttl_s: int) -> None:
        if ttl_s <= 0:
            return
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO cache_entries (key, value, expires_at)"
                " VALUES (?, ?, ?)",
                (key, value, time.time() + ttl_s),
            )
            self._conn.execute(
                "DELETE FROM cache_entries WHERE expires_at <= ?", (time.time(),)
            )
            self._conn.commit()

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM cache_entries WHERE key = ?", (key,))
            self._conn.commit()

    def clear(self) -> int:
        with self._lock:
            cursor = self._conn.execute("DELETE FROM cache_entries")
            self._conn.commit()
            return cursor.rowcount if cursor.rowcount >= 0 else 0

    def health_check(self) -> bool:
        try:
            with self._lock:
                self._conn.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    def close(self) -> None:
        try:
            with self._lock:
                self._conn.close()
        except sqlite3.Error:  # close is best-effort teardown only
            return

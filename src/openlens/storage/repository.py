"""SQLite FTS5 document repository.

Standard-library only (`sqlite3`). Stores documents in a `documents` table
with a companion FTS5 index for full-text retrieval.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any

from openlens.core.interfaces import Document, ProviderError

_PROVIDER_NAME = "custom_index"


class DocumentRepository:
    """Persist and search documents via SQLite FTS5."""

    def __init__(self, db_path: str = ":memory:") -> None:
        # check_same_thread=False + a lock: the API serves requests from a
        # server/TestClient worker thread while the connection is created at
        # startup. All access is serialized through the lock, which is the
        # documented safe pattern for sqlite3.
        self._lock = threading.Lock()
        try:
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._init_schema()
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to open document store: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    url TEXT,
                    metadata TEXT NOT NULL DEFAULT '{}'
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
                    doc_id UNINDEXED, title, content
                );
                """
            )
            self._conn.commit()

    def add_document(self, doc: Document) -> None:
        self.add_documents([doc])

    def add_documents(self, docs: list[Document]) -> int:
        try:
            with self._lock, self._conn:
                for doc in docs:
                    metadata = json.dumps(doc.metadata or {})
                    self._conn.execute(
                        "INSERT OR REPLACE INTO documents (id, title, content, url, metadata)"
                        " VALUES (?, ?, ?, ?, ?)",
                        (doc.id, doc.title, doc.content, doc.url, metadata),
                    )
                    self._conn.execute(
                        "DELETE FROM documents_fts WHERE doc_id = ?", (doc.id,)
                    )
                    self._conn.execute(
                        "INSERT INTO documents_fts (doc_id, title, content)"
                        " VALUES (?, ?, ?)",
                        (doc.id, doc.title, doc.content),
                    )
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to index documents: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc
        return len(docs)

    def get(self, doc_id: str) -> Document | None:
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT id, title, content, url, metadata FROM documents WHERE id = ?",
                    (doc_id,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to fetch document: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc
        return _row_to_document(row) if row else None

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()
        return int(row["n"])

    def search(
        self, fts_query: str, limit: int = 10, offset: int = 0
    ) -> tuple[list[tuple[Document, float]], int]:
        """Full-text search; returns ((doc, score) page, total matches).

        Scores use `-bm25(...)` so higher is better.
        """
        if not fts_query.strip():
            return [], 0
        try:
            with self._lock:
                total_row = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM documents_fts WHERE documents_fts MATCH ?",
                    (fts_query,),
                ).fetchone()
                total = int(total_row["n"])
                rows = self._conn.execute(
                    "SELECT d.id, d.title, d.content, d.url, d.metadata,"
                    " -bm25(documents_fts) AS score"
                    " FROM documents_fts"
                    " JOIN documents d ON d.id = documents_fts.doc_id"
                    " WHERE documents_fts MATCH ?"
                    " ORDER BY score DESC LIMIT ? OFFSET ?",
                    (fts_query, limit, offset),
                ).fetchall()
        except sqlite3.Error as exc:
            raise ProviderError(
                f"search failed: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc
        return [(_row_to_document(r), float(r["score"])) for r in rows], total

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _row_to_document(row: sqlite3.Row) -> Document:
    try:
        metadata: dict[str, Any] = json.loads(row["metadata"] or "{}")
    except (ValueError, TypeError):
        metadata = {}
    return Document(
        id=row["id"],
        title=row["title"],
        content=row["content"],
        url=row["url"],
        metadata=metadata,
    )

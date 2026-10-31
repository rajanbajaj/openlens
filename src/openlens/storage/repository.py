"""SQLite FTS5 document repository.

Standard-library only (`sqlite3`). Stores documents in a `documents` table
with a companion FTS5 index for full-text retrieval.
"""

from __future__ import annotations

import datetime
import email.utils
import hashlib
import json
import sqlite3
import threading
from typing import Any, Optional
from urllib.parse import urldefrag, urlsplit

from openlens.core.interfaces import Document, ProviderError
from openlens.storage.base import DEFAULT_CHUNK_SIZE, DocumentStore

_PROVIDER_NAME = "custom_index"

#: Max URL rows scanned when aggregating domains (bounded full scan).
_DOMAIN_SCAN_CAP = 50000

#: Bounded wait for the SQLite write lock before surfacing UNAVAILABLE.
_BUSY_TIMEOUT_MS = 5000


def content_hash_of(content: str) -> str:
    """Canonical content hash: sha256 over the stored content bytes."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def published_from_last_modified(value: Any) -> Optional[str]:
    """Normalize a stored ``last_modified`` value to ISO ``YYYY-MM-DD``.

    Mirrors ``indexing/crawler.py::published_from_last_modified`` without
    importing the indexing package: ISO dates pass through, HTTP dates are
    parsed via ``email.utils``. Unparseable values yield ``None``.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if len(text) == 10:
        try:
            return datetime.date.fromisoformat(text).isoformat()
        except ValueError:
            pass
    try:
        parsed = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed is None:
        return None
    return parsed.date().isoformat()


def doc_columns(doc: Document) -> tuple[Any, Any]:
    """Extract the promoted indexed columns from a Document's metadata.

    Only crawler-truth keys are promoted: ``content_type`` (always emitted
    by the crawler) and ``published`` (derived from ``last-modified`` at
    ingest, falling back to a normalized ``last_modified`` here for
    hand-built documents). No vertical/product/hotel/book/recipe fields
    exist in crawler output, so none are promoted.
    """
    metadata = doc.metadata or {}
    content_type = metadata.get("content_type")
    if not isinstance(content_type, str) or not content_type:
        content_type = None
    published = metadata.get("published")
    if not isinstance(published, str) or not published:
        published = published_from_last_modified(metadata.get("last_modified"))
    return content_type, published


#: Site-category values admitted by the ``site_category`` CHECK set.
SITE_CATEGORY_VALUES: tuple[str, ...] = ("shopping", "news", "kids", "other")


def site_category_columns(
    doc: Document,
) -> tuple[str, float | None, str | None, int, int]:
    """Extract the promoted site-category columns from metadata.

    Unknown or missing categories collapse to ``"other"`` (the column
    default); confidence/source stay NULL when absent; domain hits
    coerce bools/ints and default to 0. Never raises.
    """
    metadata = doc.metadata or {}
    category = metadata.get("site_category")
    if category not in SITE_CATEGORY_VALUES:
        category = "other"
    confidence = metadata.get("site_category_confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        confidence = None
    else:
        confidence = float(confidence)
    source = metadata.get("site_category_source")
    if not isinstance(source, str) or not source:
        source = None

    def _hit(key: str) -> int:
        value = metadata.get(key)
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        return 0

    return (
        category,
        confidence,
        source,
        _hit("kids_domain_hit"),
        _hit("news_domain_hit"),
    )


def normalize_url(url: Optional[str]) -> Optional[str]:
    """Ingest-time URL normalization.

    Uses exactly the query-time rule from
    `retrieval/deduplicator.py::dedupe_key` (strip, drop `#fragment`,
    lowercase, strip trailing `/`) so ingest-time dedup can never
    disagree with query-time dedup. Fragments never change server
    content, so `page#section` and `page` share one identity.
    `None` stays `None` (SQLite UNIQUE indexes admit multiple NULLs).
    """
    if url is None:
        return None
    bare, _ = urldefrag(url.strip())
    return bare.lower().rstrip("/")


class DocumentRepository(DocumentStore):
    """Persist and search documents via SQLite FTS5.

    Reference implementation of ``storage/base.py::DocumentStore``.
    """

    def __init__(self, db_path: str = ":memory:") -> None:
        # check_same_thread=False + a lock: the API serves requests from a
        # server/TestClient worker thread while the connection is created at
        # startup. All access is serialized through the lock, which is the
        # documented safe pattern for sqlite3.
        self._lock = threading.Lock()
        self._closed = False
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
            # WAL mode so readers don't block the single writer; the lock
            # stays (it is the documented safe pattern for a shared
            # sqlite3 handle) but is held for milliseconds, not batch
            # durations. PRAGMAs run before any transaction opens.
            # (On :memory: databases journal_mode stays "memory": harmless.)
            self._conn.execute("PRAGMA journal_mode=WAL")
            # S608 is a false positive here: the only interpolated value
            # is the integer module constant above, never user input.
            self._conn.execute(  # noqa: S608
                f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}"
            )
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    url TEXT,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    content_hash TEXT,
                    url_norm TEXT,
                    content_type TEXT,
                    published TEXT,
                    site_category TEXT DEFAULT 'other'
                        CHECK (site_category IN
                            ('shopping', 'news', 'kids', 'other')),
                    site_category_confidence REAL,
                    site_category_source TEXT,
                    kids_domain_hit INTEGER DEFAULT 0,
                    news_domain_hit INTEGER DEFAULT 0
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
                    doc_id UNINDEXED, title, content
                );
                """
            )
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Idempotent migration: content_hash/url_norm columns, backfill, indexes.

        Safe on a fresh DB (columns already exist, nothing to backfill), on a
        pre-hash DB (ALTER TABLE adds the columns, existing rows are backfilled
        with the canonical hash/normalization), and on a second run (columns
        exist, only still-NULL rows are touched, indexes use IF NOT EXISTS).
        Must be called with the lock held.
        """
        columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(documents)").fetchall()
        }
        if "content_hash" not in columns:
            self._conn.execute("ALTER TABLE documents ADD COLUMN content_hash TEXT")
        if "url_norm" not in columns:
            self._conn.execute("ALTER TABLE documents ADD COLUMN url_norm TEXT")
        if "content_type" not in columns:
            self._conn.execute("ALTER TABLE documents ADD COLUMN content_type TEXT")
        if "published" not in columns:
            self._conn.execute("ALTER TABLE documents ADD COLUMN published TEXT")
        # Category columns: plain ADD COLUMN on the migrate path (the
        # fresh DDL above carries the CHECK set; legacy rows are
        # normalized by the backfill below, so no rebuild is needed).
        if "site_category" not in columns:
            self._conn.execute(
                "ALTER TABLE documents ADD COLUMN site_category TEXT DEFAULT 'other'"
            )
        if "site_category_confidence" not in columns:
            self._conn.execute(
                "ALTER TABLE documents ADD COLUMN site_category_confidence REAL"
            )
        if "site_category_source" not in columns:
            self._conn.execute(
                "ALTER TABLE documents ADD COLUMN site_category_source TEXT"
            )
        if "kids_domain_hit" not in columns:
            self._conn.execute(
                "ALTER TABLE documents ADD COLUMN kids_domain_hit INTEGER DEFAULT 0"
            )
        if "news_domain_hit" not in columns:
            self._conn.execute(
                "ALTER TABLE documents ADD COLUMN news_domain_hit INTEGER DEFAULT 0"
            )
        rows = self._conn.execute(
            "SELECT id, content, url, metadata FROM documents "
            "WHERE content_hash IS NULL OR (url_norm IS NULL AND url IS NOT NULL)"
            " OR content_type IS NULL OR published IS NULL"
            " OR site_category_source IS NULL"
        ).fetchall()
        for row in rows:
            try:
                backfill_meta: dict[str, Any] = json.loads(row["metadata"] or "{}")
            except (ValueError, TypeError):
                backfill_meta = {}
            if not isinstance(backfill_meta, dict):
                backfill_meta = {}
            backfill_type = backfill_meta.get("content_type")
            if not isinstance(backfill_type, str) or not backfill_type:
                backfill_type = None
            backfill_pub = backfill_meta.get("published")
            if not isinstance(backfill_pub, str) or not backfill_pub:
                backfill_pub = published_from_last_modified(
                    backfill_meta.get("last_modified")
                )
            backfill_category = backfill_meta.get("site_category")
            if backfill_category not in SITE_CATEGORY_VALUES:
                backfill_category = "other"
            backfill_conf = backfill_meta.get("site_category_confidence")
            if isinstance(backfill_conf, bool) or not isinstance(
                backfill_conf, (int, float)
            ):
                backfill_conf = None
            else:
                backfill_conf = float(backfill_conf)
            backfill_source = backfill_meta.get("site_category_source")
            if not isinstance(backfill_source, str) or not backfill_source:
                backfill_source = None

            backfill_hits = []
            for hit_key in ("kids_domain_hit", "news_domain_hit"):
                hit_value = backfill_meta.get(hit_key)
                if isinstance(hit_value, bool):
                    backfill_hits.append(int(hit_value))
                elif isinstance(hit_value, int):
                    backfill_hits.append(hit_value)
                else:
                    backfill_hits.append(0)
            self._conn.execute(
                "UPDATE documents SET content_hash = ?, url_norm = ?,"
                " content_type = COALESCE(?, content_type),"
                " published = COALESCE(?, published),"
                " site_category = ?, site_category_confidence = ?,"
                " site_category_source = ?, kids_domain_hit = ?,"
                " news_domain_hit = ? WHERE id = ?",
                (
                    content_hash_of(row["content"]),
                    normalize_url(row["url"]),
                    backfill_type,
                    backfill_pub,
                    backfill_category,
                    backfill_conf,
                    backfill_source,
                    backfill_hits[0],
                    backfill_hits[1],
                    row["id"],
                ),
            )
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_url_norm "
            "ON documents(url_norm)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_documents_content_hash "
            "ON documents(content_hash)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_documents_content_type "
            "ON documents(content_type)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_documents_published ON documents(published)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_documents_site_category "
            "ON documents(site_category)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_documents_kids_domain_hit "
            "ON documents(kids_domain_hit)"
        )

    def add_document(self, doc: Document) -> None:
        self.add_documents([doc])

    def add_documents(
        self, docs: list[Document], chunk_size: int = DEFAULT_CHUNK_SIZE
    ) -> int:
        """Index documents, one transaction per ``chunk_size`` chunk.

        Returns the total indexed. Each chunk commits independently; no
        cross-chunk atomicity is promised.
        """
        if chunk_size <= 0:
            raise ValueError(f"chunk_size must be positive, got {chunk_size}")
        total = 0
        for start in range(0, len(docs), chunk_size):
            chunk = docs[start : start + chunk_size]
            try:
                with self._lock, self._conn:
                    for doc in chunk:
                        metadata = json.dumps(doc.metadata or {})
                        content_type, published = doc_columns(doc)
                        category_columns = site_category_columns(doc)
                        # S608 is a false positive here: every value below
                        # is a bound ? parameter; no user input is
                        # interpolated.
                        self._conn.execute(
                            "INSERT OR REPLACE INTO documents "  # noqa: S608
                            + "(id, title, content, url, metadata,"
                            " content_hash, url_norm, content_type, published,"
                            " site_category, site_category_confidence,"
                            " site_category_source, kids_domain_hit,"
                            " news_domain_hit)"
                            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                doc.id,
                                doc.title,
                                doc.content,
                                doc.url,
                                metadata,
                                content_hash_of(doc.content),
                                normalize_url(doc.url),
                                content_type,
                                published,
                                *category_columns,
                            ),
                        )
                        self._conn.execute(
                            "DELETE FROM documents_fts WHERE doc_id = ?",
                            (doc.id,),
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
            total += len(chunk)
        return total

    def update_document(self, doc: Document) -> bool:
        """Rewrite a document (and its FTS row) by id. False when missing."""
        try:
            with self._lock, self._conn:
                metadata = json.dumps(doc.metadata or {})
                content_type, published = doc_columns(doc)
                category_columns = site_category_columns(doc)
                cursor = self._conn.execute(
                    "UPDATE documents SET title = ?, content = ?, url = ?,"
                    " metadata = ?, content_hash = ?, url_norm = ?,"
                    " content_type = ?, published = ?,"
                    " site_category = ?, site_category_confidence = ?,"
                    " site_category_source = ?, kids_domain_hit = ?,"
                    " news_domain_hit = ?"
                    " WHERE id = ?",
                    (
                        doc.title,
                        doc.content,
                        doc.url,
                        metadata,
                        content_hash_of(doc.content),
                        normalize_url(doc.url),
                        content_type,
                        published,
                        *category_columns,
                        doc.id,
                    ),
                )
                if cursor.rowcount == 0:
                    return False
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
                f"failed to update document: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc
        return True

    def delete(self, doc_id: str) -> bool:
        """Remove a document and its FTS row. False when missing."""
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "DELETE FROM documents_fts WHERE doc_id = ?", (doc_id,)
                )
                cursor = self._conn.execute(
                    "DELETE FROM documents WHERE id = ?", (doc_id,)
                )
                if cursor.rowcount == 0:
                    return False
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to delete document: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc
        return True

    def delete_document(self, doc_id: str) -> bool:
        """Alias of :meth:`delete` (kept for the indexing flows)."""
        return self.delete(doc_id)

    def get_hash(self, doc_id: str) -> Optional[str]:
        """Return the stored content hash, or None when missing/unmigrated."""
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT content_hash FROM documents WHERE id = ?",
                    (doc_id,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to fetch document hash: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc
        if row is None:
            return None
        value: Any = row["content_hash"]
        return str(value) if value is not None else None

    def hash_exists(self, content_hash: str) -> bool:
        """True when any document carries this content hash."""
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT 1 AS found FROM documents WHERE content_hash = ? LIMIT 1",
                    (content_hash,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to look up content hash: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc
        return row is not None

    def get(self, doc_id: str) -> Document | None:
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT id, title, content, url, metadata, "
                    + "content_type, published, site_category, "
                    + "site_category_confidence, site_category_source, "
                    + "kids_domain_hit, news_domain_hit "
                    + "FROM documents WHERE id = ?",
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

    def list_domains(self, limit: int = 100) -> list[tuple[str, int]]:
        """(host, doc count) pairs, most-indexed first (read-only).

        Hosts parse from stored URLs in Python (SQLite has no URL
        parser); the scan is capped so a large index stays bounded.
        """
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            return []
        if isinstance(limit, bool) or limit <= 0:
            return []
        limit = min(limit, 1000)
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT url FROM documents WHERE url IS NOT NULL"  # noqa: S608
                    f" AND url != '' LIMIT {_DOMAIN_SCAN_CAP}",
                ).fetchall()
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to list domains: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc
        counts: dict[str, int] = {}
        for row in rows:
            try:
                host = (urlsplit(row["url"]).hostname or "").lower()
            except (ValueError, AttributeError):
                continue
            if host:
                counts[host] = counts.get(host, 0) + 1
        ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        return ranked[:limit]

    def list_documents(self, limit: int = 500, offset: int = 0) -> list[Document]:
        """Return one page of documents in stable id order (export seam)."""
        if limit <= 0 or offset < 0:
            return []
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT id, title, content, url, metadata,"
                    " content_type, published, site_category,"
                    " site_category_confidence, site_category_source,"
                    " kids_domain_hit, news_domain_hit FROM documents"
                    " ORDER BY id ASC LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to list documents: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc
        return [_row_to_document(row) for row in rows]

    def list_titles(self, limit: int = 500) -> list[str]:
        """Return up to ``limit`` document titles (suggest-vocabulary source).

        Best-effort read for the query-assistance path: a closed store or
        any SQLite failure yields ``[]`` rather than raising, so suggest
        degrades to its static fallback instead of failing the request.
        """
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit <= 0
            or self._closed
        ):
            return []
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT title FROM documents LIMIT ?", (limit,)
                ).fetchall()
        except Exception:
            return []
        return [str(row["title"]) for row in rows if row["title"]]

    def search(
        self,
        fts_query: str,
        limit: int = 10,
        offset: int = 0,
        site_category: str | None = None,
    ) -> tuple[list[tuple[Document, float]], int]:
        """Full-text search; returns ((doc, score) page, total matches).

        Scores use `-bm25(...)` so higher is better. ``site_category``
        narrows both rows and total to one indexed category value.
        """
        if not fts_query.strip():
            return [], 0
        category = site_category if site_category in SITE_CATEGORY_VALUES else None
        try:
            with self._lock:
                # S608 false positive: fts_query is a bound ? parameter;
                # the narrowing fragment is a static string, never input.
                if category is None:
                    total_row = self._conn.execute(
                        "SELECT COUNT(*) AS n FROM documents_fts "  # noqa: S608
                        + "WHERE documents_fts MATCH ?",
                        (fts_query,),
                    ).fetchone()
                else:
                    total_row = self._conn.execute(
                        "SELECT COUNT(*) AS n FROM documents_fts "  # noqa: S608
                        + "JOIN documents d ON d.id = documents_fts.doc_id "
                        + "WHERE documents_fts MATCH ? AND d.site_category = ?",
                        (fts_query, category),
                    ).fetchone()
                total = int(total_row["n"])
                rows = self._conn.execute(
                    "SELECT d.id, d.title, d.content, d.url, d.metadata,"  # noqa: S608
                    " d.content_type, d.published, d.site_category,"
                    " d.site_category_confidence, d.site_category_source,"
                    " d.kids_domain_hit, d.news_domain_hit,"
                    " -bm25(documents_fts) AS score"
                    " FROM documents_fts"
                    " JOIN documents d ON d.id = documents_fts.doc_id"
                    " WHERE documents_fts MATCH ?"
                    + (" AND d.site_category = ?" if category is not None else "")
                    + " ORDER BY score DESC LIMIT ? OFFSET ?",
                    (fts_query, limit, offset)
                    if category is None
                    else (fts_query, category, limit, offset),
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
            if self._closed:
                return
            self._closed = True
            self._conn.close()


def _row_to_document(row: sqlite3.Row) -> Document:
    try:
        metadata: dict[str, Any] = json.loads(row["metadata"] or "{}")
    except (ValueError, TypeError):
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    # Promoted indexed columns stay authoritative: backfill the metadata
    # view so readers see one shape whether the row predates the columns.
    try:
        column_type = row["content_type"]
    except (IndexError, KeyError):
        column_type = None
    try:
        column_published = row["published"]
    except (IndexError, KeyError):
        column_published = None
    if isinstance(column_type, str) and column_type:
        metadata.setdefault("content_type", column_type)
    if isinstance(column_published, str) and column_published:
        metadata.setdefault("published", column_published)
    # Promoted category columns fill gaps in the metadata view, but only
    # with informative values: default verdicts ('other', no confidence,
    # no hits) stay out so hand-built documents round-trip verbatim.
    # An invalid stored category is normalized to the column value.
    try:
        column_category = row["site_category"]
        column_confidence = row["site_category_confidence"]
        column_source = row["site_category_source"]
        column_kids_hit = row["kids_domain_hit"]
        column_news_hit = row["news_domain_hit"]
    except (IndexError, KeyError):
        column_category = None
        column_confidence = None
        column_source = None
        column_kids_hit = None
        column_news_hit = None
    if column_category in SITE_CATEGORY_VALUES:
        current_category = metadata.get("site_category")
        if current_category not in SITE_CATEGORY_VALUES and (
            current_category is not None or column_category != "other"
        ):
            metadata["site_category"] = column_category
    if (
        isinstance(column_confidence, (int, float))
        and not isinstance(column_confidence, bool)
        and "site_category_confidence" not in metadata
    ):
        metadata["site_category_confidence"] = float(column_confidence)
    if (
        isinstance(column_source, str)
        and column_source
        and "site_category_source" not in metadata
    ):
        metadata["site_category_source"] = column_source
    if (
        isinstance(column_kids_hit, int)
        and not isinstance(column_kids_hit, bool)
        and column_kids_hit != 0
        and "kids_domain_hit" not in metadata
    ):
        metadata["kids_domain_hit"] = column_kids_hit
    if (
        isinstance(column_news_hit, int)
        and not isinstance(column_news_hit, bool)
        and column_news_hit != 0
        and "news_domain_hit" not in metadata
    ):
        metadata["news_domain_hit"] = column_news_hit
    return Document(
        id=row["id"],
        title=row["title"],
        content=row["content"],
        url=row["url"],
        metadata=metadata,
    )

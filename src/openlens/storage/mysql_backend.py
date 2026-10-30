"""MySQL document store (``DocumentStore`` contract over MySQL/InnoDB).

Same abstract contract as ``storage/base.py::DocumentStore`` with the
SQLite reference implementation in ``storage/repository.py``. Schema setup
creates the ``id`` primary key plus a ``FULLTEXT`` index on
``(title, content)``; review with ``SHOW INDEX FROM <table>``, not just
behavior.

Connection fields (``host``/``port``/``database``/``user``/``password``)
are plain values: resolve ``${ENV_VAR}`` through ``load_config`` in
``src/openlens/config.py`` first (conventional env names
``OPENLENS_MYSQL_HOST`` / ``OPENLENS_MYSQL_PORT`` /
``OPENLENS_MYSQL_DATABASE`` / ``OPENLENS_MYSQL_USER`` /
``OPENLENS_MYSQL_PASSWORD``), then pass the resolved mapping to
:meth:`MySQLDocumentStore.from_config`.

The driver (``pymysql``) is lazy-imported inside the constructor path, so
this module imports cleanly without the driver installed; instantiating
without the driver raises ``ProviderError(UNAVAILABLE)``, never a raw
``ImportError``. Connection failures at construction likewise surface as
``ProviderError(UNAVAILABLE)``, never raw driver exceptions.

Concurrency: a bounded connection pool (``pool_size`` connections created
at construction, blocking checkout with ``checkout_timeout``). This
deliberately does NOT copy SQLite's single-connection + global-lock
pattern. ``close()`` drains and closes every pooled connection and is
idempotent.

Lossy FTS translation (documented, per plan — cross-engine query
normalization is a non-goal):

- The input is the already-parsed FTS-style query string produced by
  ``retrieval/query_parser.parse_query`` (SQLite/FTS5 shaped).
- It is passed verbatim into ``MATCH(title, content) AGAINST (%s IN
  NATURAL LANGUAGE MODE)``. FTS5 phrase quotes, column filters, boolean
  operators, and ``*`` prefixes are NOT honored — in natural-language
  mode they are treated as ordinary words/punctuation.
- Ranking is MySQL's native relevance (higher is better, same direction
  as SQLite's ``-bm25``) but scores are engine-local: never compare them
  across backends. Only document id *sets* are expected to agree.
- Tokenization differs: MySQL stopwords and ``ft_min_word_len``
  (default 4) / ``innodb_ft_min_token_size`` (default 3) may drop short
  or common tokens that SQLite matches, so id sets can differ on such
  queries. Blank queries return ``([], 0)`` without touching the server.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import queue
import re
import threading
import urllib.parse
from collections.abc import Iterator, Mapping
from typing import Any

from openlens.core.interfaces import Document, ProviderError
from openlens.storage.base import DEFAULT_CHUNK_SIZE, DocumentStore
from openlens.storage.repository import doc_columns

_PROVIDER_NAME = "custom_index"

#: Optional-driver import name. Never imported at module top level.
_DRIVER_MODULE = "pymysql"

#: ``table`` names are interpolated into DDL, so only safe identifiers pass.
_TABLE_RE = re.compile(r"[A-Za-z0-9_]+")


def _load_driver() -> Any:
    """Import the MySQL driver lazily; missing driver -> ProviderError."""
    try:
        return importlib.import_module(_DRIVER_MODULE)
    except ImportError as exc:
        raise ProviderError(
            "MySQL driver 'pymysql' is not installed; "
            "install the mysql extra to use the MySQL backend",
            provider_name=_PROVIDER_NAME,
            code=ProviderError.UNAVAILABLE,
        ) from exc


def _check_table(table: str) -> str:
    if not table or _TABLE_RE.fullmatch(table) is None:
        raise ValueError(f"invalid table name: {table!r}")
    return table


def _doc_to_row(
    doc: Document,
) -> tuple[str, str, str, str | None, str, str | None, str | None]:
    content_type, published = doc_columns(doc)
    return (
        doc.id,
        doc.title,
        doc.content,
        doc.url,
        json.dumps(doc.metadata or {}),
        content_type,
        published,
    )


def _row_to_document(row: Mapping[str, Any]) -> Document:
    try:
        metadata: dict[str, Any] = json.loads(row["metadata"] or "{}")
    except (ValueError, TypeError):
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    # Promoted indexed columns stay authoritative: surface them on the
    # metadata view when the stored JSON predates the columns.
    content_type = row.get("content_type") if isinstance(row, Mapping) else None
    if isinstance(content_type, str) and content_type:
        metadata.setdefault("content_type", content_type)
    published = row.get("published") if isinstance(row, Mapping) else None
    if isinstance(published, str) and published:
        metadata.setdefault("published", published)
    return Document(
        id=str(row["id"]),
        title=str(row["title"]),
        content=str(row["content"]),
        url=row["url"],
        metadata=metadata,
    )


class MySQLDocumentStore(DocumentStore):
    """Persist and search documents via MySQL FULLTEXT."""

    def __init__(
        self,
        *,
        host: str = "localhost",
        port: int = 3306,
        database: str = "openlens",
        user: str = "root",
        password: str = "",
        table: str = "documents",
        pool_size: int = 5,
        connect_timeout: int = 5,
        checkout_timeout: float = 5.0,
        charset: str = "utf8mb4",
    ) -> None:
        if pool_size <= 0:
            raise ValueError(f"pool_size must be positive, got {pool_size}")
        self._table = _check_table(table)
        self._schema = database
        self._checkout_timeout = checkout_timeout
        self._connect_kwargs: dict[str, Any] = {
            "host": host,
            "port": int(port),
            "database": database,
            "user": user,
            "password": password,
            "charset": charset,
            "connect_timeout": connect_timeout,
        }
        self._state_lock = threading.Lock()
        self._closed = False
        # Eager pool: construction proves connectivity, so an
        # unreachable-but-configured DB fails here (UNAVAILABLE), not
        # mid-request. The driver import below is the lazy seam: it is
        # the first driver touch in this module.
        self._pool: queue.Queue[Any] = queue.Queue()
        try:
            for _ in range(pool_size):
                self._pool.put(self._connect())
            self._init_schema()
        except ProviderError:
            self._drain()
            raise

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "MySQLDocumentStore":
        """Build a store from load_config-resolved ``storage.mysql`` fields.

        ``database`` also accepts the ``db`` alias. Ports and sizes may be
        strings (YAML/env resolution yields text); they are coerced here.
        """
        database = config.get("database", config.get("db", "openlens"))
        kwargs: dict[str, Any] = {
            "host": config.get("host", "localhost"),
            "port": int(config.get("port", 3306)),
            "database": database,
            "user": config.get("user", "root"),
            "password": config.get("password", ""),
        }
        for key in ("table", "charset"):
            if key in config:
                kwargs[key] = config[key]
        for key in ("pool_size", "connect_timeout"):
            if key in config:
                kwargs[key] = int(config[key])
        if "checkout_timeout" in config:
            kwargs["checkout_timeout"] = float(config["checkout_timeout"])
        return cls(**kwargs)

    @classmethod
    def from_dsn(cls, dsn: str, **overrides: Any) -> "MySQLDocumentStore":
        """Build a store from ``mysql://user:pass@host:port/database``."""
        parts = urllib.parse.urlparse(dsn)
        if parts.scheme != "mysql":
            raise ValueError(f"unsupported DSN scheme: {parts.scheme!r}")
        kwargs: dict[str, Any] = {
            "host": parts.hostname or "localhost",
            "port": parts.port or 3306,
            "database": (parts.path or "/openlens").lstrip("/") or "openlens",
            "user": urllib.parse.unquote(parts.username or "root"),
            "password": urllib.parse.unquote(parts.password or ""),
        }
        kwargs.update(overrides)
        return cls(**kwargs)

    # -- connection handling ------------------------------------------

    def _connect(self) -> Any:
        driver = _load_driver()
        try:
            return driver.connect(
                cursorclass=driver.cursors.DictCursor,
                **self._connect_kwargs,
            )
        except Exception as exc:
            raise ProviderError(
                f"failed to connect to MySQL at {self._connect_kwargs['host']}"
                f":{self._connect_kwargs['port']}: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    @contextlib.contextmanager
    def _connection(self) -> Iterator[Any]:
        with self._state_lock:
            closed = self._closed
        if closed:
            raise ProviderError(
                "MySQL document store is closed",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            )
        try:
            conn = self._pool.get(timeout=self._checkout_timeout)
        except queue.Empty as exc:
            raise ProviderError(
                "MySQL connection pool exhausted",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc
        try:
            yield conn
        except Exception:
            self._replace(conn)
            raise
        else:
            self._release(conn)

    def _release(self, conn: Any) -> None:
        with self._state_lock:
            closed = self._closed
        if closed:
            with contextlib.suppress(Exception):
                conn.close()
        else:
            self._pool.put(conn)

    def _replace(self, conn: Any) -> None:
        """Discard a (possibly broken) connection; keep the pool bounded."""
        with contextlib.suppress(Exception):
            conn.close()
        with self._state_lock:
            closed = self._closed
        if closed:
            return
        with contextlib.suppress(Exception):
            self._pool.put(self._connect())

    def _drain(self) -> None:
        while True:
            try:
                conn = self._pool.get_nowait()
            except queue.Empty:
                return
            with contextlib.suppress(Exception):
                conn.close()

    # -- schema --------------------------------------------------------

    def _init_schema(self) -> None:
        # The table name passed the identifier check in _check_table;
        # every value below is a bound %s parameter, never interpolated.
        ddl = (
            "CREATE TABLE IF NOT EXISTS `" + self._table + "` ("
            "`id` VARCHAR(191) PRIMARY KEY,"
            " `title` TEXT NOT NULL,"
            " `content` MEDIUMTEXT NOT NULL,"
            " `url` TEXT NULL,"
            " `metadata` MEDIUMTEXT NOT NULL,"
            " `content_type` TEXT NULL,"
            " `published` TEXT NULL,"
            " FULLTEXT KEY `ftx_title_content` (`title`, `content`),"
            " KEY `idx_content_type` (`content_type`(191)),"
            " KEY `idx_published` (`published`(10))"
            ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
        )
        with self._connection() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(ddl)
                    self._migrate(cur)
                conn.commit()
            except Exception as exc:
                with contextlib.suppress(Exception):
                    conn.rollback()
                raise ProviderError(
                    f"failed to initialize MySQL schema: {exc}",
                    provider_name=_PROVIDER_NAME,
                    code=ProviderError.UNAVAILABLE,
                ) from exc

    # -- DocumentStore contract ----------------------------------------

    def _migrate(self, cur: Any) -> None:
        """Idempotent migration: crawler-truth columns + indexes.

        Safe on a fresh table (DDL already has the columns) and on a
        pre-existing table (missing columns/indexes are added). Existing
        rows keep their stored metadata JSON; new writes fill the
        columns going forward.
        """
        cur.execute(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS"
            " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
            (self._table,),
        )
        existing: set[str] = set()
        for item in cur.fetchall():
            if isinstance(item, Mapping):
                name = item.get("COLUMN_NAME", item.get("column_name", ""))
            else:
                name = item[0]
            existing.add(str(name).lower())
        if "content_type" not in existing:
            cur.execute(
                "ALTER TABLE `" + self._table + "`"  # noqa: S608
                " ADD COLUMN `content_type` TEXT NULL"
            )
        if "published" not in existing:
            cur.execute(
                "ALTER TABLE `" + self._table + "`"  # noqa: S608
                " ADD COLUMN `published` TEXT NULL"
            )
        cur.execute(
            "SELECT INDEX_NAME FROM INFORMATION_SCHEMA.STATISTICS"
            " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
            (self._table,),
        )
        indexes: set[str] = set()
        for item in cur.fetchall():
            if isinstance(item, Mapping):
                name = item.get("INDEX_NAME", item.get("index_name", ""))
            else:
                name = item[0]
            indexes.add(str(name).lower())
        if "idx_content_type" not in indexes:
            cur.execute(
                "CREATE INDEX `idx_content_type` ON `"  # noqa: S608
                + self._table
                + "` (`content_type`(191))"
            )
        if "idx_published" not in indexes:
            cur.execute(
                "CREATE INDEX `idx_published` ON `"  # noqa: S608
                + self._table
                + "` (`published`(10))"
            )

    def add_document(self, doc: Document) -> None:
        self.add_documents([doc])

    def add_documents(
        self, docs: list[Document], chunk_size: int = DEFAULT_CHUNK_SIZE
    ) -> int:
        """Index documents, one transaction per ``chunk_size`` chunk."""
        if chunk_size <= 0:
            raise ValueError(f"chunk_size must be positive, got {chunk_size}")
        rows = [_doc_to_row(doc) for doc in docs]
        total = 0
        for start in range(0, len(rows), chunk_size):
            chunk = rows[start : start + chunk_size]
            # Validated table name; every value is a bound %s parameter.
            sql = (
                "INSERT INTO `" + self._table + "`"  # noqa: S608
                " (id, title, content, url, metadata, content_type, published)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)"
                " ON DUPLICATE KEY UPDATE title = VALUES(title),"
                " content = VALUES(content), url = VALUES(url),"
                " metadata = VALUES(metadata),"
                " content_type = VALUES(content_type),"
                " published = VALUES(published)"
            )
            with self._connection() as conn:
                try:
                    with conn.cursor() as cur:
                        cur.executemany(sql, chunk)
                    conn.commit()
                except Exception as exc:
                    with contextlib.suppress(Exception):
                        conn.rollback()
                    raise ProviderError(
                        f"failed to index documents: {exc}",
                        provider_name=_PROVIDER_NAME,
                        code=ProviderError.UNAVAILABLE,
                    ) from exc
            total += len(chunk)
        return total

    def delete(self, doc_id: str) -> bool:
        with self._connection() as conn:
            try:
                with conn.cursor() as cur:
                    # Validated table name; doc_id is a bound %s parameter.
                    cur.execute(
                        "DELETE FROM `" + self._table + "` WHERE id = %s",  # noqa: S608
                        (doc_id,),
                    )
                    removed = bool(cur.rowcount != 0)
                conn.commit()
            except Exception as exc:
                with contextlib.suppress(Exception):
                    conn.rollback()
                raise ProviderError(
                    f"failed to delete document: {exc}",
                    provider_name=_PROVIDER_NAME,
                    code=ProviderError.UNAVAILABLE,
                ) from exc
        return removed

    def get(self, doc_id: str) -> Document | None:
        with self._connection() as conn:
            try:
                with conn.cursor() as cur:
                    # Validated table name; doc_id is a bound %s parameter.
                    cur.execute(
                        "SELECT id, title, content, url, metadata,"  # noqa: S608
                        " content_type, published"
                        " FROM `" + self._table + "` WHERE id = %s",
                        (doc_id,),
                    )
                    row = cur.fetchone()
            except Exception as exc:
                raise ProviderError(
                    f"failed to fetch document: {exc}",
                    provider_name=_PROVIDER_NAME,
                    code=ProviderError.UNAVAILABLE,
                ) from exc
        return _row_to_document(row) if row is not None else None

    def count(self) -> int:
        with self._connection() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT COUNT(*) AS n FROM `" + self._table + "`"  # noqa: S608
                    )
                    row = cur.fetchone()
            except Exception as exc:
                raise ProviderError(
                    f"failed to count documents: {exc}",
                    provider_name=_PROVIDER_NAME,
                    code=ProviderError.UNAVAILABLE,
                ) from exc
        return int(row["n"])

    def list_documents(self, limit: int = 500, offset: int = 0) -> list[Document]:
        """Return one page of documents in stable id order (export seam)."""
        if limit <= 0 or offset < 0:
            return []
        with self._connection() as conn:
            try:
                with conn.cursor() as cur:
                    # Validated table name; limit/offset are bound parameters.
                    cur.execute(
                        "SELECT id, title, content, url, metadata,"  # noqa: S608
                        " content_type, published"
                        " FROM `" + self._table + "` ORDER BY id ASC"
                        " LIMIT %s OFFSET %s",
                        (limit, offset),
                    )
                    rows = cur.fetchall()
            except Exception as exc:
                raise ProviderError(
                    f"failed to list documents: {exc}",
                    provider_name=_PROVIDER_NAME,
                    code=ProviderError.UNAVAILABLE,
                ) from exc
        return [_row_to_document(row) for row in rows]

    def search(
        self, fts_query: str, limit: int = 10, offset: int = 0
    ) -> tuple[list[tuple[Document, float]], int]:
        """Full-text search; returns ((doc, score) page, total matches)."""
        if not fts_query.strip():
            return [], 0
        match_against = "MATCH(title, content) AGAINST (%s IN NATURAL LANGUAGE MODE)"
        # Validated table name; the query text and paging values below are
        # bound %s parameters, never interpolated.
        count_sql = (
            "SELECT COUNT(*) AS n FROM `"  # noqa: S608
            + self._table
            + "` WHERE "
            + match_against
        )
        page_sql = (
            "SELECT id, title, content, url, metadata, content_type, published, "  # noqa: S608
            + match_against
            + " AS score FROM `"
            + self._table
            + "` WHERE "
            + match_against
            + " ORDER BY score DESC LIMIT %s OFFSET %s"
        )
        with self._connection() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(count_sql, (fts_query,))
                    total = int(cur.fetchone()["n"])
                    cur.execute(page_sql, (fts_query, fts_query, limit, offset))
                    rows = cur.fetchall()
            except Exception as exc:
                raise ProviderError(
                    f"search failed: {exc}",
                    provider_name=_PROVIDER_NAME,
                    code=ProviderError.UNAVAILABLE,
                ) from exc
        return [(_row_to_document(r), float(r["score"])) for r in rows], total

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        self._drain()

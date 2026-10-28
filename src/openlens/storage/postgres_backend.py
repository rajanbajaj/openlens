"""Postgres document store (``DocumentStore`` contract over Postgres).

Same abstract contract as ``storage/base.py::DocumentStore`` with the
SQLite reference implementation in ``storage/repository.py``. Schema setup
creates the ``id`` primary key plus a ``search_tsv`` tsvector column with
a GIN index; review with ``pg_indexes``/``EXPLAIN``, not just behavior.

Connection fields (``host``/``port``/``dbname``/``user``/``password``,
optional ``schema``/``table``) are plain values: resolve ``${ENV_VAR}``
through ``load_config`` in ``src/openlens/config.py`` first (conventional
env names ``OPENLENS_PG_HOST`` / ``OPENLENS_PG_PORT`` /
``OPENLENS_PG_DBNAME`` / ``OPENLENS_PG_USER`` / ``OPENLENS_PG_PASSWORD``),
then pass the resolved mapping to
:meth:`PostgresDocumentStore.from_config`. A ``postgresql://`` DSN string
is also accepted via :meth:`PostgresDocumentStore.from_dsn` (the test
gate ``OPENLENS_TEST_PG_DSN`` uses that form).

The driver (``psycopg`` v3) is lazy-imported inside the connection path,
so this module imports cleanly without the driver installed;
instantiating without the driver raises
``ProviderError(UNAVAILABLE)``, never a raw ``ImportError``. Connection
failures at construction likewise surface as
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
- FTS5 phrase quotes, ``*`` prefixes, ``^`` boosts, ``:`` column
  filters, parentheses, and the ``AND``/``OR``/``NOT``/``NEAR`` keywords
  are stripped before the query reaches Postgres; the remaining text is
  passed as a single parameter to ``plainto_tsquery('english', %s)``.
  ``plainto_tsquery`` ANDs the surviving terms, so an FTS5 ``OR`` query
  silently becomes an AND, and stop-word-only or operator-only queries
  match nothing (blank/empty translations return ``([], 0)`` without
  touching the server).
- Ranking is Postgres ``ts_rank`` (higher is better, same direction as
  SQLite's ``-bm25``) but scores are engine-local: never compare them
  across backends. Only document id *sets* are expected to agree.
- Tokenization differs: the ``english`` stemming/dictionary may drop or
  conflate tokens that SQLite matches, so id sets can differ on such
  queries.

Metadata is stored in a ``JSONB`` column and read back as a parsed dict
(the driver may return ``dict`` or ``str``; both are normalized, and
anything else falls back to ``{}`` so conformance compares parsed dicts,
not raw storage).
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

_PROVIDER_NAME = "custom_index"

#: Optional-driver import name. Never imported at module top level.
_DRIVER_MODULE = "psycopg"

#: ``schema``/``table`` names are interpolated into DDL, so only safe
#: identifiers pass (must start with a letter or underscore).
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: FTS5 surface syntax with no plainto_tsquery equivalent — stripped by
#: the lossy translation below.
_FTS_OPERATOR_RE = re.compile(r'["*():^]|\b(?:AND|OR|NOT|NEAR)\b', re.IGNORECASE)


def _load_driver() -> Any:
    """Import the Postgres driver lazily; missing driver -> ProviderError."""
    try:
        return importlib.import_module(_DRIVER_MODULE)
    except ImportError as exc:
        raise ProviderError(
            "Postgres driver 'psycopg' is not installed; "
            "install the postgres extra to use the Postgres backend",
            provider_name=_PROVIDER_NAME,
            code=ProviderError.UNAVAILABLE,
        ) from exc


def _check_identifier(name: str, kind: str) -> str:
    if not name or _IDENTIFIER_RE.fullmatch(name) is None:
        raise ValueError(f"invalid {kind} name: {name!r}")
    return name


def _to_tsquery_text(fts_query: str) -> str:
    """Lossy FTS5-shaped query -> plainto_tsquery input (see docstring)."""
    text = _FTS_OPERATOR_RE.sub(" ", fts_query)
    return re.sub(r"\s+", " ", text).strip()


def _doc_to_row(doc: Document) -> tuple[str, str, str, str | None, str]:
    return (doc.id, doc.title, doc.content, doc.url, json.dumps(doc.metadata or {}))


def _row_to_document(row: tuple[Any, ...]) -> Document:
    raw_metadata = row[4]
    metadata: dict[str, Any]
    if isinstance(raw_metadata, dict):
        metadata = raw_metadata
    else:
        try:
            parsed: Any = json.loads(raw_metadata or "{}")
        except (ValueError, TypeError):
            parsed = {}
        metadata = parsed if isinstance(parsed, dict) else {}
    return Document(
        id=str(row[0]),
        title=str(row[1]),
        content=str(row[2]),
        url=row[3],
        metadata=metadata,
    )


class PostgresDocumentStore(DocumentStore):
    """Persist and search documents via Postgres tsvector + GIN."""

    def __init__(
        self,
        *,
        host: str = "localhost",
        port: int = 5432,
        dbname: str = "openlens",
        user: str = "postgres",
        password: str = "",
        schema: str = "public",
        table: str = "documents",
        pool_size: int = 5,
        connect_timeout: int = 5,
        checkout_timeout: float = 5.0,
    ) -> None:
        if pool_size <= 0:
            raise ValueError(f"pool_size must be positive, got {pool_size}")
        self._schema = _check_identifier(schema, "schema")
        self._table = _check_identifier(table, "table")
        self._checkout_timeout = checkout_timeout
        self._connect_kwargs: dict[str, Any] = {
            "host": host,
            "port": int(port),
            "dbname": dbname,
            "user": user,
            "password": password,
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
    def from_config(cls, config: Mapping[str, Any]) -> "PostgresDocumentStore":
        """Build a store from load_config-resolved ``storage.postgres`` fields.

        ``dbname`` also accepts the ``database``/``db`` aliases. Ports and
        sizes may be strings (YAML/env resolution yields text); they are
        coerced here.
        """
        dbname = config.get(
            "dbname", config.get("database", config.get("db", "openlens"))
        )
        kwargs: dict[str, Any] = {
            "host": config.get("host", "localhost"),
            "port": int(config.get("port", 5432)),
            "dbname": dbname,
            "user": config.get("user", "postgres"),
            "password": config.get("password", ""),
        }
        for key in ("schema", "table"):
            if key in config:
                kwargs[key] = config[key]
        for key in ("pool_size", "connect_timeout"):
            if key in config:
                kwargs[key] = int(config[key])
        if "checkout_timeout" in config:
            kwargs["checkout_timeout"] = float(config["checkout_timeout"])
        return cls(**kwargs)

    @classmethod
    def from_dsn(cls, dsn: str, **overrides: Any) -> "PostgresDocumentStore":
        """Build a store from ``postgresql://user:pass@host:port/dbname``."""
        parts = urllib.parse.urlparse(dsn)
        if parts.scheme not in ("postgresql", "postgres"):
            raise ValueError(f"unsupported DSN scheme: {parts.scheme!r}")
        kwargs: dict[str, Any] = {
            "host": parts.hostname or "localhost",
            "port": parts.port or 5432,
            "dbname": (parts.path or "/openlens").lstrip("/") or "openlens",
            "user": urllib.parse.unquote(parts.username or "postgres"),
            "password": urllib.parse.unquote(parts.password or ""),
        }
        kwargs.update(overrides)
        return cls(**kwargs)

    # -- connection handling ------------------------------------------

    def _connect(self) -> Any:
        driver = _load_driver()
        try:
            return driver.connect(**self._connect_kwargs)
        except Exception as exc:
            raise ProviderError(
                f"failed to connect to Postgres at {self._connect_kwargs['host']}"
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
                "Postgres document store is closed",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            )
        try:
            conn = self._pool.get(timeout=self._checkout_timeout)
        except queue.Empty as exc:
            raise ProviderError(
                "Postgres connection pool exhausted",
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

    def _qualified(self) -> str:
        return f'"{self._schema}"."{self._table}"'

    # -- schema --------------------------------------------------------

    def _init_schema(self) -> None:
        # S608 is a false positive here: schema/table passed the
        # identifier check; every value is a bound %s parameter.
        qualified = self._qualified()
        index_name = f"idx_{self._table}_search_tsv"
        ddl_table = (
            f'CREATE SCHEMA IF NOT EXISTS "{self._schema}";'  # noqa: S608
            f" CREATE TABLE IF NOT EXISTS {qualified} ("
            "id TEXT PRIMARY KEY,"
            " title TEXT NOT NULL,"
            " content TEXT NOT NULL,"
            " url TEXT NULL,"
            " metadata JSONB NOT NULL DEFAULT '{}'::jsonb,"
            " search_tsv tsvector"
            ")"
        )
        ddl_index = (
            f'CREATE INDEX IF NOT EXISTS "{index_name}"'  # noqa: S608
            f" ON {qualified} USING GIN (search_tsv)"
        )
        backfill = (
            f"UPDATE {qualified} SET search_tsv ="  # noqa: S608
            " to_tsvector('english', coalesce(title, '')"
            " || ' ' || coalesce(content, ''))"
            " WHERE search_tsv IS NULL"
        )
        with self._connection() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(ddl_table)
                    cur.execute(ddl_index)
                    cur.execute(backfill)
                conn.commit()
            except Exception as exc:
                with contextlib.suppress(Exception):
                    conn.rollback()
                raise ProviderError(
                    f"failed to initialize Postgres schema: {exc}",
                    provider_name=_PROVIDER_NAME,
                    code=ProviderError.UNAVAILABLE,
                ) from exc

    # -- DocumentStore contract ----------------------------------------

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
            # S608 is a false positive here: identifiers passed the
            # check; every value is a bound %s parameter.
            sql = (
                f"INSERT INTO {self._qualified()}"  # noqa: S608
                " (id, title, content, url, metadata, search_tsv)"
                " VALUES (%s, %s, %s, %s, %s::jsonb,"
                " to_tsvector('english', coalesce(%s, '')"
                " || ' ' || coalesce(%s, '')))"
                " ON CONFLICT (id) DO UPDATE SET title = EXCLUDED.title,"
                " content = EXCLUDED.content, url = EXCLUDED.url,"
                " metadata = EXCLUDED.metadata,"
                " search_tsv = EXCLUDED.search_tsv"
            )
            params = [(r[0], r[1], r[2], r[3], r[4], r[1], r[2]) for r in chunk]
            with self._connection() as conn:
                try:
                    with conn.cursor() as cur:
                        cur.executemany(sql, params)
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
                    # S608 false positive: validated identifiers, bound %s.
                    cur.execute(
                        f"DELETE FROM {self._qualified()} WHERE id = %s",  # noqa: S608
                        (doc_id,),
                    )
                    removed = cur.rowcount not in (0, None)
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
                    # S608 false positive: validated identifiers, bound %s.
                    cur.execute(
                        f"SELECT id, title, content, url, metadata"  # noqa: S608
                        f" FROM {self._qualified()} WHERE id = %s",
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
                    # S608 false positive: validated identifiers only.
                    sql = f"SELECT COUNT(*) FROM {self._qualified()}"  # noqa: S608
                    cur.execute(sql)
                    row = cur.fetchone()
            except Exception as exc:
                raise ProviderError(
                    f"failed to count documents: {exc}",
                    provider_name=_PROVIDER_NAME,
                    code=ProviderError.UNAVAILABLE,
                ) from exc
        return int(row[0])

    def search(
        self, fts_query: str, limit: int = 10, offset: int = 0
    ) -> tuple[list[tuple[Document, float]], int]:
        """Full-text search; returns ((doc, score) page, total matches)."""
        ts_text = _to_tsquery_text(fts_query)
        if not ts_text:
            return [], 0
        match = "search_tsv @@ plainto_tsquery('english', %s)"
        rank = "ts_rank(search_tsv, plainto_tsquery('english', %s))"
        # S608 false positives below: validated identifiers; the query
        # text and paging values are bound %s parameters.
        count_sql = f"SELECT COUNT(*) FROM {self._qualified()} WHERE {match}"  # noqa: S608
        page_sql = (
            f"SELECT id, title, content, url, metadata,"  # noqa: S608
            f" {rank} AS score FROM {self._qualified()}"
            f" WHERE {match}"
            " ORDER BY score DESC LIMIT %s OFFSET %s"
        )
        with self._connection() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(count_sql, (ts_text,))
                    row = cur.fetchone()
                    total = int(row[0])
                    cur.execute(page_sql, (ts_text, ts_text, limit, offset))
                    rows = cur.fetchall()
            except Exception as exc:
                raise ProviderError(
                    f"search failed: {exc}",
                    provider_name=_PROVIDER_NAME,
                    code=ProviderError.UNAVAILABLE,
                ) from exc
        return [(_row_to_document(r), float(r[5])) for r in rows], total

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        self._drain()

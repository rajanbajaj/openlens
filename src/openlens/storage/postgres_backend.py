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
import time
import urllib.parse
from collections.abc import Iterator, Mapping
from typing import Any

from openlens.core.interfaces import Document, ProviderError
from openlens.storage.base import DEFAULT_CHUNK_SIZE, DocumentStore
from openlens.storage.frontier import ordered_postgres_frontier_schema
from openlens.storage.repository import (
    SITE_CATEGORY_VALUES,
    content_hash_of,
    description_of,
    doc_columns,
    document_link_rows,
    fetch_ms_column,
    site_category_columns,
)

#: Category columns appended (in order) to document SELECTs for metadata
#: backfill. Mirrors ``storage/repository.py::_row_to_document``.
CATEGORY_SELECT = (
    "site_category, site_category_confidence, site_category_source,"
    " kids_domain_hit, news_domain_hit"
)

#: Seconds before a failed replica pool build is retried. Reads fall
#: back to the primary in between, so a down replica never blocks.
_REPLICA_RETRY_S = 30.0

#: Consecutive mid-use replica failures before the replica is dropped
#: (reads return to primary until the next retry window). A replica
#: that connects but fails queries — e.g. recovery conflicts — must
#: not take search down with it.
_REPLICA_MAX_FAILURES = 3


def _backfill_category_metadata(
    metadata: dict[str, Any], values: Any
) -> dict[str, Any]:
    """Fill category gaps from promoted columns (sqlite-mapper parity).

    ``values`` is the 5-tuple in ``CATEGORY_SELECT`` order; short/None
    input backfills nothing. Only informative values land: default
    verdicts (``'other'``, no confidence, no hits) stay out.
    """
    try:
        category, confidence, source, kids_hit, news_hit = list(values)[:5]
    except (TypeError, ValueError):
        return metadata
    if category in SITE_CATEGORY_VALUES:
        current = metadata.get("site_category")
        if current not in SITE_CATEGORY_VALUES and (
            current is not None or category != "other"
        ):
            metadata["site_category"] = category
    if (
        isinstance(confidence, (int, float))
        and not isinstance(confidence, bool)
        and "site_category_confidence" not in metadata
    ):
        metadata["site_category_confidence"] = float(confidence)
    if isinstance(source, str) and source and "site_category_source" not in metadata:
        metadata["site_category_source"] = source
    if (
        isinstance(kids_hit, int)
        and not isinstance(kids_hit, bool)
        and kids_hit != 0
        and "kids_domain_hit" not in metadata
    ):
        metadata["kids_domain_hit"] = kids_hit
    if (
        isinstance(news_hit, int)
        and not isinstance(news_hit, bool)
        and news_hit != 0
        and "news_domain_hit" not in metadata
    ):
        metadata["news_domain_hit"] = news_hit
    return metadata


#: Short TTL (seconds) for the ``list_domains`` instance cache:
#: repeats within the window skip the GROUP BY query entirely.
#: Cached ranks may be stale (documented on ``list_domains``).
_DOMAINS_CACHE_TTL_S = 30.0

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


def _doc_to_row(
    doc: Document,
) -> tuple[Any, ...]:
    content_type, published = doc_columns(doc)
    category, confidence, source, kids_hit, news_hit = site_category_columns(doc)
    return (
        doc.id,
        doc.title,
        doc.content,
        doc.url,
        json.dumps(doc.metadata or {}),
        content_type,
        published,
        content_hash_of(doc.content),
        category,
        confidence,
        source,
        kids_hit,
        news_hit,
        fetch_ms_column(doc),
        # Trailing tsvector input (index 14): meta description folded
        # into ``search_tsv`` so description-only terms match.
        description_of(doc),
    )


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
    # Promoted indexed columns (positions 5/6 when selected) stay
    # authoritative: surface them on the metadata view when the stored
    # JSON predates the columns.
    if len(row) > 6:
        content_type, published = row[5], row[6]
        if isinstance(content_type, str) and content_type:
            metadata.setdefault("content_type", content_type)
        if isinstance(published, str) and published:
            metadata.setdefault("published", published)
    if len(row) > 11:
        _backfill_category_metadata(metadata, row[7:12])
    if len(row) > 12:
        fetch_ms = row[12]
        if (
            isinstance(fetch_ms, (int, float))
            and not isinstance(fetch_ms, bool)
            and "fetch_ms" not in metadata
        ):
            metadata["fetch_ms"] = float(fetch_ms)
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
        replica: Mapping[str, Any] | None = None,
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
        # Optional read replica: search/suggest reads go here while
        # writes stay on the primary, so crawl write pressure cannot
        # starve search. Best-effort — the pool builds lazily and any
        # failure degrades to the primary (see ``_read_connection``).
        # A mapping without a non-empty ``host`` disables the replica.
        self._replica_kwargs: dict[str, Any] | None = None
        self._replica_pool_size = pool_size
        if isinstance(replica, Mapping):
            replica_host = str(replica.get("host") or "").strip()
            if replica_host:
                self._replica_kwargs = {
                    "host": replica_host,
                    "port": int(replica.get("port", 5432)),
                    "dbname": str(
                        replica.get("dbname", replica.get("database", dbname))
                    ),
                    "user": str(replica.get("user", user)),
                    "password": str(replica.get("password", password)),
                    "connect_timeout": int(
                        replica.get("connect_timeout", connect_timeout)
                    ),
                }
                self._replica_pool_size = int(replica.get("pool_size", pool_size))
                if self._replica_pool_size <= 0:
                    raise ValueError(
                        "replica pool_size must be positive, got "
                        f"{self._replica_pool_size}"
                    )
        self._replica_pool: queue.Queue[Any] | None = None
        self._replica_retry_at = 0.0
        self._replica_consec_failures = 0
        self._state_lock = threading.Lock()
        self._closed = False
        # Short-TTL cache for list_domains: limit -> (cached_at, ranks).
        # Best-effort only; entries expire after _DOMAINS_CACHE_TTL_S.
        self._domains_cache: dict[int, tuple[float, list[tuple[str, int]]]] = {}
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
        replica = config.get("replica")
        if isinstance(replica, Mapping):
            # Empty host disables the replica (zero behavior change for
            # existing deploys); user/password/dbname fall back to the
            # primary's when omitted.
            replica_host = str(replica.get("host") or "").strip()
            if replica_host:
                replica_kwargs: dict[str, Any] = {
                    "host": replica_host,
                    "port": int(replica.get("port", kwargs.get("port", 5432))),
                    "dbname": str(
                        replica.get(
                            "dbname",
                            replica.get("database", kwargs.get("dbname")),
                        )
                    ),
                    "user": str(replica.get("user", kwargs.get("user", "postgres"))),
                    "password": str(
                        replica.get("password", kwargs.get("password", ""))
                    ),
                }
                for key in ("pool_size", "connect_timeout"):
                    if key in replica:
                        replica_kwargs[key] = int(replica[key])
                kwargs["replica"] = replica_kwargs
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
        return self._connect_to(self._connect_kwargs)

    def _connect_to(self, connect_kwargs: dict[str, Any]) -> Any:
        driver = _load_driver()
        try:
            return driver.connect(**connect_kwargs)
        except Exception as exc:
            raise ProviderError(
                f"failed to connect to Postgres at {connect_kwargs['host']}"
                f":{connect_kwargs['port']}: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def _ensure_replica_pool(self) -> queue.Queue[Any] | None:
        """Return the replica pool, building it lazily (None when unusable).

        The pool builds on first read — never at construction — so the
        backend boots while the replica is still basebacking-up or down.
        Build failures throttle to one attempt per ``_REPLICA_RETRY_S``;
        reads use the primary in between.
        """
        if self._replica_kwargs is None:
            return None
        pool = self._replica_pool
        if pool is not None:
            return pool
        now = time.monotonic()
        if now < self._replica_retry_at:
            return None
        try:
            pool = queue.Queue()
            for _ in range(self._replica_pool_size):
                pool.put(self._connect_to(self._replica_kwargs))
        except Exception:
            self._replica_retry_at = now + _REPLICA_RETRY_S
            return None
        self._replica_pool = pool
        return pool

    def _drop_replica_pool(self) -> None:
        """Discard the replica pool; reads return to primary until retry."""
        pool, self._replica_pool = self._replica_pool, None
        self._replica_retry_at = time.monotonic() + _REPLICA_RETRY_S
        if pool is None:
            return
        while True:
            try:
                conn = pool.get_nowait()
            except queue.Empty:
                return
            with contextlib.suppress(Exception):
                conn.close()

    @contextlib.contextmanager
    def _read_connection(self) -> Iterator[Any]:
        """Checkout for read-only methods: replica first, primary fallback.

        No replica configured (or still unreachable) degrades to the
        primary path, so behavior is identical to ``_connection``. A
        saturated replica also falls back instead of queueing reads
        behind write pressure on the wrong host. Mid-use replica errors
        propagate after discarding the connection; after
        ``_REPLICA_MAX_FAILURES`` consecutive failures the pool is
        dropped so a sick replica stops failing searches.
        """
        # Test doubles may bypass __init__ (no replica attrs): degrade to
        # the primary path instead of raising, mirroring list_domains.
        if getattr(self, "_replica_kwargs", None) is None:
            with self._connection() as conn:
                yield conn
            return
        with self._state_lock:
            closed = self._closed
        if closed:
            raise ProviderError(
                "Postgres document store is closed",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            )
        pool = self._ensure_replica_pool()
        if pool is None:
            with self._connection() as conn:
                yield conn
            return
        try:
            conn = pool.get(timeout=self._checkout_timeout)
        except queue.Empty:
            with self._connection() as conn:
                yield conn
            return
        try:
            yield conn
        except Exception:
            with contextlib.suppress(Exception):
                conn.close()
            self._replica_consec_failures += 1
            if self._replica_consec_failures >= _REPLICA_MAX_FAILURES:
                self._drop_replica_pool()
            raise
        else:
            self._replica_consec_failures = 0
            self._release_to(pool, conn)

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
        self._release_to(self._pool, conn)

    def _release_to(self, pool: queue.Queue[Any], conn: Any) -> None:
        # End any read transaction before recycling: psycopg leaves
        # successful SELECTs "idle in transaction", and the lingering
        # AccessShare locks wedge all later DDL (boot migrations take
        # AccessExclusive), freezing every request on the event loop.
        # Rollback is a no-op for already-committed writers.
        with contextlib.suppress(Exception):
            conn.rollback()
        with self._state_lock:
            closed = self._closed
        if closed:
            with contextlib.suppress(Exception):
                conn.close()
        else:
            pool.put(conn)

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
            " search_tsv tsvector,"
            " content_type TEXT NULL,"
            " published TEXT NULL"
            ")"
        )
        ddl_index = (
            f'CREATE INDEX IF NOT EXISTS "{index_name}"'  # noqa: S608
            f" ON {qualified} USING GIN (search_tsv)"
        )
        ddl_content_type = (
            f"ALTER TABLE {qualified} ADD COLUMN IF NOT EXISTS content_type TEXT"  # noqa: S608
        )
        ddl_published = (
            f"ALTER TABLE {qualified} ADD COLUMN IF NOT EXISTS published TEXT"  # noqa: S608
        )
        ddl_content_hash = (
            f"ALTER TABLE {qualified} ADD COLUMN IF NOT EXISTS content_hash TEXT"  # noqa: S608
        )
        # Category columns predate this migration on SQLite; on Postgres
        # they arrive here so category SELECTs work on existing databases.
        # The indexer still carries the verdict in the metadata JSON, so
        # NULL columns simply backfill nothing (sqlite-mapper parity).
        ddl_category = (
            f"ALTER TABLE {qualified}"  # noqa: S608
            " ADD COLUMN IF NOT EXISTS site_category TEXT,"
            " ADD COLUMN IF NOT EXISTS site_category_confidence REAL,"
            " ADD COLUMN IF NOT EXISTS site_category_source TEXT,"
            " ADD COLUMN IF NOT EXISTS kids_domain_hit INTEGER,"
            " ADD COLUMN IF NOT EXISTS news_domain_hit INTEGER"
        )
        ddl_fetch_ms = (
            f"ALTER TABLE {qualified}"  # noqa: S608
            " ADD COLUMN IF NOT EXISTS fetch_ms DOUBLE PRECISION"
        )
        links_qualified = f'"{self._schema}"."document_links"'
        ddl_links_table = (
            f"CREATE TABLE IF NOT EXISTS {links_qualified} ("  # noqa: S608
            " source_id TEXT NOT NULL,"
            " source_host TEXT NOT NULL DEFAULT '',"
            " target TEXT NOT NULL,"
            " target_host TEXT NOT NULL DEFAULT '')"
        )
        ddl_links_target_idx = (
            'CREATE INDEX IF NOT EXISTS "idx_document_links_target"'  # noqa: S608
            f" ON {links_qualified} (target)"
        )
        ddl_links_source_idx = (
            'CREATE INDEX IF NOT EXISTS "idx_document_links_source"'  # noqa: S608
            f" ON {links_qualified} (source_id)"
        )
        ddl_fetch_ms_idx = (
            f'CREATE INDEX IF NOT EXISTS "idx_{self._table}_fetch_ms"'  # noqa: S608
            f" ON {qualified} (fetch_ms)"
        )
        ddl_content_hash_idx = (
            f'CREATE INDEX IF NOT EXISTS "idx_{self._table}_content_hash"'  # noqa: S608
            f" ON {qualified} (content_hash)"
        )
        ddl_content_type_idx = (
            f'CREATE INDEX IF NOT EXISTS "idx_{self._table}_content_type"'  # noqa: S608
            f" ON {qualified} (content_type)"
        )
        ddl_published_idx = (
            f'CREATE INDEX IF NOT EXISTS "idx_{self._table}_published"'  # noqa: S608
            f" ON {qualified} (published)"
        )
        site_category_idx = f"idx_{self._table}_site_category"
        ddl_site_category_idx = (
            f'CREATE INDEX IF NOT EXISTS "{site_category_idx}"'  # noqa: S608
            f" ON {qualified} (site_category)"
        )
        backfill = (
            f"UPDATE {qualified} SET search_tsv ="  # noqa: S608
            " to_tsvector('english', coalesce(title, '')"
            " || ' ' || coalesce(content, '')"
            " || ' ' || coalesce(metadata->>'description', ''))"
            " WHERE search_tsv IS NULL"
        )
        with self._connection() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(ddl_table)
                    # Tables, then coordination migrations, then indexes:
                    # the raw DDL list puts idx_frontier_owner before the
                    # ADD COLUMN migration, which fails on pre-coordination
                    # databases with column "owner" does not exist.
                    for stmt in ordered_postgres_frontier_schema(self._schema):
                        cur.execute(stmt)
                    cur.execute(ddl_index)
                    cur.execute(ddl_content_type)
                    cur.execute(ddl_published)
                    cur.execute(ddl_content_hash)
                    cur.execute(ddl_category)
                    cur.execute(ddl_fetch_ms)
                    cur.execute(ddl_links_table)
                    cur.execute(ddl_links_target_idx)
                    cur.execute(ddl_links_source_idx)
                    cur.execute(ddl_fetch_ms_idx)
                    cur.execute(ddl_content_type_idx)
                    cur.execute(ddl_published_idx)
                    cur.execute(ddl_content_hash_idx)
                    cur.execute(ddl_site_category_idx)
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
                " (id, title, content, url, metadata,"
                " content_type, published, content_hash,"
                " site_category, site_category_confidence,"
                " site_category_source, kids_domain_hit, news_domain_hit,"
                " fetch_ms, search_tsv)"
                " VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s,"
                " %s, %s, %s, %s, %s, %s,"
                " to_tsvector('english', coalesce(%s, '')"
                " || ' ' || coalesce(%s, '')"
                " || ' ' || coalesce(%s, '')))"
                " ON CONFLICT (id) DO UPDATE SET title = EXCLUDED.title,"
                " content = EXCLUDED.content, url = EXCLUDED.url,"
                " metadata = EXCLUDED.metadata,"
                " content_type = EXCLUDED.content_type,"
                " published = EXCLUDED.published,"
                " content_hash = EXCLUDED.content_hash,"
                " site_category = EXCLUDED.site_category,"
                " site_category_confidence = EXCLUDED.site_category_confidence,"
                " site_category_source = EXCLUDED.site_category_source,"
                " kids_domain_hit = EXCLUDED.kids_domain_hit,"
                " news_domain_hit = EXCLUDED.news_domain_hit,"
                " fetch_ms = EXCLUDED.fetch_ms,"
                " search_tsv = EXCLUDED.search_tsv"
            )
            params = [
                (
                    r[0],
                    r[1],
                    r[2],
                    r[3],
                    r[4],
                    r[5],
                    r[6],
                    r[7],
                    r[8],
                    r[9],
                    r[10],
                    r[11],
                    r[12],
                    r[13],
                    r[1],
                    r[2],
                    r[14],
                )
                for r in chunk
            ]
            with self._connection() as conn:
                try:
                    with conn.cursor() as cur:
                        cur.executemany(sql, params)
                        self._sync_document_links(cur, chunk)
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

    def _sync_document_links(self, cur: Any, rows: list[tuple[Any, ...]]) -> None:
        """Replace authority-graph rows for an indexed chunk.

        Delete-then-insert per source id keeps reindex idempotent.
        ``rows`` are :func:`_doc_to_row` tuples (id at 0, url at 3,
        metadata JSON at 4).
        """
        links: list[tuple[Any, ...]] = []
        for row in rows:
            try:
                metadata: Any = json.loads(row[4] or "{}")
            except (ValueError, TypeError):
                metadata = {}
            if not isinstance(metadata, dict):
                metadata = {}
            # Reuse the shared row builder over a light Document view.
            for link in document_link_rows(
                Document(id=row[0], title="", content="", url=row[3], metadata=metadata)
            ):
                links.append(link)
            # S608 false positive: validated identifiers, bound %s.
            cur.execute(
                f'DELETE FROM "{self._schema}"."document_links"'  # noqa: S608
                " WHERE source_id = %s",
                (row[0],),
            )
        if links:
            # S608 false positive: validated identifiers, bound %s.
            cur.executemany(
                f'INSERT INTO "{self._schema}"."document_links"'  # noqa: S608
                " (source_id, source_host, target, target_host)"
                " VALUES (%s, %s, %s, %s)",
                links,
            )

    def inlink_counts(self, targets: list[str]) -> dict[str, int]:
        """Distinct linking hosts per target URL (authority signal source).

        Unknown targets are absent (never zero-filled); any failure
        yields ``{}`` so ranking degrades to neutral.
        """
        wanted = [t for t in targets if isinstance(t, str) and t]
        if not wanted:
            return {}
        with self._read_connection() as conn:
            try:
                with conn.cursor() as cur:
                    placeholders = ",".join(["%s"] * len(wanted))
                    # S608 false positive: validated identifiers, bound %s.
                    cur.execute(
                        f"SELECT target, COUNT(DISTINCT source_host)"  # noqa: S608
                        f' FROM "{self._schema}"."document_links"'
                        f" WHERE target IN ({placeholders}) GROUP BY target",
                        wanted,
                    )
                    rows = cur.fetchall()
            except Exception:
                with contextlib.suppress(Exception):
                    conn.rollback()
                return {}
        return {str(row[0]): int(row[1]) for row in rows}

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
                    cur.execute(
                        f'DELETE FROM "{self._schema}"."document_links"'  # noqa: S608
                        " WHERE source_id = %s",
                        (doc_id,),
                    )
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
        with self._read_connection() as conn:
            try:
                with conn.cursor() as cur:
                    # S608 false positive: validated identifiers, bound %s.
                    cur.execute(
                        f"SELECT id, title, content, url, metadata,"  # noqa: S608
                        f" content_type, published, {CATEGORY_SELECT}, fetch_ms"
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

    def hash_exists(self, content_hash: str) -> bool:
        """True when any document carries this content hash.

        Rows predating the ``content_hash`` migration store NULL and never
        match; the next crawl upserts them by id (which stamps the hash),
        so the column self-heals without a backfill.
        """
        with self._read_connection() as conn:
            try:
                with conn.cursor() as cur:
                    # S608 false positive: validated identifiers, bound %s.
                    cur.execute(
                        f"SELECT 1 FROM {self._qualified()}"  # noqa: S608
                        " WHERE content_hash = %s LIMIT 1",
                        (content_hash,),
                    )
                    row = cur.fetchone()
            except Exception as exc:
                raise ProviderError(
                    f"failed to look up content hash: {exc}",
                    provider_name=_PROVIDER_NAME,
                    code=ProviderError.UNAVAILABLE,
                ) from exc
        return row is not None

    def update_document(self, doc: Document) -> bool:
        """Rewrite a document by id (and its tsvector). False when missing."""
        content_type, published = doc_columns(doc)
        params = (
            doc.title,
            doc.content,
            doc.url,
            json.dumps(doc.metadata or {}),
            content_hash_of(doc.content),
            content_type,
            published,
            doc.title,
            doc.content,
            description_of(doc),
            doc.id,
        )
        with self._connection() as conn:
            try:
                with conn.cursor() as cur:
                    # S608 false positive: validated identifiers, bound %s.
                    cur.execute(
                        f"UPDATE {self._qualified()}"  # noqa: S608
                        " SET title = %s, content = %s, url = %s,"
                        " metadata = %s::jsonb, content_hash = %s,"
                        " content_type = %s, published = %s,"
                        " search_tsv = to_tsvector('english',"
                        " coalesce(%s, '') || ' ' || coalesce(%s, '')"
                        " || ' ' || coalesce(%s, ''))"
                        " WHERE id = %s",
                        params,
                    )
                    updated = bool(cur.rowcount and cur.rowcount > 0)
                conn.commit()
            except Exception as exc:
                with contextlib.suppress(Exception):
                    conn.rollback()
                raise ProviderError(
                    f"failed to update document: {exc}",
                    provider_name=_PROVIDER_NAME,
                    code=ProviderError.UNAVAILABLE,
                ) from exc
        return updated

    def list_domains(self, limit: int = 100) -> list[tuple[str, int]]:
        """(host, doc count) pairs, most-indexed first (read-only).

        Repeats with the same ``limit`` within ``_DOMAINS_CACHE_TTL_S``
        seconds return the cached ranking without querying, so those
        ranks may be stale (writes inside the window stay invisible
        until the entry expires).
        """
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            return []
        if isinstance(limit, bool) or limit <= 0:
            return []
        limit = min(limit, 1000)
        # Test doubles may bypass __init__ (no cache attrs): degrade to
        # the uncached scan instead of raising.
        state_lock = getattr(self, "_state_lock", None)
        domains_cache = getattr(self, "_domains_cache", None)
        if state_lock is not None and domains_cache is not None:
            with state_lock:
                cached = domains_cache.get(limit)
                if (
                    cached is not None
                    and time.monotonic() - cached[0] < _DOMAINS_CACHE_TTL_S
                ):
                    return list(cached[1])
        # S608 false positives below: validated identifiers, a static
        # regexp, and a bound limit — no user input is interpolated.
        sql = (
            f"SELECT lower(regexp_replace(url, '^https?://([^/:?#]+).*$', '\\1'))"  # noqa: S608
            f" AS host, COUNT(*) AS n FROM {self._qualified()}"  # noqa: S608
            " WHERE url LIKE 'http%'"
            " GROUP BY 1 ORDER BY 2 DESC, 1 ASC LIMIT %s"
        )
        with self._read_connection() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(sql, (limit,))
                    rows = cur.fetchall()
            except Exception as exc:
                raise ProviderError(
                    f"failed to list domains: {exc}",
                    provider_name=_PROVIDER_NAME,
                    code=ProviderError.UNAVAILABLE,
                ) from exc
        result = [(str(row[0]), int(row[1])) for row in rows if row[0]]
        if state_lock is not None and domains_cache is not None:
            with state_lock:
                domains_cache[limit] = (time.monotonic(), list(result))
        return result

    def count(self) -> int:
        with self._read_connection() as conn:
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

    def list_documents(self, limit: int = 500, offset: int = 0) -> list[Document]:
        """Return one page of documents in stable id order (export seam)."""
        if limit <= 0 or offset < 0:
            return []
        with self._read_connection() as conn:
            try:
                with conn.cursor() as cur:
                    # S608 false positive: validated identifiers, bound %s.
                    cur.execute(
                        f"SELECT id, title, content, url, metadata,"  # noqa: S608
                        f" content_type, published, {CATEGORY_SELECT}, fetch_ms"
                        f" FROM {self._qualified()} ORDER BY id ASC"
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

    def list_titles(self, limit: int = 500) -> list[str]:
        """Return up to ``limit`` document titles (suggest-vocabulary source).

        Best-effort read for the query-assistance path: a closed store or
        any failure yields ``[]`` rather than raising, so suggest
        degrades to its static fallback instead of failing the request.
        Ordered by title so the vocabulary is deterministic.
        Mirrors ``storage/repository.py::DocumentRepository.list_titles``.
        """
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            return []
        with self._state_lock:
            if self._closed:
                return []
        try:
            with self._read_connection() as conn:
                with conn.cursor() as cur:
                    # S608 false positive: validated identifiers, bound %s.
                    cur.execute(
                        f"SELECT title FROM {self._qualified()}"  # noqa: S608
                        " ORDER BY title ASC LIMIT %s",
                        (limit,),
                    )
                    rows = cur.fetchall()
        except Exception:
            return []
        titles: list[str] = []
        for row in rows:
            if isinstance(row, Mapping):
                value = row.get("title")
            else:
                try:
                    value = row[0]
                except (IndexError, TypeError):
                    continue
            if isinstance(value, str) and value:
                titles.append(value)
        return titles

    def search(
        self,
        fts_query: str,
        limit: int = 10,
        offset: int = 0,
        site_category: str | None = None,
    ) -> tuple[list[tuple[Document, float]], int]:
        """Full-text search; returns ((doc, score) page, total matches).

        ``site_category`` narrows rows and total to one indexed value.
        """
        ts_text = _to_tsquery_text(fts_query)
        if not ts_text:
            return [], 0
        category = site_category if site_category in SITE_CATEGORY_VALUES else None
        match = "search_tsv @@ plainto_tsquery('english', %s)"
        rank = "ts_rank(search_tsv, plainto_tsquery('english', %s))"
        narrowing = " AND site_category = %s" if category is not None else ""
        # S608 false positives below: validated identifiers; the query
        # text, category, and paging values are bound %s parameters.
        count_sql = f"SELECT COUNT(*) FROM {self._qualified()} WHERE {match}{narrowing}"  # noqa: S608
        # Two-phase page: rank narrow (id, score) rows first, then fetch
        # full rows — including TOASTed content/metadata — for the top-N
        # only. Ranking 20k+ matches of a common term while heap-fetching
        # every candidate's content blows past the provider timeout; the
        # inner scan touches (id, search_tsv) alone. Parameter order is
        # unchanged, so existing call sites are untouched.
        page_sql = (
            f"WITH ranked AS (SELECT id, {rank} AS score"  # noqa: S608
            f" FROM {self._qualified()} WHERE {match}{narrowing}"
            " ORDER BY score DESC, id ASC LIMIT %s OFFSET %s)"
            " SELECT documents.id, documents.title, documents.content,"
            " documents.url, documents.metadata, documents.content_type,"
            f" documents.published, {CATEGORY_SELECT}, documents.fetch_ms,"
            " ranked.score"
            f" FROM {self._qualified()} AS documents"
            " JOIN ranked ON ranked.id = documents.id"
            " ORDER BY ranked.score DESC, ranked.id ASC"
        )
        with self._read_connection() as conn:
            try:
                with conn.cursor() as cur:
                    if category is None:
                        cur.execute(count_sql, (ts_text,))
                    else:
                        cur.execute(count_sql, (ts_text, category))
                    row = cur.fetchone()
                    total = int(row[0])
                    if category is None:
                        cur.execute(page_sql, (ts_text, ts_text, limit, offset))
                    else:
                        narrowed = (ts_text, ts_text, category, limit, offset)
                        cur.execute(page_sql, narrowed)
                    rows = cur.fetchall()
            except Exception as exc:
                raise ProviderError(
                    f"search failed: {exc}",
                    provider_name=_PROVIDER_NAME,
                    code=ProviderError.UNAVAILABLE,
                ) from exc
        # Score is the trailing column (index 13 on full rows): the
        # promoted content_type/published/category/fetch_ms columns
        # shifted it. Trailing access keeps short test doubles working.
        return [(_row_to_document(r), float(r[-1])) for r in rows], total

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        self._drain()
        self._drop_replica_pool()

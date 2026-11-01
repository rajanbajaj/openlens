"""Offline unit tests for ``PostgresDocumentStore`` row mapping (no server).

A stub connection stands in for the pool so these run anywhere: they pin
the SELECT column/score contract that the live-skipped conformance suite
in ``tests/integration/test_storage_postgres.py`` cannot guard.
"""

from __future__ import annotations

import queue
import threading
from typing import Any

import pytest

from openlens.core.interfaces import Document, ProviderError
from openlens.storage.postgres_backend import (
    PostgresDocumentStore,
    _doc_to_row,
    _row_to_document,
)


class _StubCursor:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows
        self.executed: list[str] = []
        self.calls: list[tuple[str, Any]] = []

    def __enter__(self) -> "_StubCursor":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append(sql)
        self.calls.append((sql, params))

    def fetchone(self) -> tuple[Any, ...] | None:
        return (len(self._rows),)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _StubConnectionCtx:
    def __init__(self, cursor: _StubCursor) -> None:
        self._cursor = cursor

    def __enter__(self) -> "_StubConnectionCtx":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def cursor(self) -> _StubCursor:
        return self._cursor

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


def _store_with_rows(rows: list[tuple[Any, ...]]) -> PostgresDocumentStore:
    store = PostgresDocumentStore.__new__(PostgresDocumentStore)
    store._schema = "public"
    store._table = "documents"
    cursor = _StubCursor(rows)
    store._connection = lambda: _StubConnectionCtx(cursor)  # type: ignore[method-assign]
    store._stub_cursor = cursor  # type: ignore[attr-defined]
    return store


def test_search_returns_trailing_score_not_content_type() -> None:
    """Score is the trailing column; content_type must not leak into it."""
    rows = [
        (
            "d1",
            "hello world",
            "hello world content",
            "https://x.test/a",
            "{}",
            "text/html",
            "2024-01-02",
            "other",
            None,
            None,
            0,
            0,
            120.0,
            0.75,
        ),
    ]
    store = _store_with_rows(rows)
    hits, total = store.search("hello")
    assert total == 1
    assert len(hits) == 1
    doc, score = hits[0]
    assert score == 0.75
    assert doc.id == "d1"
    assert doc.metadata["content_type"] == "text/html"
    # Default 'other' verdict stays out of the metadata view.
    assert "site_category" not in doc.metadata


def test_search_backfills_category_columns_into_metadata() -> None:
    rows = [
        (
            "d1",
            "kids story",
            "kids story content",
            "https://kids.test/a",
            "{}",
            "text/html",
            "2024-01-02",
            "kids",
            0.9,
            "auto",
            1,
            0,
            120.0,
            0.5,
        ),
    ]
    store = _store_with_rows(rows)
    (hit, _score) = store.search("kids")[0][0]
    assert hit.metadata["site_category"] == "kids"
    assert hit.metadata["site_category_confidence"] == 0.9
    assert hit.metadata["site_category_source"] == "auto"
    assert hit.metadata["kids_domain_hit"] == 1
    assert "news_domain_hit" not in hit.metadata


def test_search_short_rows_skip_category_backfill() -> None:
    """Pre-migration 8-column rows still map; categories stay absent."""
    row = (
        "d1",
        "hello world",
        "hello world content",
        "https://x.test/a",
        "{}",
        "text/html",
        "2024-01-02",
        0.75,
    )
    doc = _row_to_document(row)
    assert doc.metadata["content_type"] == "text/html"
    assert "site_category" not in doc.metadata


def _fourteen_col_row(doc_id: str = "d1", category: str = "news") -> tuple[Any, ...]:
    """Full search row: 13 data columns (fetch_ms at 12) + trailing score."""
    return (
        doc_id,
        "title",
        "content",
        "https://x.test/a",
        "{}",
        "text/html",
        "2024-01-02",
        category,
        0.9,
        "auto",
        0,
        1,
        120.0,
        0.5,
    )


def test_search_narrows_by_site_category() -> None:
    cursor_rows = [_fourteen_col_row("d1", "news"), _fourteen_col_row("d2", "other")]
    store = _store_with_rows(cursor_rows)
    (hit, _score) = store.search("hello", site_category="news")[0][0]
    assert hit.id == "d1"
    executed = store._stub_cursor.executed  # type: ignore[attr-defined]
    assert any("AND site_category" in sql for sql in executed)


def test_search_without_category_has_no_narrowing() -> None:
    store = _store_with_rows([_fourteen_col_row()])
    store.search("hello")
    executed = store._stub_cursor.executed  # type: ignore[attr-defined]
    assert not any("AND site_category" in sql for sql in executed)


def test_search_category_params_match_placeholders() -> None:
    """Every executed statement binds exactly its ``%s`` placeholders.

    Regression: the narrowed page query once passed six parameters for
    five placeholders, so news/shopping searches failed on Postgres
    with ``provider_unavailable`` while default mode kept working.
    """
    store = _store_with_rows([_fourteen_col_row("d1", "news")])
    store.search("hello", site_category="news")
    calls = store._stub_cursor.calls  # type: ignore[attr-defined]
    assert calls
    for sql, params in calls:
        assert sql.count("%s") == len(params), sql


def test_list_domains_groups_by_host() -> None:
    rows = [("example.com", 2), ("other.test", 1)]
    store = _store_with_rows(rows)
    assert store.list_domains() == [("example.com", 2), ("other.test", 1)]
    assert store.list_domains(limit=0) == []
    assert store.list_domains(limit=-3) == []


def test_search_blank_query_returns_empty_without_io() -> None:
    store = _store_with_rows([])
    assert store.search("   ") == ([], 0)


def test_doc_to_row_carries_description_last() -> None:
    doc = Document(
        id="d1",
        title="t",
        content="c",
        url="https://x.test/a",
        metadata={"description": "meta desc here"},
    )
    assert _doc_to_row(doc)[-1] == "meta desc here"


def test_doc_to_row_empty_description_without_metadata() -> None:
    doc = Document(id="d1", title="t", content="c", url="https://x.test/a")
    assert _doc_to_row(doc)[-1] == ""


class _WriteCursor(_StubCursor):
    def executemany(self, sql: str, params: Any = None) -> None:
        self.calls.append((sql, params))


def _write_store() -> PostgresDocumentStore:
    store = PostgresDocumentStore.__new__(PostgresDocumentStore)
    store._schema = "public"
    store._table = "documents"
    cursor = _WriteCursor([])
    store._connection = lambda: _StubConnectionCtx(cursor)  # type: ignore[method-assign]
    store._stub_cursor = cursor  # type: ignore[attr-defined]
    return store


def test_add_documents_tsvector_folds_in_description() -> None:
    store = _write_store()
    store.add_documents(
        [
            Document(
                id="d1",
                title="t",
                content="c",
                url="https://x.test/a",
                metadata={"description": "meta desc here"},
            )
        ]
    )
    inserts = [
        (sql, params)
        for sql, params in store._stub_cursor.calls  # type: ignore[attr-defined]
        if "INSERT INTO" in sql
    ]
    assert len(inserts) == 1
    sql, params = inserts[0]
    assert sql.count("coalesce(%s, '')") == 3
    assert sql.count("%s") == len(params[0])
    assert params[0][-1] == "meta desc here"


def test_update_document_tsvector_folds_in_description() -> None:
    store = _store_with_rows([])
    store._stub_cursor.rowcount = 1  # type: ignore[attr-defined]
    updated = store.update_document(
        Document(
            id="d1",
            title="t",
            content="c",
            url="https://x.test/a",
            metadata={"description": "meta desc here"},
        )
    )
    assert updated is True
    updates = [
        (sql, params)
        for sql, params in store._stub_cursor.calls  # type: ignore[attr-defined]
        if sql.lstrip().startswith("UPDATE")
    ]
    assert len(updates) == 1
    sql, params = updates[0]
    assert sql.count("coalesce(%s, '')") == 3
    assert sql.count("%s") == len(params)
    assert params[-2] == "meta desc here"


# -- read-replica routing -------------------------------------------------


def _search_rows() -> list[tuple[Any, ...]]:
    return [
        (
            "d1",
            "hello world",
            "hello world content",
            "https://x.test/a",
            "{}",
            "text/html",
            "2024-01-02",
            "other",
            None,
            None,
            0,
            0,
            120.0,
            0.75,
        ),
    ]


def _store_with_replica(
    primary_cursor: _StubCursor,
    replica_cursor: _StubCursor | None = None,
    replica_kwargs: dict[str, Any] | None = None,
) -> PostgresDocumentStore:
    """Offline store with stubbed primary (and optional replica pool)."""
    store = PostgresDocumentStore.__new__(PostgresDocumentStore)
    store._schema = "public"
    store._table = "documents"
    store._checkout_timeout = 0.5
    store._closed = False
    store._state_lock = threading.Lock()
    store._connect_kwargs = {}
    store._connection = (  # type: ignore[method-assign]
        lambda: _StubConnectionCtx(primary_cursor)
    )
    store._replica_kwargs = replica_kwargs
    store._replica_pool = None
    if replica_cursor is not None:
        pool: queue.Queue[Any] = queue.Queue()
        pool.put(_StubConnectionCtx(replica_cursor))
        pool.put(_StubConnectionCtx(replica_cursor))
        pool.put(_StubConnectionCtx(replica_cursor))
        store._replica_pool = pool
    store._replica_pool_size = 1
    store._replica_retry_at = 0.0
    store._replica_consec_failures = 0
    return store


def test_read_uses_replica_when_healthy() -> None:
    primary = _StubCursor(_search_rows())
    replica = _StubCursor(_search_rows())
    store = _store_with_replica(primary, replica, replica_kwargs={"host": "replica"})
    docs, total = store.search("hello", limit=10)
    assert total == 1 and docs[0][0].id == "d1"
    assert replica.executed, "expected the query on the replica"
    assert not primary.executed, "primary must stay untouched"
    assert store._replica_pool is not None
    assert store._replica_pool.qsize() == 3  # connection recycled


def test_read_falls_back_when_replica_unreachable() -> None:
    primary = _StubCursor(_search_rows())
    store = _store_with_replica(primary, None, replica_kwargs={"host": "replica"})

    def _boom(_kwargs: dict[str, Any]) -> Any:
        raise RuntimeError("replica down")

    store._connect_to = _boom  # type: ignore[method-assign]
    docs, total = store.search("hello", limit=10)
    assert total == 1 and docs[0][0].id == "d1"
    assert primary.executed, "expected fallback to the primary"


def test_sick_replica_opens_circuit_back_to_primary() -> None:
    class _FailCursor(_StubCursor):
        def execute(self, sql: str, params: Any = None) -> None:
            super().execute(sql, params)
            raise RuntimeError("recovery conflict")

    primary = _StubCursor(_search_rows())
    store = _store_with_replica(
        primary, _FailCursor(_search_rows()), {"host": "replica"}
    )
    for _ in range(3):
        with pytest.raises(ProviderError):
            store.search("hello", limit=10)
    assert store._replica_pool is None, "circuit must open after 3 failures"
    docs, total = store.search("hello", limit=10)
    assert total == 1 and docs[0][0].id == "d1"
    assert primary.executed, "reads return to the primary"


def test_from_config_parses_replica(monkeypatch: Any) -> None:
    monkeypatch.setattr(PostgresDocumentStore, "_connect", lambda self, *a: object())
    monkeypatch.setattr(PostgresDocumentStore, "_init_schema", lambda self: None)
    store = PostgresDocumentStore.from_config(
        {"host": "primary", "replica": {"host": "replica", "port": 5433}}
    )
    try:
        assert store._replica_kwargs is not None
        assert store._replica_kwargs["host"] == "replica"
        assert store._replica_kwargs["port"] == 5433
        assert store._replica_pool is None  # lazy: never built at boot
    finally:
        store.close()
    plain = PostgresDocumentStore.from_config({"host": "primary"})
    try:
        assert plain._replica_kwargs is None
    finally:
        plain.close()
    blank = PostgresDocumentStore.from_config(
        {"host": "primary", "replica": {"host": "  "}}
    )
    try:
        assert blank._replica_kwargs is None
    finally:
        blank.close()


def test_search_page_query_ranks_ids_before_fetching_rows() -> None:
    """Common terms match tens of thousands of rows: the page query must
    rank narrow (id, score) rows first and fetch full TOASTed content
    for the top-N only, or searches blow past the provider timeout."""
    store = _store_with_rows(_search_rows())
    store.search("hello", limit=10)
    page_sql = store._stub_cursor.calls[1][0]
    assert "WITH ranked AS" in page_sql
    assert "JOIN ranked ON ranked.id" in page_sql
    assert page_sql.count("%s") == 4  # match, rank, limit, offset
    params = store._stub_cursor.calls[1][1]
    assert params == ("hello", "hello", 10, 0)

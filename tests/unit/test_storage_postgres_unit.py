"""Offline unit tests for ``PostgresDocumentStore`` row mapping (no server).

A stub connection stands in for the pool so these run anywhere: they pin
the SELECT column/score contract that the live-skipped conformance suite
in ``tests/integration/test_storage_postgres.py`` cannot guard.
"""

from __future__ import annotations

from typing import Any

from openlens.storage.postgres_backend import (
    PostgresDocumentStore,
    _row_to_document,
)


class _StubCursor:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows
        self.executed: list[str] = []

    def __enter__(self) -> "_StubCursor":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append(sql)

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


def _thirteen_col_row(doc_id: str = "d1", category: str = "news") -> tuple[Any, ...]:
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
        0.5,
    )


def test_search_narrows_by_site_category() -> None:
    cursor_rows = [_thirteen_col_row("d1", "news"), _thirteen_col_row("d2", "other")]
    store = _store_with_rows(cursor_rows)
    (hit, _score) = store.search("hello", site_category="news")[0][0]
    assert hit.id == "d1"
    executed = store._stub_cursor.executed  # type: ignore[attr-defined]
    assert any("AND site_category" in sql for sql in executed)


def test_search_without_category_has_no_narrowing() -> None:
    store = _store_with_rows([_thirteen_col_row()])
    store.search("hello")
    executed = store._stub_cursor.executed  # type: ignore[attr-defined]
    assert not any("AND site_category" in sql for sql in executed)


def test_list_domains_groups_by_host() -> None:
    rows = [("example.com", 2), ("other.test", 1)]
    store = _store_with_rows(rows)
    assert store.list_domains() == [("example.com", 2), ("other.test", 1)]
    assert store.list_domains(limit=0) == []
    assert store.list_domains(limit=-3) == []


def test_search_blank_query_returns_empty_without_io() -> None:
    store = _store_with_rows([])
    assert store.search("   ") == ([], 0)

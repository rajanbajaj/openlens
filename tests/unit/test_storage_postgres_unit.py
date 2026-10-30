"""Offline unit tests for ``PostgresDocumentStore`` row mapping (no server).

A stub connection stands in for the pool so these run anywhere: they pin
the SELECT column/score contract that the live-skipped conformance suite
in ``tests/integration/test_storage_postgres.py`` cannot guard.
"""

from __future__ import annotations

from typing import Any

from openlens.storage.postgres_backend import PostgresDocumentStore


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


def test_search_blank_query_returns_empty_without_io() -> None:
    store = _store_with_rows([])
    assert store.search("   ") == ([], 0)

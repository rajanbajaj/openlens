"""Offline unit tests for ``MySQLDocumentStore`` search binding (no server).

A stub connection stands in for the pool so these run anywhere: they pin
the placeholder/parameter contract for the category-narrowed queries.
"""

from __future__ import annotations

from typing import Any

from openlens.storage.mysql_backend import MySQLDocumentStore


class _StubCursor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def __enter__(self) -> "_StubCursor":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self.calls.append((sql, params))

    def fetchone(self) -> dict[str, Any]:
        return {"n": 1}

    def fetchall(self) -> list[dict[str, Any]]:
        return [
            {
                "id": "d1",
                "title": "title",
                "content": "content",
                "url": "https://x.test/a",
                "metadata": "{}",
                "content_type": "text/html",
                "published": None,
                "site_category": "news",
                "score": 0.5,
            }
        ]


class _StubConnectionCtx:
    def __init__(self, cursor: _StubCursor) -> None:
        self._cursor = cursor

    def __enter__(self) -> "_StubConnectionCtx":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def cursor(self) -> _StubCursor:
        return self._cursor


def _store() -> tuple[MySQLDocumentStore, _StubCursor]:
    store = MySQLDocumentStore.__new__(MySQLDocumentStore)
    store._table = "documents"
    cursor = _StubCursor()
    store._connection = lambda: _StubConnectionCtx(cursor)  # type: ignore[method-assign]
    return store, cursor


def test_search_category_params_match_placeholders() -> None:
    """Every executed statement binds exactly its ``%s`` placeholders.

    Regression: the narrowed page query once passed six parameters for
    five placeholders, so news/shopping searches failed on MySQL with
    ``provider_unavailable`` while default mode kept working.
    """
    store, cursor = _store()
    hits, total = store.search("hello", site_category="news")
    assert total == 1
    assert [doc.id for doc, _ in hits] == ["d1"]
    assert cursor.calls
    for sql, params in cursor.calls:
        assert sql.count("%s") == len(params), sql


def test_search_without_category_params_match_placeholders() -> None:
    """Un-narrowed queries keep their existing binding contract."""
    store, cursor = _store()
    store.search("hello")
    assert cursor.calls
    for sql, params in cursor.calls:
        assert sql.count("%s") == len(params), sql

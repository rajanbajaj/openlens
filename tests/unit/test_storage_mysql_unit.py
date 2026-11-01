"""Offline unit tests for ``MySQLDocumentStore`` search binding (no server).

A stub connection stands in for the pool so these run anywhere: they pin
the placeholder/parameter contract for the category-narrowed queries.
"""

from __future__ import annotations

from typing import Any

from openlens.core.interfaces import Document
from openlens.storage.mysql_backend import MySQLDocumentStore, _doc_to_row


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


def test_search_match_covers_description() -> None:
    """Description-only terms match through the FULLTEXT key."""
    store, cursor = _store()
    store.search("hello")
    assert any("MATCH(title, content, description)" in sql for sql, _ in cursor.calls)


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


class _WriteConnectionCtx(_StubConnectionCtx):
    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


def test_add_documents_upsert_writes_description() -> None:
    """Upsert columns and the FULLTEXT-bound row carry the description."""
    store = MySQLDocumentStore.__new__(MySQLDocumentStore)
    store._table = "documents"
    cursor = _WriteCursor()
    store._connection = lambda: _WriteConnectionCtx(cursor)  # type: ignore[method-assign]
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
    inserts = [(sql, params) for sql, params in cursor.calls if "INSERT INTO" in sql]
    assert len(inserts) == 1
    sql, params = inserts[0]
    assert "description" in sql
    assert "description = VALUES(description)" in sql
    assert sql.count("%s") == len(params[0]) == 10
    assert params[0][-1] == "meta desc here"

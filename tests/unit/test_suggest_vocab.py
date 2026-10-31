"""Regression tests for live-vocabulary query assistance (no server).

Covers the Postgres/MySQL ``list_titles`` vocabulary source (stubbed
connections, mirroring ``test_storage_postgres_unit.py``), the
backend-agnostic ``CustomIndexProvider.suggest_terms`` (including a store
without ``list_titles``), and ``related_queries`` live-vocabulary
preference with static counts as fallback ordering.
"""

from __future__ import annotations

import threading
from typing import Any

from openlens.providers.custom_index import CustomIndexProvider
from openlens.retrieval.suggest import related_queries
from openlens.storage.mysql_backend import MySQLDocumentStore
from openlens.storage.postgres_backend import PostgresDocumentStore


class _StubCursor:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows
        self.executed: list[str] = []

    def __enter__(self) -> "_StubCursor":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append(sql)

    def fetchall(self) -> list[Any]:
        return list(self._rows)


class _StubConnection:
    def __init__(self, cursor: _StubCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _StubCursor:
        return self._cursor


class _StubConnectionCtx:
    def __init__(self, cursor: _StubCursor) -> None:
        self._conn = _StubConnection(cursor)

    def __enter__(self) -> _StubConnection:
        return self._conn

    def __exit__(self, *exc: Any) -> bool:
        return False


def _postgres_store(rows: list[Any]) -> PostgresDocumentStore:
    store = PostgresDocumentStore.__new__(PostgresDocumentStore)
    store._schema = "public"
    store._table = "documents"
    store._state_lock = threading.Lock()
    store._closed = False
    cursor = _StubCursor(rows)
    store._connection = lambda: _StubConnectionCtx(cursor)  # type: ignore[method-assign]
    return store


def _mysql_store(rows: list[Any]) -> MySQLDocumentStore:
    store = MySQLDocumentStore.__new__(MySQLDocumentStore)
    store._table = "documents"
    store._state_lock = threading.Lock()
    store._closed = False
    cursor = _StubCursor(rows)
    store._connection = lambda: _StubConnectionCtx(cursor)  # type: ignore[method-assign]
    return store


def test_postgres_list_titles_returns_titles() -> None:
    store = _postgres_store([("Coffee Maker",), ("Running Shoes",)])
    assert store.list_titles() == ["Coffee Maker", "Running Shoes"]


def test_postgres_list_titles_accepts_mapping_rows() -> None:
    store = _postgres_store([{"title": "Trail Shoes"}, {"title": ""}, {"title": None}])
    assert store.list_titles() == ["Trail Shoes"]


def test_postgres_list_titles_fail_soft() -> None:
    assert _postgres_store([("A",)]).list_titles(limit=0) == []
    assert _postgres_store([("A",)]).list_titles(limit=-1) == []
    assert _postgres_store([("A",)]).list_titles(limit=True) == []  # type: ignore[arg-type]
    closed = _postgres_store([("A",)])
    closed._closed = True
    assert closed.list_titles() == []

    broken = _postgres_store([])

    def _raise() -> Any:
        raise RuntimeError("pool gone")

    broken._connection = _raise  # type: ignore[method-assign]
    assert broken.list_titles() == []


def test_mysql_list_titles_returns_titles() -> None:
    store = _mysql_store([{"title": "Coffee Maker"}, {"title": "Running Shoes"}])
    assert store.list_titles() == ["Coffee Maker", "Running Shoes"]


def test_mysql_list_titles_fail_soft() -> None:
    assert _mysql_store([{"title": "A"}]).list_titles(limit=0) == []
    assert _mysql_store([{"title": "A"}]).list_titles(limit="5") == []  # type: ignore[arg-type]
    closed = _mysql_store([{"title": "A"}])
    closed._closed = True
    assert closed.list_titles() == []

    broken = _mysql_store([])

    def _raise() -> Any:
        raise RuntimeError("pool gone")

    broken._connection = _raise  # type: ignore[method-assign]
    assert broken.list_titles() == []


class _TitleStore:
    """Non-SQLite store exposing the duck-typed ``list_titles`` hook."""

    def __init__(self, titles: list[str]) -> None:
        self._titles = titles

    def list_titles(self, limit: int = 500) -> list[str]:
        return list(self._titles)[:limit]

    def close(self) -> None:
        return None


class _StoreWithoutTitles:
    """Store with no vocabulary hook at all."""

    def close(self) -> None:
        return None


def test_suggest_terms_backend_agnostic_via_duck_typing() -> None:
    provider = CustomIndexProvider(repository=_TitleStore(["Coffee Maker"]))  # type: ignore[arg-type]
    terms = provider.suggest_terms()
    folded = {term.casefold() for term in terms}
    assert {"coffee maker", "coffee", "maker"} <= folded
    assert terms == sorted(terms, key=str.casefold)


def test_suggest_terms_store_without_hook_returns_empty() -> None:
    provider = CustomIndexProvider(repository=_StoreWithoutTitles())  # type: ignore[arg-type]
    assert provider.suggest_terms() == []


def test_suggest_terms_closed_sqlite_returns_empty() -> None:
    from openlens.storage.repository import DocumentRepository

    repo = DocumentRepository(":memory:")
    repo.close()
    provider = CustomIndexProvider(repository=repo)
    assert provider.suggest_terms() == []


def test_related_prefers_live_vocabulary_over_static_counts() -> None:
    counts = {"coffee maker": 100, "coffee grinder": 3}
    vocabulary = ["coffee grinder", "espresso machine", "trail shoes"]
    # The high-count static entry is NOT in the live vocabulary, so it
    # must not win; only live-vocabulary overlap is returned.
    assert related_queries("coffee", counts, vocabulary=vocabulary) == [
        "coffee grinder"
    ]


def test_related_live_vocabulary_uses_counts_for_ordering() -> None:
    counts = {"running watch": 25, "running shoes": 42}
    vocabulary = ["running watch", "running shoes"]
    assert related_queries("running", counts, vocabulary=vocabulary) == [
        "running shoes",
        "running watch",
    ]


def test_related_live_vocabulary_without_counts_is_alphabetical() -> None:
    vocabulary = ["trail running vest", "running shoes", "coffee maker"]
    assert related_queries("running", None, vocabulary=vocabulary) == [
        "running shoes",
        "trail running vest",
    ]


def test_related_static_counts_still_fallback_without_vocabulary() -> None:
    counts = {"running shoes": 42, "running watch": 25, "coffee maker": 99}
    assert related_queries("running", counts) == ["running shoes", "running watch"]
    assert related_queries("running", None) == []

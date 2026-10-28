"""Step-1 storage contract conformance suite (SQLite reference backend).

Covers the ``storage/base.py::DocumentStore`` contract against
``storage/repository.py::DocumentRepository``: add/upsert/get,
search paging + total, count, delete (documents + FTS rows), close,
metadata round-trip, chunked bulk writes, and WAL/busy_timeout init.

Offline only (``:memory:`` DBs, plus ``tmp_path`` file DBs where the
journal mode itself is asserted).
"""

from __future__ import annotations

import sqlite3

import pytest

from openlens.core.interfaces import Document
from openlens.storage.base import DEFAULT_CHUNK_SIZE, DocumentStore
from openlens.storage.repository import DocumentRepository


def _doc(
    doc_id: str = "d1",
    title: str = "hello world",
    content: str = "hello world content",
    url: str | None = None,
    metadata: dict | None = None,
) -> Document:
    return Document(
        id=doc_id,
        title=title,
        content=content,
        url=url,
        metadata=dict(metadata) if metadata is not None else {},
    )


def _fts_count(repo: DocumentRepository) -> int:
    with repo._lock:
        row = repo._conn.execute("SELECT COUNT(*) AS n FROM documents_fts").fetchone()
    return int(row["n"])


# ---------------------------------------------------------------------------
# Contract shape
# ---------------------------------------------------------------------------


def test_repository_implements_document_store_contract():
    assert isinstance(DocumentRepository(":memory:"), DocumentStore)


def test_document_store_is_abstract():
    with pytest.raises(TypeError):
        DocumentStore()  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# add / get / metadata round-trip
# ---------------------------------------------------------------------------


def test_add_and_get_round_trip_with_metadata_dict():
    repo = DocumentRepository(":memory:")
    try:
        assert repo.count() == 0
        assert repo.get("missing") is None
        assert (
            repo.add_documents(
                [
                    _doc(
                        url="http://example.com/a",
                        metadata={"k": "v", "n": 3},
                    )
                ]
            )
            == 1
        )
        fetched = repo.get("d1")
        assert fetched is not None
        assert fetched.id == "d1"
        assert fetched.title == "hello world"
        assert fetched.content == "hello world content"
        assert fetched.url == "http://example.com/a"
        assert fetched.metadata == {"k": "v", "n": 3}
        assert repo.count() == 1
    finally:
        repo.close()


def test_add_documents_returns_total_and_empty_is_zero():
    repo = DocumentRepository(":memory:")
    try:
        assert repo.add_documents([]) == 0
        assert repo.count() == 0
        docs = [_doc(f"d{i}", content=f"body number {i}") for i in range(5)]
        assert repo.add_documents(docs) == 5
        assert repo.count() == 5
    finally:
        repo.close()


# ---------------------------------------------------------------------------
# upsert-overwrite
# ---------------------------------------------------------------------------


def test_upsert_overwrite_visible_exactly_once():
    repo = DocumentRepository(":memory:")
    try:
        repo.add_document(_doc())
        assert repo.search("hello")[1] == 1
        repo.add_document(_doc(title="goodbye moon", content="goodbye moon content"))
        assert repo.count() == 1
        assert _fts_count(repo) == 1
        fetched = repo.get("d1")
        assert fetched is not None
        assert fetched.content == "goodbye moon content"
        # Old text is gone; new text appears exactly once.
        assert repo.search("hello")[1] == 0
        hits, total = repo.search("goodbye")
        assert total == 1
        assert [h.id for h, _ in hits] == ["d1"]
    finally:
        repo.close()


# ---------------------------------------------------------------------------
# search paging + total
# ---------------------------------------------------------------------------


def _seed(repo: DocumentRepository, n: int) -> None:
    repo.add_documents(
        [_doc(f"d{i}", content=f"shared token body {i}") for i in range(n)]
    )


def test_search_paging_and_total():
    repo = DocumentRepository(":memory:")
    try:
        _seed(repo, 5)
        page1, total = repo.search("shared", limit=2, offset=0)
        assert total == 5
        assert len(page1) == 2
        page2, total2 = repo.search("shared", limit=2, offset=2)
        assert total2 == 5
        assert len(page2) == 2
        page3, _ = repo.search("shared", limit=2, offset=4)
        assert len(page3) == 1
        ids = [h.id for h, _ in page1 + page2 + page3]
        assert sorted(ids) == [f"d{i}" for i in range(5)]
    finally:
        repo.close()


def test_search_blank_query_returns_empty():
    repo = DocumentRepository(":memory:")
    try:
        _seed(repo, 2)
        assert repo.search("   ") == ([], 0)
    finally:
        repo.close()


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


def test_delete_removes_document_and_fts_rows():
    repo = DocumentRepository(":memory:")
    try:
        _seed(repo, 3)
        assert repo.delete("d0") is True
        assert repo.count() == 2
        assert _fts_count(repo) == 2
        assert repo.get("d0") is None
        # Deleted text no longer matches; survivors still do.
        assert repo.search("body")[1] == 2
        _, token_total = repo.search('"body 0"')
        assert token_total == 0
    finally:
        repo.close()


def test_delete_missing_returns_false():
    repo = DocumentRepository(":memory:")
    try:
        _seed(repo, 1)
        assert repo.delete("nope") is False
        assert repo.count() == 1
        assert repo.delete("nope") is False
    finally:
        repo.close()


def test_delete_document_alias_matches_delete():
    repo = DocumentRepository(":memory:")
    try:
        repo.add_document(_doc())
        assert repo.delete_document("d1") is True
        assert repo.delete_document("d1") is False
        assert repo.count() == 0
    finally:
        repo.close()


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


def test_close_is_idempotent():
    repo = DocumentRepository(":memory:")
    repo.add_document(_doc())
    repo.close()
    repo.close()


# ---------------------------------------------------------------------------
# chunked bulk writes
# ---------------------------------------------------------------------------


def test_bulk_write_over_default_chunk_size_returns_total():
    repo = DocumentRepository(":memory:")
    try:
        n = DEFAULT_CHUNK_SIZE + 200
        docs = [_doc(f"d{i}", content=f"chunked body {i}") for i in range(n)]
        assert repo.add_documents(docs) == n
        assert repo.count() == n
        assert _fts_count(repo) == n
        _, total = repo.search("chunked")
        assert total == n
    finally:
        repo.close()


def test_small_chunk_size_still_indexes_everything():
    repo = DocumentRepository(":memory:")
    try:
        docs = [_doc(f"d{i}", content=f"tiny chunk body {i}") for i in range(7)]
        assert repo.add_documents(docs, chunk_size=3) == 7
        assert repo.count() == 7
        assert repo.search("tiny")[1] == 7
    finally:
        repo.close()


def test_non_positive_chunk_size_rejected():
    repo = DocumentRepository(":memory:")
    try:
        with pytest.raises(ValueError):
            repo.add_documents([_doc()], chunk_size=0)
        with pytest.raises(ValueError):
            repo.add_documents([_doc()], chunk_size=-2)
        assert repo.count() == 0
    finally:
        repo.close()


# ---------------------------------------------------------------------------
# WAL mode + busy_timeout at schema init
# ---------------------------------------------------------------------------


def test_file_db_uses_wal_and_bounded_busy_timeout(tmp_path):
    repo = DocumentRepository(str(tmp_path / "store.db"))
    try:
        with repo._lock:
            mode = repo._conn.execute("PRAGMA journal_mode").fetchone()[0]
            timeout = repo._conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert str(mode).lower() == "wal"
        assert int(timeout) > 0
    finally:
        repo.close()


def test_reopening_file_db_keeps_rows_and_wal(tmp_path):
    path = str(tmp_path / "store.db")
    first = DocumentRepository(path)
    first.add_document(_doc(metadata={"round": "trip"}))
    first.close()
    second = DocumentRepository(path)
    try:
        with second._lock:
            mode = second._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(mode).lower() == "wal"
        fetched = second.get("d1")
        assert fetched is not None
        assert fetched.metadata == {"round": "trip"}
        assert second.search("hello")[1] == 1
    finally:
        second.close()


def test_legacy_db_without_hash_columns_migrates(tmp_path):
    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE documents (id TEXT PRIMARY KEY, title TEXT NOT NULL,"
        " content TEXT NOT NULL, url TEXT,"
        " metadata TEXT NOT NULL DEFAULT '{}')"
    )
    conn.execute(
        "CREATE VIRTUAL TABLE documents_fts USING fts5("
        "doc_id UNINDEXED, title, content)"
    )
    conn.execute(
        "INSERT INTO documents (id, title, content, url) VALUES (?, ?, ?, ?)",
        ("old", "old title", "old body", "http://example.com/old"),
    )
    conn.execute(
        "INSERT INTO documents_fts (doc_id, title, content) VALUES (?, ?, ?)",
        ("old", "old title", "old body"),
    )
    conn.commit()
    conn.close()

    repo = DocumentRepository(path)
    try:
        fetched = repo.get("old")
        assert fetched is not None
        assert fetched.content == "old body"
        assert repo.count() == 1
    finally:
        repo.close()

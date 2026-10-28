"""Step-1 repository primitives: update/delete/hash, migration, indexes.

Offline only (sqlite file DBs under tmp_path, in-memory elsewhere).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from openlens.core.interfaces import Document, SearchResult
from openlens.indexing.indexer import DocumentIndexer, run_index
from openlens.retrieval.deduplicator import dedupe_key
from openlens.storage.repository import (
    DocumentRepository,
    content_hash_of,
    normalize_url,
)


def _doc(
    doc_id: str = "d1",
    title: str = "hello world",
    content: str = "hello world content",
    url: str | None = "http://example.com/a",
) -> Document:
    return Document(id=doc_id, title=title, content=content, url=url)


def _fts_count(repo: DocumentRepository) -> int:
    with repo._lock:
        row = repo._conn.execute("SELECT COUNT(*) AS n FROM documents_fts").fetchone()
    return int(row["n"])


def _index_names(repo: DocumentRepository) -> dict[str, int]:
    with repo._lock:
        rows = repo._conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
    return {row["name"]: row["sql"] for row in rows}


def test_content_hash_formula_is_pinned_sha256_of_content():
    assert content_hash_of("abc") == hashlib.sha256(b"abc").hexdigest()


def test_normalize_url_reuses_dedupe_key_rule():
    probe = SearchResult(
        id="x",
        title="t",
        snippet="s",
        original_score=0.0,
        final_score=0.0,
        url=" HTTP://Example.COM/Path/ ",
    )
    assert dedupe_key(probe) == "url:" + normalize_url(probe.url)  # type: ignore[arg-type]
    assert normalize_url(" HTTP://Example.COM/Path/ ") == "http://example.com/path"
    assert normalize_url(None) is None


def test_fragment_variants_share_one_identity():
    from openlens.indexing.crawler import normalize_url as crawl_normalize_url

    base = "https://example.com/page"
    assert normalize_url(base + "#section") == base
    assert crawl_normalize_url(base + "#section") == normalize_url(base)
    probe = SearchResult(
        id="x",
        title="t",
        snippet="s",
        original_score=0.0,
        final_score=0.0,
        url=base + "#section",
    )
    assert dedupe_key(probe) == "url:" + base


def test_incremental_fragment_refetch_collapses_to_duplicate():
    repo, indexer = _make_indexer()
    first = Document(
        id="a", title="t", content="same body", url="http://example.com/p#one"
    )
    second = Document(
        id="b", title="t", content="same body", url="http://example.com/p#two"
    )
    stats = indexer.index_incremental([first, second])
    assert repo.count() == 1
    assert stats.added == 1
    assert stats.skipped_duplicates == 1


def test_update_document_rewrites_fts_row():
    repo = DocumentRepository(":memory:")
    repo.add_document(_doc())
    assert repo.search("hello")[1] == 1

    updated = _doc(title="goodbye moon", content="goodbye moon content")
    assert repo.update_document(updated) is True

    assert repo.count() == 1
    assert _fts_count(repo) == 1
    assert repo.get("d1") is not None
    assert repo.get("d1").content == "goodbye moon content"  # type: ignore[union-attr]
    assert repo.search("hello")[1] == 0
    hits, total = repo.search("goodbye")
    assert total == 1
    assert hits[0][0].id == "d1"
    assert repo.get_hash("d1") == content_hash_of("goodbye moon content")


def test_update_missing_document_returns_false():
    repo = DocumentRepository(":memory:")
    assert repo.update_document(_doc()) is False
    assert repo.count() == 0
    assert _fts_count(repo) == 0


def test_delete_document_removes_fts_row():
    repo = DocumentRepository(":memory:")
    repo.add_document(_doc())
    assert repo.delete_document("d1") is True

    assert repo.count() == 0
    assert _fts_count(repo) == 0
    assert repo.get("d1") is None
    assert repo.search("hello")[1] == 0
    assert repo.delete_document("d1") is False


def test_get_hash_and_hash_exists():
    repo = DocumentRepository(":memory:")
    assert repo.get_hash("missing") is None
    assert repo.hash_exists("nope") is False

    repo.add_document(_doc())
    expected = content_hash_of("hello world content")
    assert repo.get_hash("d1") == expected
    assert repo.hash_exists(expected) is True
    assert repo.hash_exists(content_hash_of("other")) is False

    repo.delete_document("d1")
    assert repo.get_hash("d1") is None
    assert repo.hash_exists(expected) is False


def test_url_norm_unique_index_and_content_hash_index_exist():
    repo = DocumentRepository(":memory:")
    indexes = _index_names(repo)
    assert "idx_documents_url_norm" in indexes
    assert "UNIQUE" in indexes["idx_documents_url_norm"].upper()
    assert "idx_documents_content_hash" in indexes
    assert indexes["idx_documents_content_hash"] is not None


def test_null_urls_coexist_under_unique_norm_index():
    repo = DocumentRepository(":memory:")
    repo.add_document(_doc("a", url=None))
    repo.add_document(_doc("b", url=None))
    assert repo.count() == 2
    assert _fts_count(repo) == 2


def test_migration_on_fresh_db(tmp_path):
    repo = DocumentRepository(str(tmp_path / "fresh.db"))
    repo.add_document(_doc())
    assert repo.get_hash("d1") == content_hash_of("hello world content")
    assert repo.count() == 1
    assert _fts_count(repo) == 1
    repo.close()


def _make_pre_hash_db(path: str) -> None:
    """Build a legacy DB: documents table without hash/norm columns."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE documents (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            url TEXT,
            metadata TEXT NOT NULL DEFAULT '{}'
        );
        CREATE VIRTUAL TABLE documents_fts USING fts5(
            doc_id UNINDEXED, title, content
        );
        """
    )
    docs = [
        ("d1", "hello world", "hello world content", "HTTP://Example.COM/A/"),
        ("d2", "second doc", "second doc body", None),
    ]
    for doc_id, title, content, url in docs:
        conn.execute(
            "INSERT INTO documents (id, title, content, url, metadata)"
            " VALUES (?, ?, ?, ?, ?)",
            (doc_id, title, content, url, json.dumps({})),
        )
        conn.execute(
            "INSERT INTO documents_fts (doc_id, title, content) VALUES (?, ?, ?)",
            (doc_id, title, content),
        )
    conn.commit()
    conn.close()


def test_migration_on_pre_hash_db(tmp_path):
    path = str(tmp_path / "legacy.db")
    _make_pre_hash_db(path)

    repo = DocumentRepository(path)
    assert repo.count() == 2
    assert _fts_count(repo) == 2
    assert repo.get_hash("d1") == content_hash_of("hello world content")
    assert repo.get_hash("d2") == content_hash_of("second doc body")
    assert repo.hash_exists(content_hash_of("hello world content")) is True
    # Old and new content stay searchable through the untouched FTS rows.
    assert repo.search("hello")[1] == 1
    assert repo.search("second")[1] == 1
    indexes = _index_names(repo)
    assert "idx_documents_url_norm" in indexes
    assert "idx_documents_content_hash" in indexes
    repo.close()


def _crawl_docs(n: int, prefix: str = "page") -> list[Document]:
    return [
        Document(
            id=f"{prefix}-{i}",
            title=f"{prefix} title {i}",
            content=f"{prefix} body {i} alpha beta",
            url=f"http://example.com/{prefix}/{i}",
        )
        for i in range(n)
    ]


def test_migration_run_twice_keeps_hashes_and_counts(tmp_path):
    path = str(tmp_path / "twice.db")
    _make_pre_hash_db(path)

    first = DocumentRepository(path)
    hashes_first = (first.get_hash("d1"), first.get_hash("d2"))
    count_first = first.count()
    fts_first = _fts_count(first)
    first.close()

    second = DocumentRepository(path)
    assert (second.get_hash("d1"), second.get_hash("d2")) == hashes_first
    assert second.count() == count_first == 2
    assert _fts_count(second) == fts_first == 2
    assert second.hash_exists(hashes_first[0]) is True  # type: ignore[arg-type]
    second.close()

    # And a migrated DB survives a third open with writes in between.
    third = DocumentRepository(path)
    third.add_document(_doc("d3", content="third body", url="http://example.com/c"))
    assert third.count() == 3
    assert third.get_hash("d3") == content_hash_of("third body")
    third.close()


# ---------------------------------------------------------------------------
# Step 2: indexer update/reindex/incremental flows + chunked commits.
# ---------------------------------------------------------------------------


def _make_indexer() -> tuple[DocumentRepository, DocumentIndexer]:
    repo = DocumentRepository(":memory:")
    return repo, DocumentIndexer(repo)


def test_index_documents_writes_in_chunks(monkeypatch):
    repo = DocumentRepository(":memory:")
    indexer = DocumentIndexer(repo)
    batch_sizes: list[int] = []
    orig = repo.add_documents

    def counting(docs):
        batch_sizes.append(len(docs))
        return orig(docs)

    monkeypatch.setattr(repo, "add_documents", counting)
    docs = _crawl_docs(1200)
    assert indexer.index_documents(docs, chunk_size=500) == 1200
    assert batch_sizes == [500, 500, 200]
    assert repo.count() == 1200
    assert _fts_count(repo) == 1200


def test_index_documents_default_chunk_single_batch(monkeypatch):
    repo = DocumentRepository(":memory:")
    indexer = DocumentIndexer(repo)
    calls: list[int] = []
    orig = repo.add_documents

    def counting(docs):
        calls.append(len(docs))
        return orig(docs)

    monkeypatch.setattr(repo, "add_documents", counting)
    # A small corpus under the default chunk size is a single batch.
    docs = _crawl_docs(8)
    assert indexer.index_documents(docs) == 8
    assert calls == [8]
    assert repo.count() == 8


def test_index_seed_still_seeds_all_seed_documents():
    from openlens.indexing.seed import SEED_DOCUMENTS

    repo, indexer = _make_indexer()
    assert indexer.index_seed() == len(SEED_DOCUMENTS)
    assert repo.count() == len(SEED_DOCUMENTS)


def test_chunk_size_must_be_positive():
    repo, indexer = _make_indexer()
    docs = _crawl_docs(2)
    for bad in (0, -1):
        with pytest.raises(ValueError):
            indexer.index_documents(docs, chunk_size=bad)
        with pytest.raises(ValueError):
            indexer.index_incremental(docs, chunk_size=bad)
        with pytest.raises(ValueError):
            indexer.reindex(docs, chunk_size=bad)
    assert repo.count() == 0


def test_add_update_delete_wrappers():
    repo, indexer = _make_indexer()
    indexer.add_document(_doc())
    assert repo.count() == 1

    assert indexer.update_document(_doc(title="new title")) is True
    assert repo.get("d1").title == "new title"  # type: ignore[union-attr]
    assert indexer.update_document(_doc("missing")) is False

    assert indexer.delete_document("d1") is True
    assert indexer.delete_document("d1") is False
    assert repo.count() == 0
    assert _fts_count(repo) == 0


def test_incremental_first_run_adds_everything():
    repo, indexer = _make_indexer()
    docs = _crawl_docs(5)
    stats = indexer.index_incremental(docs)
    assert (stats.added, stats.updated) == (5, 0)
    assert (stats.skipped_unchanged, stats.skipped_duplicates) == (0, 0)
    assert repo.count() == 5
    assert _fts_count(repo) == 5
    assert stats.batches == 1


def test_incremental_unchanged_recrawl_writes_zero_rows(monkeypatch):
    repo, indexer = _make_indexer()
    docs = _crawl_docs(5)
    first = indexer.index_incremental(docs)
    assert first.added == 5

    add_calls: list[int] = []
    update_calls: list[int] = []
    orig_add = repo.add_documents
    orig_update = repo.update_document

    def counting_add(batch):
        add_calls.append(len(batch))
        return orig_add(batch)

    def counting_update(doc):
        update_calls.append(doc.id)
        return orig_update(doc)

    monkeypatch.setattr(repo, "add_documents", counting_add)
    monkeypatch.setattr(repo, "update_document", counting_update)

    second = indexer.index_incremental(_crawl_docs(5))
    assert second.added == 0
    assert second.updated == 0
    assert second.skipped_unchanged == 5
    assert second.skipped_duplicates == 0
    assert add_calls == []
    assert update_calls == []
    assert repo.count() == 5
    assert _fts_count(repo) == 5


def test_incremental_one_page_edit_rewrites_exactly_that_row():
    repo, indexer = _make_indexer()
    docs = _crawl_docs(5)
    indexer.index_incremental(docs)
    hashes_before = {d.id: repo.get_hash(d.id) for d in docs}

    edited = _crawl_docs(5)
    edited[2] = Document(
        id="page-2",
        title="page title 2",
        content="page body 2 completely rewritten gamma delta",
        url="http://example.com/page/2",
    )
    stats = indexer.index_incremental(edited)
    assert stats.updated == 1
    assert stats.added == 0
    assert stats.skipped_unchanged == 4
    assert repo.count() == 5
    assert _fts_count(repo) == 5

    # Exactly the edited row changed hash; the rest are untouched.
    for d in docs:
        if d.id == "page-2":
            assert repo.get_hash(d.id) == content_hash_of(edited[2].content)
            assert repo.get_hash(d.id) != hashes_before[d.id]
        else:
            assert repo.get_hash(d.id) == hashes_before[d.id]

    # New content is searchable, old content is not.
    assert repo.search("rewritten")[1] == 1
    assert repo.search("rewritten")[0][0][0].id == "page-2"
    assert repo.search("alpha")[1] == 4


def test_incremental_url_normalization_matches_query_time():
    repo, indexer = _make_indexer()
    indexer.index_incremental(
        [_doc("u1", content="stable body here", url="http://example.com/A")]
    )
    # Same normalized URL (case + trailing slash differ), same content.
    again = [
        Document(
            id="u1",
            title="hello world",
            content="stable body here",
            url="HTTP://Example.COM/a/",
        )
    ]
    stats = indexer.index_incremental(again)
    assert stats.skipped_unchanged == 1
    assert stats.written == 0
    assert repo.count() == 1

    probe = SearchResult(
        id="u1",
        title="t",
        snippet="s",
        original_score=0.0,
        final_score=0.0,
        url="HTTP://Example.COM/a/",
    )
    assert dedupe_key(probe) == "url:" + normalize_url(probe.url)  # type: ignore[arg-type]


def test_incremental_new_url_known_hash_skipped_as_duplicate():
    repo, indexer = _make_indexer()
    indexer.index_incremental(_crawl_docs(3))
    dupe = Document(
        id="copy-1",
        title="copied title",
        content="page body 0 alpha beta",  # same content as page-0
        url="http://example.com/some/other/path",
    )
    stats = indexer.index_incremental([dupe])
    assert stats.skipped_duplicates == 1
    assert stats.written == 0
    assert repo.count() == 3
    assert _fts_count(repo) == 3


def test_incremental_in_batch_same_url_collapses_without_replace():
    repo, indexer = _make_indexer()
    first = Document(
        id="a1", title="t", content="shared body", url="http://example.com/x"
    )
    second = Document(
        id="a2", title="t", content="shared body", url="http://example.com/X/"
    )
    stats = indexer.index_incremental([first, second])
    assert stats.added == 1
    assert stats.skipped_duplicates == 1
    assert repo.count() == 1
    assert _fts_count(repo) == 1


def test_incremental_preserves_ranking_metadata_on_update():
    repo, indexer = _make_indexer()
    stored = Document(
        id="r1",
        title="ranked title",
        content="ranked body one",
        url="http://example.com/ranked",
        metadata={"trusted": True, "recency_score": 0.8, "topic": "x"},
    )
    indexer.index_incremental([stored])

    # Crawler re-delivers the page (edited content) without ranker metadata.
    incoming = Document(
        id="r1",
        title="ranked title",
        content="ranked body two edited",
        url="http://example.com/ranked",
        metadata={"topic": "x"},
    )
    stats = indexer.index_incremental([incoming])
    assert stats.updated == 1
    kept = repo.get("r1")
    assert kept is not None
    assert kept.metadata["trusted"] is True
    assert kept.metadata["recency_score"] == 0.8
    assert kept.metadata["topic"] == "x"
    assert kept.title == "ranked title"
    assert kept.content == "ranked body two edited"


def test_incremental_preserved_metadata_does_not_force_rewrite():
    repo, indexer = _make_indexer()
    stored = Document(
        id="r2",
        title="steady title",
        content="steady body",
        url="http://example.com/steady",
        metadata={"trusted": False, "recency_score": 0.2},
    )
    assert indexer.index_incremental([stored]).added == 1
    bare = Document(
        id="r2",
        title="steady title",
        content="steady body",
        url="http://example.com/steady",
    )
    second = indexer.index_incremental([bare])
    assert second.skipped_unchanged == 1
    assert second.written == 0


def test_incremental_title_change_triggers_update():
    repo, indexer = _make_indexer()
    indexer.index_incremental(_crawl_docs(2))
    renamed = _crawl_docs(2)
    renamed[0] = Document(
        id="page-0",
        title="a brand new headline",
        content="page body 0 alpha beta",
        url="http://example.com/page/0",
    )
    stats = indexer.index_incremental(renamed)
    assert stats.updated == 1
    assert stats.skipped_unchanged == 1
    assert repo.get("page-0").title == "a brand new headline"  # type: ignore[union-attr]
    assert repo.search("headline")[1] == 1


def test_incremental_null_url_docs_coexist():
    repo, indexer = _make_indexer()
    docs = [
        Document(id="n1", title="t1", content="body one here"),
        Document(id="n2", title="t2", content="body two here"),
    ]
    stats = indexer.index_incremental(docs)
    assert stats.added == 2
    assert repo.count() == 2
    again = indexer.index_incremental(docs)
    assert again.skipped_unchanged == 2
    assert again.written == 0


def test_reindex_rewrites_all_and_preserves_ranking_fields(monkeypatch):
    repo, indexer = _make_indexer()
    seed = [
        Document(
            id=f"k-{i}",
            title=f"keep title {i}",
            content=f"keep body {i} lore ipsum",
            url=f"http://example.com/keep/{i}",
            metadata={"trusted": True, "recency_score": 0.5},
        )
        for i in range(4)
    ]
    indexer.index_incremental(seed)

    update_calls: list[str] = []
    orig_update = repo.update_document

    def counting_update(doc):
        update_calls.append(doc.id)
        return orig_update(doc)

    monkeypatch.setattr(repo, "update_document", counting_update)

    bare = [
        Document(
            id=f"k-{i}",
            title=f"keep title {i}",
            content=f"keep body {i} lore ipsum",
            url=f"http://example.com/keep/{i}",
        )
        for i in range(4)
    ]
    stats = indexer.reindex(bare)
    assert stats.updated == 4
    assert stats.added == 0
    assert sorted(update_calls) == [f"k-{i}" for i in range(4)]
    assert repo.count() == 4
    assert _fts_count(repo) == 4
    for i in range(4):
        kept = repo.get(f"k-{i}")
        assert kept is not None
        assert kept.id == f"k-{i}"
        assert kept.title == f"keep title {i}"
        assert kept.content == f"keep body {i} lore ipsum"
        assert kept.metadata["trusted"] is True
        assert kept.metadata["recency_score"] == 0.5
    assert repo.search("lore")[1] == 4


def test_reindex_adds_missing_and_leaves_unlisted(monkeypatch):
    repo, indexer = _make_indexer()
    indexer.index_incremental(_crawl_docs(3))

    batches: list[int] = []
    orig_add = repo.add_documents

    def counting_add(batch):
        batches.append(len(batch))
        return orig_add(batch)

    monkeypatch.setattr(repo, "add_documents", counting_add)

    corpus = _crawl_docs(3) + [
        Document(
            id="fresh",
            title="fresh title",
            content="fresh body zeta",
            url="http://example.com/fresh",
        )
    ]
    stats = indexer.reindex(corpus, chunk_size=2)
    assert stats.updated == 3
    assert stats.added == 1
    assert batches == [1]
    assert repo.count() == 4
    assert repo.search("zeta")[1] == 1

    # Reindex never prunes: unlisted rows survive until explicit delete.
    stats2 = indexer.reindex(_crawl_docs(3))
    assert repo.count() == 4
    assert stats2.updated == 3
    assert indexer.delete_document("fresh") is True
    assert repo.count() == 3
    assert repo.search("zeta")[1] == 0
    assert _fts_count(repo) == 3


def test_run_index_delegates_crawl_to_incremental(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S7 entry point: run_index crawls then incrementally indexes (no network)."""
    import openlens.indexing.indexer as indexer_module

    repo = DocumentRepository(tmp_path / "run_index.db")
    crawled = [
        _doc("one", "one title", "one body alpha", "http://example.com/1"),
        _doc("two", "two title", "two body beta", "http://example.com/2"),
    ]
    monkeypatch.setattr(
        indexer_module.PoliteCrawler, "crawl", lambda self, seeds: crawled
    )
    stats = run_index(["http://example.com/"], repo, {"max_pages": 10})
    assert (stats.added, stats.updated) == (2, 0)
    assert repo.count() == 2
    # Second run with identical crawl output writes nothing.
    stats2 = run_index(["http://example.com/"], repo, {"max_pages": 10})
    assert (stats2.added, stats2.updated) == (0, 0)
    assert stats2.skipped_unchanged == 2

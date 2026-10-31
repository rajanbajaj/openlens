"""Authority graph + fetch-latency columns on the SQLite reference backend.

Accept: outlinks in document metadata land in ``document_links`` (one row
per normalized target, self-links dropped); ``inlink_counts`` reports
distinct linking hosts per target (site-wide nav counts once) with
unknown targets absent; reindex replaces a source's rows; delete cleans
them; ``fetch_ms`` round-trips through the promoted column into the
metadata view; legacy rows backfill both from stored metadata JSON.
"""

from __future__ import annotations

import json

from openlens.core.interfaces import Document
from openlens.storage.repository import DocumentRepository


def _doc(
    doc_id: str,
    url: str,
    metadata: dict | None = None,
) -> Document:
    return Document(
        id=doc_id,
        title=f"title {doc_id}",
        content=f"Informative crawlable prose about the topic. {doc_id} " * 10,
        url=url,
        metadata=dict(metadata or {}),
    )


def _link_rows(repo: DocumentRepository) -> list[tuple]:
    with repo._lock:
        return repo._conn.execute(
            "SELECT source_id, source_host, target, target_host"
            " FROM document_links ORDER BY source_id, target"
        ).fetchall()


def test_outlinks_indexed_and_counted_by_distinct_host() -> None:
    repo = DocumentRepository(":memory:")
    try:
        target = "https://example.com/target"
        repo.add_documents(
            [
                _doc(
                    "https://a.com/1",
                    "https://a.com/1",
                    {"outlinks": [target, target]},
                ),
                _doc(
                    "https://b.com/1",
                    "https://b.com/1",
                    {"outlinks": [target.upper()]},
                ),
                _doc(
                    "https://a.com/2",
                    "https://a.com/2",
                    {"outlinks": [target, "https://a.com/2"]},
                ),
            ]
        )
        rows = _link_rows(repo)
        # Dedup: one row per (source, target); self-link dropped.
        assert len(rows) == 3
        counts = repo.inlink_counts([target, "https://missing.example/"])
        # Two distinct hosts (a.com, b.com); missing target absent.
        assert counts == {target: 2}
    finally:
        repo.close()


def test_reindex_replaces_source_links() -> None:
    repo = DocumentRepository(":memory:")
    try:
        repo.add_documents(
            [_doc("a", "https://a.com/1", {"outlinks": ["https://x.com/1"]})]
        )
        assert repo.inlink_counts(["https://x.com/1"]) == {"https://x.com/1": 1}
        repo.add_documents(
            [_doc("a", "https://a.com/1", {"outlinks": ["https://y.com/1"]})]
        )
        assert repo.inlink_counts(["https://x.com/1"]) == {}
        assert repo.inlink_counts(["https://y.com/1"]) == {"https://y.com/1": 1}
    finally:
        repo.close()


def test_delete_cleans_authority_rows() -> None:
    repo = DocumentRepository(":memory:")
    try:
        repo.add_documents(
            [_doc("a", "https://a.com/1", {"outlinks": ["https://x.com/1"]})]
        )
        assert repo.delete("a") is True
        assert _link_rows(repo) == []
        assert repo.inlink_counts(["https://x.com/1"]) == {}
    finally:
        repo.close()


def test_fetch_ms_round_trips_into_metadata_view() -> None:
    repo = DocumentRepository(":memory:")
    try:
        repo.add_documents([_doc("a", "https://a.com/1", {"fetch_ms": 123.5})])
        fetched = repo.get("a")
        assert fetched is not None
        assert fetched.metadata["fetch_ms"] == 123.5
        with repo._lock:
            row = repo._conn.execute(
                "SELECT fetch_ms FROM documents WHERE id = 'a'"
            ).fetchone()
        assert float(row[0]) == 123.5
    finally:
        repo.close()


def test_non_numeric_fetch_ms_not_promoted() -> None:
    repo = DocumentRepository(":memory:")
    try:
        repo.add_documents([_doc("a", "https://a.com/1", {"fetch_ms": "fast"})])
        with repo._lock:
            row = repo._conn.execute(
                "SELECT fetch_ms FROM documents WHERE id = 'a'"
            ).fetchone()
        assert row[0] is None
    finally:
        repo.close()


def test_legacy_rows_backfill_links_and_fetch_ms() -> None:
    repo = DocumentRepository(":memory:")
    try:
        repo.add_documents([_doc("a", "https://a.com/1")])
        with repo._lock:
            repo._conn.execute(
                "UPDATE documents SET fetch_ms = NULL, metadata = ? WHERE id = 'a'",
                (
                    json.dumps(
                        {
                            "fetch_ms": 77.0,
                            "outlinks": ["https://x.com/1"],
                        }
                    ),
                ),
            )
            repo._conn.execute("DELETE FROM document_links")
            repo._conn.commit()
            repo._migrate()
        assert repo.inlink_counts(["https://x.com/1"]) == {"https://x.com/1": 1}
        fetched = repo.get("a")
        assert fetched is not None
        assert fetched.metadata["fetch_ms"] == 77.0
    finally:
        repo.close()


def test_inlink_counts_empty_and_neutral_inputs() -> None:
    repo = DocumentRepository(":memory:")
    try:
        assert repo.inlink_counts([]) == {}
        assert repo.inlink_counts(["", None, 42]) == {}  # type: ignore[list-item]
    finally:
        repo.close()

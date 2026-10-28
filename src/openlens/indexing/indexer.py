"""Indexer flows: chunked bulk writes, dedup-gated incremental runs, reindex.

Step-2 flows sit on top of the Step-1 repository primitives
(`update_document` / `delete_document` / `get_hash` / `hash_exists` plus the
`content_hash` / `url_norm` columns). Ingest-time dedup reuses the exact
query-time normalization rule (`normalize_url`, shared with
`retrieval/deduplicator.py::dedupe_key`), so ingest can never store a row
that query-time dedup would then drop (or vice versa).

Dedup rule (per incoming document):

- known id + same normalized URL + same content hash + same effective
  title/metadata -> skip, no write (`skipped_unchanged`);
- known id + anything else changed -> `update_document`
  (`updated`), with stored ranking-critical metadata preserved;
- unknown id + new normalized URL + already-known content hash ->
  skip as duplicate (`skipped_duplicates`);
- otherwise -> chunked `add_documents` (`added`).

Chunked commits: bulk adds go out as one `add_documents` call per
`chunk_size` documents (default 500) so the repository lock is released
between chunks instead of being held for the whole corpus.

Ranking-critical stored fields: stable `id`, verbatim `title`/`content`,
and any stored `trusted` / `recency_score` metadata survive both
incremental updates and full reindex runs: when an incoming document
lacks those keys, the stored values are merged back in before the write.

Stable-id assumption: same-URL matching across crawls is keyed on
`Document.id`. The crawler (plan step 3) must therefore assign stable,
preferably normalized-URL-derived ids; the repository exposes no
URL-to-id lookup, so same-URL/different-id collisions cannot be resolved
here (the UNIQUE `url_norm` index would REPLACE the older row).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from openlens.core.interfaces import Document
from openlens.indexing.crawler import PoliteCrawler
from openlens.indexing.seed import SEED_DOCUMENTS
from openlens.storage.repository import (
    DocumentRepository,
    content_hash_of,
    normalize_url,
)

#: Default bulk-write chunk size: one `add_documents` call per chunk.
DEFAULT_CHUNK_SIZE = 500

#: Stored metadata keys that survive updates/reindex even when the
#: incoming document does not carry them (the ranker reads these).
RANKING_METADATA_KEYS = ("trusted", "recency_score")


@dataclass
class IndexStats:
    """Outcome counters for one incremental or reindex run."""

    added: int = 0
    updated: int = 0
    skipped_unchanged: int = 0
    skipped_duplicates: int = 0
    deleted: int = 0
    batches: int = 0

    @property
    def written(self) -> int:
        """Rows rewritten (adds plus updates)."""
        return self.added + self.updated

    @property
    def skipped(self) -> int:
        """Rows deliberately not written."""
        return self.skipped_unchanged + self.skipped_duplicates


def _with_preserved_metadata(stored: Document | None, doc: Document) -> Document:
    """Return `doc` with stored ranking-critical metadata merged in.

    Keys in RANKING_METADATA_KEYS already present on the incoming document
    win; missing ones are backfilled from the stored row so incremental
    updates and reindex runs never drop ranker inputs.
    """
    if stored is None:
        return doc
    stored_meta = stored.metadata or {}
    incoming_meta = doc.metadata or {}
    merged = dict(incoming_meta)
    for key in RANKING_METADATA_KEYS:
        if key not in merged and key in stored_meta:
            merged[key] = stored_meta[key]
    if merged == incoming_meta:
        return doc
    return Document(
        id=doc.id,
        title=doc.title,
        content=doc.content,
        url=doc.url,
        metadata=merged,
    )


def _chunks(docs: list[Document], chunk_size: int) -> list[list[Document]]:
    return [docs[i : i + chunk_size] for i in range(0, len(docs), chunk_size)]


def _check_chunk_size(chunk_size: int) -> None:
    if not isinstance(chunk_size, int) or chunk_size < 1:
        raise ValueError("chunk_size must be a positive integer")


def run_index(
    seed_urls: list[str],
    repository: DocumentRepository,
    config: dict[str, Any] | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> IndexStats:
    """Crawl seeds and incrementally index the results (CLI entry point).

    This is the single delegation path the future ``index`` CLI command
    calls (review seam S7): configure a :class:`PoliteCrawler` from the
    ``indexing:`` config section, crawl, then
    :meth:`DocumentIndexer.index_incremental`. Unknown config keys are
    rejected by ``PoliteCrawler.configure``.
    """
    crawler = PoliteCrawler()
    crawler.configure(dict(config or {}))
    docs = crawler.crawl(list(seed_urls))
    return DocumentIndexer(repository).index_incremental(docs, chunk_size)


class DocumentIndexer:
    """Indexes local documents into a DocumentRepository."""

    def __init__(self, repository: DocumentRepository) -> None:
        self._repository = repository

    def index_documents(
        self, docs: list[Document], chunk_size: int = DEFAULT_CHUNK_SIZE
    ) -> int:
        """Chunked verbatim bulk write; returns the number of documents given.

        Legacy path used by seeding and the provider: every document is
        written (INSERT OR REPLACE semantics in the repository), one
        `add_documents` call per chunk so the repository lock is released
        between chunks. No dedup or metadata merging happens here; use
        `index_incremental` for crawl ingest.
        """
        _check_chunk_size(chunk_size)
        batches = _chunks(list(docs), chunk_size)
        for batch in batches:
            self._repository.add_documents(batch)
        return len(docs)

    def index_seed(self) -> int:
        return self.index_documents(list(SEED_DOCUMENTS))

    def add_document(self, doc: Document) -> None:
        """Write one document verbatim (no dedup, no metadata merge)."""
        self._repository.add_document(doc)

    def update_document(self, doc: Document) -> bool:
        """Rewrite one document by id. False when the id is unknown."""
        return self._repository.update_document(doc)

    def delete_document(self, doc_id: str) -> bool:
        """Remove one document (and its FTS row). False when unknown."""
        return self._repository.delete_document(doc_id)

    def index_incremental(
        self, docs: list[Document], chunk_size: int = DEFAULT_CHUNK_SIZE
    ) -> IndexStats:
        """Ingest a crawl batch, writing only new or changed documents.

        Applies the ingest dedup rule (see module docstring) and commits
        new documents in chunks. Returns per-run counters.
        """
        _check_chunk_size(chunk_size)
        stats = IndexStats()
        # Staged writes for this call (last occurrence wins per id).
        staged_adds: dict[str, Document] = {}
        # Normalized URL -> staged id: collapses in-batch same-URL
        # duplicates without hitting the UNIQUE url_norm index.
        staged_norms: dict[str, str] = {}
        updates: dict[str, Document] = {}
        unchanged: set[str] = set()

        def _unstage(doc_id: str) -> None:
            old = staged_adds.pop(doc_id, None)
            if old is not None:
                old_norm = normalize_url(old.url)
                if old_norm is not None and staged_norms.get(old_norm) == doc_id:
                    del staged_norms[old_norm]

        for doc in docs:
            content_hash = content_hash_of(doc.content)
            norm = normalize_url(doc.url)
            stored = self._repository.get(doc.id)
            if stored is not None:
                _unstage(doc.id)
                updates.pop(doc.id, None)
                unchanged.discard(doc.id)
                effective = _with_preserved_metadata(stored, doc)
                if (
                    normalize_url(stored.url) == norm
                    and content_hash_of(stored.content) == content_hash
                    and stored.title == effective.title
                    and (stored.metadata or {}) == (effective.metadata or {})
                ):
                    unchanged.add(doc.id)
                else:
                    updates[doc.id] = effective
                continue
            if norm is not None and self._repository.hash_exists(content_hash):
                _unstage(doc.id)
                updates.pop(doc.id, None)
                unchanged.discard(doc.id)
                stats.skipped_duplicates += 1
                continue
            if (
                norm is not None
                and norm in staged_norms
                and staged_norms[norm] != doc.id
            ):
                # Same normalized URL already staged in this batch under a
                # different id: first wins, the later copy is a duplicate
                # of an uncommitted row.
                _unstage(doc.id)
                updates.pop(doc.id, None)
                unchanged.discard(doc.id)
                stats.skipped_duplicates += 1
                continue
            _unstage(doc.id)
            updates.pop(doc.id, None)
            unchanged.discard(doc.id)
            staged_adds[doc.id] = doc
            if norm is not None:
                staged_norms[norm] = doc.id

        to_add = list(staged_adds.values())
        for doc_id, effective in updates.items():
            if self._repository.update_document(effective):
                stats.updated += 1
            else:  # id vanished mid-call; fall back to a chunked insert
                to_add.append(effective)
        for batch in _chunks(to_add, chunk_size):
            self._repository.add_documents(batch)
            stats.batches += 1
        stats.added = len(to_add)
        stats.skipped_unchanged = len(unchanged)
        return stats

    def reindex(
        self, docs: list[Document], chunk_size: int = DEFAULT_CHUNK_SIZE
    ) -> IndexStats:
        """Rewrite every given document (full reindex).

        Unlike `index_incremental`, nothing is skipped: every row is
        rewritten verbatim (stable `id`, verbatim `title`/`content`) with
        stored ranking-critical metadata preserved, in chunks. Documents
        not listed here are left untouched; use `delete_document` for
        explicit deletes.
        """
        _check_chunk_size(chunk_size)
        stats = IndexStats()
        to_add: list[Document] = []
        for doc in docs:
            stored = self._repository.get(doc.id)
            effective = _with_preserved_metadata(stored, doc)
            if stored is None:
                to_add.append(effective)
            else:
                self._repository.update_document(effective)
                stats.updated += 1
        for batch in _chunks(to_add, chunk_size):
            self._repository.add_documents(batch)
            stats.batches += 1
        stats.added = len(to_add)
        return stats

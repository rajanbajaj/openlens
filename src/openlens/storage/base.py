"""Storage backend contract.

Single swappable interface for document persistence and full-text search.
SQLite (`storage/repository.py::DocumentRepository`) is the reference
implementation; every future backend (MySQL, Postgres) implements this
same ABC so switching databases is a config change, not a rewrite.

Conventions shared by all backends:

- ``add_documents`` writes in chunks of ``chunk_size`` documents per
  transaction and returns the total indexed. Each chunk commits
  independently — callers needing all-or-nothing ingest must layer it
  themselves; the contract does not promise cross-chunk atomicity.
- ``delete`` removes the document row and every index row atomically;
  deleting a missing id returns ``False`` and never raises.
- ``search`` takes the already-parsed FTS-style query string produced by
  ``retrieval/query_parser.parse_query``; each backend translates it to
  its native form (translation is documented as lossy).
- ``Document.metadata`` round-trips as a parsed dict (conformance
  compares parsed dicts, not raw storage).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from openlens.core.interfaces import Document

#: Default bulk-write chunk size: one transaction per chunk.
DEFAULT_CHUNK_SIZE = 500


class DocumentStore(ABC):
    """Abstract document store all backends implement."""

    @abstractmethod
    def add_document(self, doc: Document) -> None:
        """Index one document (upsert: same id overwrites)."""
        raise NotImplementedError

    @abstractmethod
    def add_documents(self, docs: list[Document], chunk_size: int = 500) -> int:
        """Index documents in chunks of ``chunk_size`` per transaction.

        Upsert semantics (same id overwrites). Returns the total number
        of documents indexed. Each chunk commits independently — no
        cross-chunk atomicity is promised.
        """
        raise NotImplementedError

    @abstractmethod
    def delete(self, doc_id: str) -> bool:
        """Remove a document and its index rows.

        Returns True iff a row was removed; a missing id returns False
        and is not an error.
        """
        raise NotImplementedError

    @abstractmethod
    def get(self, doc_id: str) -> Document | None:
        """Fetch a document by id, or None when missing."""
        raise NotImplementedError

    @abstractmethod
    def search(
        self, fts_query: str, limit: int = 10, offset: int = 0
    ) -> tuple[list[tuple[Document, float]], int]:
        """Full-text search; returns ((doc, score) page, total matches)."""
        raise NotImplementedError

    @abstractmethod
    def count(self) -> int:
        """Return the number of stored documents."""
        raise NotImplementedError

    @abstractmethod
    def list_documents(self, limit: int = 500, offset: int = 0) -> list[Document]:
        """Return one page of stored documents in stable id order.

        Paginating with increasing ``offset`` enumerates the whole store;
        used by offline export paths (e.g. bulk-loading a search index).
        """
        raise NotImplementedError

    @abstractmethod
    def close(self) -> None:
        """Release the backend connection. Idempotent."""
        raise NotImplementedError

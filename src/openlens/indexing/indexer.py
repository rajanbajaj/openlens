"""Minimal indexer: writes Documents into the FTS5 repository."""

from __future__ import annotations

from openlens.core.interfaces import Document
from openlens.indexing.seed import SEED_DOCUMENTS
from openlens.storage.repository import DocumentRepository


class DocumentIndexer:
    """Indexes local documents into a DocumentRepository."""

    def __init__(self, repository: DocumentRepository) -> None:
        self._repository = repository

    def index_documents(self, docs: list[Document]) -> int:
        return self._repository.add_documents(docs)

    def index_seed(self) -> int:
        return self.index_documents(list(SEED_DOCUMENTS))

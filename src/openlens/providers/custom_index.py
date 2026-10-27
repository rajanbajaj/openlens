"""Custom index provider: SQLite FTS5 search behind SearchProvider."""

from __future__ import annotations

from typing import Any

from openlens.core.interfaces import (
    ConfigError,
    Document,
    ProviderError,
    SearchProvider,
    SearchRequest,
    SearchResult,
)
from openlens.indexing.indexer import DocumentIndexer
from openlens.retrieval.deduplicator import deduplicate
from openlens.retrieval.normalizer import normalize_many
from openlens.retrieval.query_parser import parse_query
from openlens.storage.repository import DocumentRepository

PROVIDER_NAME = "custom_index"


class CustomIndexProvider(SearchProvider):
    """SearchProvider over a local SQLite FTS5 document store."""

    def __init__(
        self,
        db_path: str = ":memory:",
        repository: DocumentRepository | None = None,
    ) -> None:
        self._db_path = db_path
        self._repository = repository or DocumentRepository(db_path)
        self._indexer = DocumentIndexer(self._repository)

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    def configure(self, config: dict[str, Any]) -> None:
        if not isinstance(config, dict):
            raise ConfigError("custom_index config must be a mapping")
        db_path = config.get("db_path", self._db_path)
        if not isinstance(db_path, str) or not db_path:
            raise ConfigError("custom_index 'db_path' must be a non-empty string")
        seed = config.get("seed", True)
        if not isinstance(seed, bool):
            raise ConfigError("custom_index 'seed' must be a boolean")
        self._db_path = db_path
        self._repository = DocumentRepository(db_path)
        self._indexer = DocumentIndexer(self._repository)
        if seed:
            self._indexer.index_seed()

    def index_documents(self, docs: list[Document]) -> int:
        return self._indexer.index_documents(docs)

    def document_count(self) -> int:
        return self._repository.count()

    def search(self, request: SearchRequest) -> list[SearchResult]:
        if not request.query or not request.query.strip():
            raise ProviderError(
                "query must not be empty",
                provider_name=self.name,
                code=ProviderError.INVALID_REQUEST,
            )
        if request.limit < 0 or request.offset < 0:
            raise ProviderError(
                "limit and offset must be non-negative",
                provider_name=self.name,
                code=ProviderError.INVALID_REQUEST,
            )
        parsed = parse_query(request.query)
        if not parsed.fts_query:
            return []
        try:
            scored, _total = self._repository.search(
                parsed.fts_query, limit=request.limit, offset=request.offset
            )
        except ProviderError:
            raise
        except Exception as exc:  # never leak raw client errors
            raise ProviderError(
                f"search failed: {exc}",
                provider_name=self.name,
                code=ProviderError.UNKNOWN,
            ) from exc
        results = normalize_many(scored, provider=self.name, query=request.query)
        return deduplicate(results)

    def health_check(self) -> bool:
        try:
            self._repository.count()
            return True
        except Exception:
            return False

"""Custom index provider: swappable document-store search behind SearchProvider.

Backend selection lives in the ``storage:`` config section (see
``configs/default.yaml``): ``storage.backend`` is the single switch
(``sqlite`` | ``mysql`` | ``postgres``, default ``sqlite``) with
per-backend connection fields under ``storage.sqlite`` /
``storage.mysql`` / ``storage.postgres``. The provider-entry ``db_path``
keeps working as the SQLite backend's field (back-compat: a config with
no ``storage:`` section at all boots SQLite).

Server drivers are lazy-imported inside the mysql/postgres branches, so
an unconfigured backend adds no import-time dependency; connection
failures at construction surface as ``ProviderError(UNAVAILABLE)``,
never raw driver exceptions.
"""

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
from openlens.indexing.seed import SEED_DOCUMENTS
from openlens.retrieval.deduplicator import deduplicate
from openlens.retrieval.normalizer import normalize_many
from openlens.retrieval.query_parser import parse_query
from openlens.storage.base import DocumentStore
from openlens.storage.repository import DocumentRepository

PROVIDER_NAME = "custom_index"

#: Backends selectable via ``storage.backend``. ``sqlite`` is the default
#: and needs no extra dependency; ``mysql``/``postgres`` need their
#: optional extras and lazy-import their drivers on first use.
SUPPORTED_BACKENDS = ("sqlite", "mysql", "postgres")

#: Backend used when no ``storage:`` section (or no ``backend`` key) is set.
DEFAULT_BACKEND = "sqlite"


def _storage_map(config: dict[str, Any]) -> dict[str, Any]:
    """Return the ``storage:`` subsection of a provider config ({} when absent)."""
    storage = config.get("storage", None)
    if storage is None:
        return {}
    if not isinstance(storage, dict):
        raise ConfigError("custom_index 'storage' must be a mapping")
    return storage


def _backend_name(storage: dict[str, Any]) -> str:
    """Validate and return the selected backend name (default ``sqlite``)."""
    backend = storage.get("backend", DEFAULT_BACKEND)
    if not isinstance(backend, str) or backend not in SUPPORTED_BACKENDS:
        raise ConfigError(
            f"Unknown storage backend {backend!r}. "
            f"Supported: {sorted(SUPPORTED_BACKENDS)}."
        )
    return backend


def _subsection(storage: dict[str, Any], key: str) -> dict[str, Any]:
    """Return ``storage.<key>`` ({} when absent); never a non-mapping."""
    section = storage.get(key, None)
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise ConfigError(f"custom_index 'storage.{key}' must be a mapping")
    return section


def resolve_sqlite_db_path(
    config: dict[str, Any], fallback_db_path: str = ":memory:"
) -> str:
    """Resolve the SQLite ``db_path``: ``storage.sqlite.db_path`` wins, then
    the provider-entry ``db_path``, then ``fallback_db_path``."""
    if not isinstance(config, dict):
        raise ConfigError("custom_index config must be a mapping")
    storage = _storage_map(config)
    sqlite_cfg = _subsection(storage, "sqlite")
    db_path = sqlite_cfg.get("db_path", config.get("db_path", fallback_db_path))
    if not isinstance(db_path, str) or not db_path:
        raise ConfigError("custom_index 'db_path' must be a non-empty string")
    return db_path


def open_store(
    config: dict[str, Any], fallback_db_path: str = ":memory:"
) -> DocumentStore:
    """Build the configured document store without touching any provider state.

    ``fallback_db_path`` (the provider-entry ``db_path``) is the SQLite
    backend's field when ``storage.sqlite.db_path`` is unset. Server
    drivers import lazily here, so calling this with the SQLite backend
    never imports ``pymysql``/``psycopg``.
    """
    if not isinstance(config, dict):
        raise ConfigError("custom_index config must be a mapping")
    storage = _storage_map(config)
    backend = _backend_name(storage)
    if backend == "sqlite":
        return DocumentRepository(resolve_sqlite_db_path(config, fallback_db_path))
    if backend == "mysql":
        from openlens.storage.mysql_backend import MySQLDocumentStore

        return MySQLDocumentStore.from_config(_subsection(storage, "mysql"))
    from openlens.storage.postgres_backend import PostgresDocumentStore

    return PostgresDocumentStore.from_config(_subsection(storage, "postgres"))


class CustomIndexProvider(SearchProvider):
    """SearchProvider over a swappable document store (default SQLite FTS5)."""

    def __init__(
        self,
        db_path: str = ":memory:",
        repository: DocumentStore | None = None,
    ) -> None:
        self._db_path = db_path
        self._repository: DocumentStore = (
            repository if repository is not None else DocumentRepository(db_path)
        )
        self._indexer: DocumentIndexer | None = (
            DocumentIndexer(self._repository)
            if isinstance(self._repository, DocumentRepository)
            else None
        )

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    def _attach_store(self, store: DocumentStore) -> None:
        """Swap the backing store, rebuilding the SQLite indexer seam."""
        self._repository = store
        self._indexer = (
            DocumentIndexer(store) if isinstance(store, DocumentRepository) else None
        )

    def configure(self, config: dict[str, Any]) -> None:
        if not isinstance(config, dict):
            raise ConfigError("custom_index config must be a mapping")
        # Validate the cheap fields before touching any connection so a
        # bad config never closes a healthy store.
        seed = config.get("seed", True)
        if not isinstance(seed, bool):
            raise ConfigError("custom_index 'seed' must be a boolean")
        # Fail fast on unknown backends / malformed sections while the
        # previous repository is still open (raises ConfigError).
        store = open_store(config, fallback_db_path=self._db_path)
        previous = self._repository
        if previous is not None:
            previous.close()
        if isinstance(store, DocumentRepository):
            self._db_path = resolve_sqlite_db_path(config, self._db_path)
        self._attach_store(store)
        if seed:
            self._index_seed()

    def _index_seed(self) -> int:
        if self._indexer is not None:
            return self._indexer.index_seed()
        return self._repository.add_documents(list(SEED_DOCUMENTS))

    def index_documents(self, docs: list[Document]) -> int:
        if self._indexer is not None:
            return self._indexer.index_documents(docs)
        return self._repository.add_documents(list(docs))

    def document_count(self) -> int:
        return self._repository.count()

    def close(self) -> None:
        """Release the backing store. Idempotent."""
        self._repository.close()

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

"""Single-cluster blue-green index lifecycle for OpenSearch.

One cluster holds timestamped physical indices (``<prefix>_YYYYMMDDHHMMSS``)
plus a single serving alias (default ``openlens`` — the same name the
``opensearch`` search provider already configures, so serving through the
alias needs no provider change). The crawl/index path fills a fresh stage
index while serving reads the alias; promotion moves the alias atomically
(one ``update_aliases`` call carrying every remove+add, so there is never
a half-state); the previous index is kept for instant rollback and later
pruned.

Data flow (SQL store stays canonical)::

    crawl -> DocumentStore -> bulk_load(stage index) -> verify
        -> promote (alias flip) -> prune old indices

Cross-endpoint moves (local <-> remote over an ``ssh -L`` tunnel) reuse
:func:`copy_index`, which copies mappings first and then documents with
scroll + bulk. Transport and credentials stay outside this module: callers
pass ready clients/URLs, auth travels via environment variables, and no
function logs or returns secrets — errors summarize counts only.

All functions take an injected OpenSearch client (``Any`` — ``opensearch-py``
is untyped), so unit tests run fully offline against fakes.
"""

from __future__ import annotations

from contextlib import suppress
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from openlens.core.interfaces import (
    ConfigError,
    Document,
    ProviderError,
)
from openlens.storage.base import DocumentStore
from openlens.storage.repository import content_hash_of, normalize_url

#: Provider name carried by every ProviderError raised here.
PROVIDER_NAME = "opensearch"

#: Default serving alias (doubles as the provider's configured index name).
DEFAULT_ALIAS = "openlens"

#: Default physical-index prefix; stage indices are `<prefix>_UTCSTAMP`.
DEFAULT_PREFIX = "openlens"

#: Documents per bulk/scroll batch.
DEFAULT_CHUNK_SIZE = 500

#: Scroll context lifetime for copy reads.
_SCROLL_TTL = "2m"

#: Settings applied while an index is being filled (restored by finalize).
_LOAD_SETTINGS = {"index": {"refresh_interval": "-1", "number_of_replicas": 0}}

#: Settings restored once a stage index is fully loaded.
_SERVE_SETTINGS = {"index": {"refresh_interval": "1s"}}

#: Stamp recorded on every loaded document for future recrawl passes.
CRAWL_FIELD = "last_seen_crawl"

#: Content-hash field recorded on every loaded document (change detection).
HASH_FIELD = "content_hash"


def stage_mappings() -> dict[str, Any]:
    """Return the mappings applied to every managed index.

    Mirrors the fields the ``opensearch`` search provider reads
    (``title``/``content``/``url``) plus crawl bookkeeping
    (``content_hash``, ``last_seen_crawl``); ``metadata`` stays dynamic
    so ranker attributes survive without mapping updates.
    """
    return {
        "properties": {
            "title": {"type": "text"},
            "content": {"type": "text"},
            "url": {"type": "keyword"},
            HASH_FIELD: {"type": "keyword"},
            CRAWL_FIELD: {"type": "keyword"},
            "metadata": {"type": "object", "dynamic": True},
        }
    }


def validate_section(section: Any) -> dict[str, Any]:
    """Validate the top-level ``opensearch_index:`` config section.

    Returns a normalized mapping with ``hosts`` (non-empty list),
    ``alias``, ``index_prefix``, ``timeout_s``, ``username``/``password``
    (optional, never logged), and ``verify_certs``. Raises ConfigError
    on any malformed value so boot fails loudly at startup.
    """
    if section is None:
        section = {}
    if not isinstance(section, dict):
        raise ConfigError(
            "Config section 'opensearch_index' must be a mapping. "
            "Startup validation failed."
        )
    raw_hosts = section.get("hosts", [])
    if isinstance(raw_hosts, str):
        raw_hosts = [raw_hosts]
    if (
        not isinstance(raw_hosts, list)
        or not raw_hosts
        or not all(isinstance(h, str) and h for h in raw_hosts)
    ):
        raise ConfigError(
            "Config section 'opensearch_index': 'hosts' must be a non-empty "
            f"list of host URLs, got {raw_hosts!r}. Startup validation failed."
        )
    alias = section.get("alias", DEFAULT_ALIAS)
    if not isinstance(alias, str) or not alias:
        raise ConfigError(
            "Config section 'opensearch_index': 'alias' must be a "
            "non-empty string. Startup validation failed."
        )
    prefix = section.get("index_prefix", DEFAULT_PREFIX)
    if not isinstance(prefix, str) or not prefix:
        raise ConfigError(
            "Config section 'opensearch_index': 'index_prefix' must be a "
            "non-empty string. Startup validation failed."
        )
    timeout_s = section.get("timeout_s", 5)
    if (
        not isinstance(timeout_s, (int, float))
        or isinstance(timeout_s, bool)
        or timeout_s <= 0
    ):
        raise ConfigError(
            "Config section 'opensearch_index': 'timeout_s' must be a "
            "positive number. Startup validation failed."
        )
    return {
        "hosts": list(raw_hosts),
        "alias": alias,
        "index_prefix": prefix,
        "timeout_s": timeout_s,
        "username": section.get("username"),
        "password": section.get("password"),
        "verify_certs": bool(section.get("verify_certs", True)),
    }


def build_client(
    hosts: list[str],
    username: Any | None = None,
    password: Any | None = None,
    verify_certs: bool = True,
    timeout_s: float = 5,
) -> Any:
    """Build an OpenSearch client. Raises ProviderError when unavailable.

    Credentials travel only into the client constructor — never into
    error messages, logs, or return values.
    """
    try:
        from opensearchpy import OpenSearch
    except ImportError as exc:
        raise ProviderError(
            "opensearch-py is not installed",
            provider_name=PROVIDER_NAME,
            retryable=False,
            code=ProviderError.UNAVAILABLE,
        ) from exc
    auth = None
    if username or password:
        auth = (username, password)
    try:
        return OpenSearch(
            hosts=hosts,
            http_auth=auth,
            verify_certs=verify_certs,
            request_timeout=timeout_s,
        )
    except Exception as exc:
        raise ProviderError(
            "cannot build opensearch client",
            provider_name=PROVIDER_NAME,
            retryable=False,
            code=ProviderError.UNAVAILABLE,
        ) from exc


def stage_index_name(prefix: str, now: datetime | None = None) -> str:
    """Return a fresh timestamped physical-index name (UTC, sortable)."""
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d%H%M%S")
    return f"{prefix}_{stamp}"


def _fail(action: str, exc: Exception) -> ProviderError:
    """Wrap a client failure without echoing bodies or credentials."""
    return ProviderError(
        f"opensearch stage {action} failed: {type(exc).__name__}",
        provider_name=PROVIDER_NAME,
        retryable=True,
        code=ProviderError.UNAVAILABLE,
    )


def create_index(
    client: Any, name: str, mappings: Mapping[str, Any] | None = None
) -> str:
    """Create the empty index ``name`` with load settings; return it."""
    body: dict[str, Any] = {
        "settings": dict(_LOAD_SETTINGS),
        "mappings": dict(mappings) if mappings is not None else stage_mappings(),
    }
    try:
        client.indices.create(index=name, body=body)
    except Exception as exc:
        raise _fail("create", exc) from exc
    return name


def create_stage_index(
    client: Any,
    prefix: str,
    now: datetime | None = None,
    mappings: Mapping[str, Any] | None = None,
) -> str:
    """Create an empty timestamped stage index; return its name."""
    return create_index(client, stage_index_name(prefix, now), mappings)


def stage_document_id(doc: Document) -> str:
    """Stable id for a document: normalized URL, else the stored id.

    Upserts on this id make repeat loads idempotent — crawling the same
    URL twice overwrites one document instead of creating a duplicate.
    """
    return normalize_url(doc.url) or doc.id


def to_source_doc(doc: Document, crawl_id: str) -> dict[str, Any]:
    """Render a store Document as an OpenSearch ``_source`` body."""
    return {
        "title": doc.title,
        "content": doc.content,
        "url": doc.url,
        "metadata": dict(doc.metadata or {}),
        HASH_FIELD: content_hash_of(doc.content),
        CRAWL_FIELD: crawl_id,
    }


def iter_store_documents(
    store: DocumentStore, batch_size: int = DEFAULT_CHUNK_SIZE
) -> Iterable[Document]:
    """Yield every document in the store in stable id order."""
    if batch_size < 1:
        raise ValueError("batch_size must be a positive integer")
    offset = 0
    while True:
        page = store.list_documents(limit=batch_size, offset=offset)
        if not page:
            return
        yield from page
        offset += len(page)


def bulk_load(
    client: Any,
    index: str,
    docs: Iterable[Document],
    crawl_id: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> int:
    """Bulk-upsert documents into ``index``; return the count written.

    Idempotent on repeat runs (stable ``_id`` per normalized URL).
    Failures raise ProviderError summarizing counts only — document
    bodies are never echoed into the error.
    """
    actions = (
        {
            "_index": index,
            "_id": stage_document_id(doc),
            "_source": to_source_doc(doc, crawl_id),
        }
        for doc in docs
    )
    try:
        from opensearchpy import helpers

        written, _ = helpers.bulk(client, actions, chunk_size=chunk_size)
    except Exception as exc:
        raise _fail("bulk-load", exc) from exc
    return int(written)


def finalize_stage(client: Any, index: str) -> int:
    """Make a loaded stage index servable; return its document count.

    Restores the serving refresh interval, forces a refresh so every
    document is visible, then returns the count for the verify gate.
    """
    try:
        client.indices.put_settings(index=index, body=_SERVE_SETTINGS)
        client.indices.refresh(index=index)
        response = client.count(index=index)
    except Exception as exc:
        raise _fail("finalize", exc) from exc
    try:
        total = int(response.get("count", 0))
    except (AttributeError, TypeError, ValueError) as exc:
        raise _fail("finalize", exc) from exc
    return total


def verify_stage(client: Any, index: str, min_docs: int = 1) -> int:
    """Verify a stage index is servable; return its document count.

    Requires at least ``min_docs`` documents plus one successful
    match-all read proving the index answers queries. Raises
    ProviderError otherwise (counts only, never document bodies).
    """
    try:
        total = int(client.count(index=index).get("count", 0))
    except Exception as exc:
        raise _fail("verify-count", exc) from exc
    if total < min_docs:
        raise ProviderError(
            f"opensearch stage verify failed: {index} holds {total} "
            f"document(s), need at least {min_docs}",
            provider_name=PROVIDER_NAME,
            retryable=False,
            code=ProviderError.UNAVAILABLE,
        )
    try:
        probe = client.search(index=index, body={"query": {"match_all": {}}, "size": 1})
        hits = probe.get("hits", {}).get("hits", [])
    except Exception as exc:
        raise _fail("verify-search", exc) from exc
    if not hits:
        raise ProviderError(
            f"opensearch stage verify failed: {index} answers no hits",
            provider_name=PROVIDER_NAME,
            retryable=False,
            code=ProviderError.UNAVAILABLE,
        )
    return total


def alias_targets(client: Any, alias: str) -> list[str]:
    """Return the indices ``alias`` currently points at ([] when absent)."""
    try:
        response = client.indices.get_alias(name=alias)
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}".lower()
        if "notfound" in message or "404" in message or "aliasmissing" in message:
            return []
        raise _fail("alias-read", exc) from exc
    if not isinstance(response, dict):
        return []
    return sorted(str(name) for name in response.keys())


def promote(client: Any, alias: str, new_index: str) -> list[str]:
    """Atomically point ``alias`` at ``new_index``; return prior targets.

    One ``update_aliases`` call carries every remove+add, so serving
    never observes a half-state. The previous indices are kept (not
    deleted) for instant rollback via the same function.
    """
    try:
        exists = client.indices.exists(index=new_index)
    except Exception as exc:
        raise _fail("promote-exists", exc) from exc
    if not exists:
        raise ProviderError(
            f"opensearch stage promote failed: {new_index} does not exist",
            provider_name=PROVIDER_NAME,
            retryable=False,
            code=ProviderError.UNAVAILABLE,
        )
    previous = alias_targets(client, alias)
    actions: list[dict[str, Any]] = [
        {"remove": {"index": old, "alias": alias}} for old in previous
    ]
    actions.append({"add": {"index": new_index, "alias": alias}})
    try:
        client.indices.update_aliases(body={"actions": actions})
    except Exception as exc:
        raise _fail("promote", exc) from exc
    return previous


def prune(client: Any, prefix: str, alias: str, keep: int = 2) -> list[str]:
    """Delete superseded timestamped indices; return deleted names.

    Never deletes the indices ``alias`` points at; keeps the ``keep``
    newest remaining ones. Returns [] when there is nothing to prune.
    """
    if keep < 0:
        raise ValueError("keep must be zero or a positive integer")
    try:
        response = client.indices.get(index=f"{prefix}_*")
    except Exception as exc:
        raise _fail("prune-list", exc) from exc
    names: list[str] = (
        sorted(str(name) for name in response.keys())
        if isinstance(response, dict)
        else []
    )
    live = set(alias_targets(client, alias))
    candidates = [name for name in names if name not in live]
    doomed = candidates[: max(0, len(candidates) - keep)]
    deleted: list[str] = []
    for name in doomed:
        try:
            client.indices.delete(index=name)
        except Exception as exc:
            raise _fail("prune-delete", exc) from exc
        deleted.append(name)
    return deleted


def latest_stage_index(client: Any, prefix: str) -> str | None:
    """Return the newest timestamped index, or None when none exists."""
    try:
        response = client.indices.get(index=f"{prefix}_*")
    except Exception as exc:
        raise _fail("stage-list", exc) from exc
    if not isinstance(response, dict) or not response:
        return None
    names: list[str] = sorted(str(name) for name in response.keys())
    return names[-1]


def source_mappings(source: Any, index: str) -> dict[str, Any]:
    """Fetch the mappings of ``index`` from ``source`` (migrate path)."""
    try:
        response = source.indices.get_mapping(index=index)
    except Exception as exc:
        raise _fail("migrate-mapping", exc) from exc
    try:
        mappings = response[index]["mappings"]
    except (KeyError, TypeError) as exc:
        raise _fail("migrate-mapping", exc) from exc
    if not isinstance(mappings, dict):
        raise ProviderError(
            "opensearch stage migrate failed: malformed mappings",
            provider_name=PROVIDER_NAME,
            retryable=False,
            code=ProviderError.UNAVAILABLE,
        )
    return dict(mappings)


def copy_index(
    source: Any,
    dest: Any,
    source_index: str,
    dest_index: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> int:
    """Copy one index between endpoints; return documents written.

    Mappings travel first (recreated on the destination), then documents
    stream over scroll + bulk with ids preserved — a rerun overwrites
    the same ids instead of duplicating. ``source`` and ``dest`` may be
    the same client (same-cluster copy) or two clients over an SSH
    tunnel (local <-> remote). The destination is finalized (servable)
    before returning.
    """
    mappings = source_mappings(source, source_index)
    try:
        already = dest.indices.exists(index=dest_index)
    except Exception as exc:
        raise _fail("migrate-exists", exc) from exc
    if already:
        # Reruns replace the destination: same ids overwrite, stale ids
        # vanish with the old index instead of lingering beside the copy.
        try:
            dest.indices.delete(index=dest_index)
        except Exception as exc:
            raise _fail("migrate-replace", exc) from exc
    create_index(dest, dest_index, mappings)
    try:
        page = source.search(
            index=source_index,
            body={"query": {"match_all": {}}},
            scroll=_SCROLL_TTL,
            size=chunk_size,
        )
        scroll_id = page.get("_scroll_id")
        hits = page.get("hits", {}).get("hits", [])
    except Exception as exc:
        raise _fail("migrate-scroll", exc) from exc
    written = 0
    try:
        from opensearchpy import helpers

        while hits:
            actions = (
                {
                    "_index": dest_index,
                    "_id": hit.get("_id"),
                    "_source": hit.get("_source", {}),
                }
                for hit in hits
            )
            try:
                count, _ = helpers.bulk(dest, actions, chunk_size=chunk_size)
            except Exception as exc:
                raise _fail("migrate-bulk", exc) from exc
            written += int(count)
            try:
                page = source.scroll(scroll_id=scroll_id, scroll=_SCROLL_TTL)
            except Exception as exc:
                raise _fail("migrate-scroll", exc) from exc
            scroll_id = page.get("_scroll_id", scroll_id)
            hits = page.get("hits", {}).get("hits", [])
    finally:
        if scroll_id is not None:
            with suppress(Exception):  # scroll cleanup is best-effort
                source.clear_scroll(scroll_id=scroll_id)
    finalize_stage(dest, dest_index)
    return written

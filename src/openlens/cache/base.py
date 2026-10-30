"""Cache backend contract, key construction, and value serialization."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from abc import ABC, abstractmethod
from typing import Any

from openlens.core.interfaces import SearchResult

#: Key namespace + format version. Bump the version when the canonical
#: key shape changes so old entries can never collide with new ones.
CACHE_KEY_PREFIX = "olcache"
CACHE_KEY_VERSION = "v1"

#: Cache lookup outcomes reported in diagnostics and metrics.
HIT = "hit"
MISS = "miss"
BACKEND_ERROR = "error"


class CacheProvider(ABC):
    """Swappable cache backend. All methods fail open at the call site:
    backends may raise (unreachable server, full disk); the engine hook
    catches everything and degrades to uncached search.
    """

    @abstractmethod
    def get(self, key: str) -> str | None:
        """Return the stored value, or None on miss/expiry. May raise."""
        raise NotImplementedError

    @abstractmethod
    def set(self, key: str, value: str, ttl_s: int) -> None:
        """Store ``value`` for ``ttl_s`` seconds (upsert). May raise."""
        raise NotImplementedError

    @abstractmethod
    def invalidate(self, key: str) -> None:
        """Drop one entry; missing keys are not an error. May raise."""
        raise NotImplementedError

    @abstractmethod
    def clear(self) -> int:
        """Drop every entry; returns the count removed (best effort)."""
        raise NotImplementedError

    @abstractmethod
    def health_check(self) -> bool:
        """True when the backend answers; never raises."""
        raise NotImplementedError

    @abstractmethod
    def close(self) -> None:
        """Release connections; never raises."""
        raise NotImplementedError


def _canonical(value: Any) -> Any:
    """Reduce a value to JSON-canonical form (sorted mappings)."""
    if isinstance(value, dict):
        return {str(key): _canonical(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def build_cache_key(
    *,
    normalized_query: str,
    mode: str,
    filters: dict[str, Any] | None,
    provider_prefs: dict[str, Any] | None,
    provider_names: list[str] | tuple[str, ...],
    ranking_profile: str,
    fetch_limit: int,
    history: tuple[str, ...] | list[str] | None = None,
) -> str:
    """Build the fixed-length cache key for one fetch-depth search.

    Every input that can change the ranked list is in the canonical
    payload — including the restriction filters (user scoping) and the
    configured provider set — so a hit is always safe to serve.
    Request-scoped history joins the payload only when non-empty (it
    feeds context-aware expansion); empty history keeps the legacy key
    so existing cached pages stay addressable.
    """
    canonical: dict[str, Any] = {
        "filters": _canonical(filters or {}),
        "mode": mode,
        "prefs": _canonical(provider_prefs or {}),
        "profile": ranking_profile,
        "providers": sorted(provider_names),
        "query": normalized_query,
        "depth": fetch_limit,
    }
    if history:
        canonical["history"] = _canonical(list(history))
    raw = json.dumps(
        canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"{CACHE_KEY_PREFIX}:{CACHE_KEY_VERSION}:{digest}"


def serialize_cached_page(
    results: list[SearchResult], total: int, applied_filters: list[str]
) -> str:
    """Serialize a ranked page (pre-slice) to its cache value."""
    return json.dumps(
        {
            "results": [dataclasses.asdict(result) for result in results],
            "total": total,
            "applied_filters": list(applied_filters),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )


def restore_cached_page(value: str) -> tuple[list[SearchResult], int, list[str]] | None:
    """Restore a cached page; None when the value is corrupt (treat as miss)."""
    try:
        payload = json.loads(value)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    raw_results = payload.get("results")
    total = payload.get("total")
    applied = payload.get("applied_filters")
    if not isinstance(raw_results, list) or not isinstance(total, int):
        return None
    if not isinstance(applied, list) or not all(isinstance(f, str) for f in applied):
        return None
    try:
        results = [
            SearchResult(**item) for item in raw_results if isinstance(item, dict)
        ]
    except TypeError:
        return None
    if len(results) != len(raw_results):
        return None
    return results, total, list(applied)

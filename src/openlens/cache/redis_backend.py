"""Redis cache backend (optional driver, env-gated tests).

The driver imports lazily so a default install never requires it: when
the ``redis`` package is missing or the server is unreachable, factory
construction raises :class:`ProviderError` with ``UNAVAILABLE`` and the
caller disables caching with a warning instead of crashing.
"""

from __future__ import annotations

from typing import Any

from openlens.cache.base import CACHE_KEY_PREFIX, CacheProvider
from openlens.core.interfaces import ProviderError

_UNAVAILABLE = "UNAVAILABLE"


class RedisCacheProvider(CacheProvider):
    """SETEX-backed entries under the shared key namespace."""

    def __init__(self, url: str, socket_timeout_s: float = 2.0) -> None:
        try:
            import redis  # type: ignore[import-not-found]  # lazy: optional extra
        except ImportError as exc:
            raise ProviderError(
                "redis cache backend needs the 'redis' extra",
                provider_name="cache-redis",
                retryable=False,
                code=ProviderError.UNAVAILABLE,
            ) from exc
        if not isinstance(url, str) or not url:
            raise ValueError("RedisCacheProvider 'url' must be a non-empty string.")
        self._client: Any = redis.Redis.from_url(url, socket_timeout=socket_timeout_s)

    def get(self, key: str) -> str | None:
        raw = self._client.get(key)
        if raw is None:
            return None
        return raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)

    def set(self, key: str, value: str, ttl_s: int) -> None:
        if ttl_s <= 0:
            return
        self._client.setex(key, ttl_s, value)

    def invalidate(self, key: str) -> None:
        self._client.delete(key)

    def clear(self) -> int:
        removed = 0
        for key in self._client.scan_iter(f"{CACHE_KEY_PREFIX}:*", count=500):
            removed += self._client.delete(key)
        return removed

    def health_check(self) -> bool:
        try:
            return bool(self._client.ping())
        except Exception:  # noqa: BLE001 — health must never raise
            return False

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # close is best-effort teardown only
            return

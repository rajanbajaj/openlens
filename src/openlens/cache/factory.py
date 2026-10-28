"""Build a cache backend from the top-level ``cache:`` config section."""

from __future__ import annotations

from typing import Any

from openlens.cache.base import CacheProvider
from openlens.cache.memory import MemoryCacheProvider
from openlens.cache.sqlite import SqliteCacheProvider
from openlens.core.interfaces import ConfigError, ProviderError

#: Backend names accepted by the ``cache.backend`` switch.
SUPPORTED_CACHE_BACKENDS = frozenset({"memory", "sqlite", "redis"})

#: Default entry TTL in seconds when ``cache.ttl_s`` is absent.
DEFAULT_TTL_S = 120

#: Default memory-backend entry bound when ``cache.max_entries`` is absent.
DEFAULT_MAX_ENTRIES = 1000


def _positive_int(section: dict[str, Any], key: str, default: int) -> int:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"Config section 'cache.{key}' must be a positive int.")
    return value


def validate_cache_section(section: Any) -> dict[str, Any]:
    """Normalize the ``cache:`` section; {} stays disabled-neutral.

    Raises ConfigError on malformed sections or unknown backend names so
    boot fails loudly at startup. Absent section means memory defaults.
    """
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise ConfigError("Config section 'cache' must be a mapping.")
    backend = section.get("backend", "memory")
    if not isinstance(backend, str) or backend not in SUPPORTED_CACHE_BACKENDS:
        raise ConfigError(
            f"Unknown cache backend {backend!r}. "
            f"Supported: {sorted(SUPPORTED_CACHE_BACKENDS)}. Startup validation failed."
        )
    enabled = section.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError("Config section 'cache.enabled' must be a bool.")
    if "ttl_s" in section:
        _positive_int(section, "ttl_s", DEFAULT_TTL_S)
    return dict(section)


def build_cache_provider(section: dict[str, Any] | None) -> CacheProvider | None:
    """Construct the configured backend, or None when caching is off.

    Returns None when the section is absent-but-default? No — absent
    section means memory defaults (cache on). Only ``enabled: false``
    disables. A missing/unreachable driver raises ProviderError with
    ``UNAVAILABLE``; callers turn that into a warning, never a crash.
    """
    normalized = validate_cache_section(section)
    if normalized.get("enabled", True) is False:
        return None
    backend = normalized.get("backend", "memory")
    ttl_s = _positive_int(normalized, "ttl_s", DEFAULT_TTL_S)
    _ = ttl_s  # TTL applies per entry at the engine hook, not the backend.
    if backend == "memory":
        max_entries = _positive_int(normalized, "max_entries", DEFAULT_MAX_ENTRIES)
        return MemoryCacheProvider(max_entries=max_entries)
    if backend == "sqlite":
        sqlite_cfg = normalized.get("sqlite", {})
        if not isinstance(sqlite_cfg, dict):
            raise ConfigError("Config section 'cache.sqlite' must be a mapping.")
        path = sqlite_cfg.get("path", "data/openlens-cache.db")
        if not isinstance(path, str) or not path:
            raise ConfigError(
                "Config section 'cache.sqlite.path' must be a non-empty string."
            )
        return SqliteCacheProvider(path)
    redis_cfg = normalized.get("redis", {})
    if not isinstance(redis_cfg, dict):
        raise ConfigError("Config section 'cache.redis' must be a mapping.")
    url = redis_cfg.get("url", "redis://localhost:6379/0")
    if not isinstance(url, str) or not url:
        raise ConfigError(
            "Config section 'cache.redis.url' must be a non-empty string."
        )
    try:
        from openlens.cache.redis_backend import RedisCacheProvider

        provider = RedisCacheProvider(url)
    except ProviderError:
        raise
    if not provider.health_check():
        provider.close()
        raise ProviderError(
            "redis cache backend unreachable",
            provider_name="cache-redis",
            retryable=False,
            code=ProviderError.UNAVAILABLE,
        )
    return provider

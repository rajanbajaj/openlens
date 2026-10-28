"""Cache backends, key construction, and factory validation (Task 21)."""

from __future__ import annotations

import time

import pytest

from openlens.cache.base import (
    build_cache_key,
    restore_cached_page,
    serialize_cached_page,
)
from openlens.cache.factory import (
    SUPPORTED_CACHE_BACKENDS,
    build_cache_provider,
    validate_cache_section,
)
from openlens.cache.memory import MemoryCacheProvider
from openlens.cache.sqlite import SqliteCacheProvider
from openlens.core.interfaces import ConfigError, ProviderError, SearchResult


def _hit(doc_id: str = "d1") -> SearchResult:
    return SearchResult(
        id=doc_id,
        title=f"title-{doc_id}",
        snippet=f"snippet-{doc_id}",
        original_score=1.0,
        final_score=2.0,
        url=f"https://example.com/{doc_id}",
        provider="fake",
        reasons=["ranked"],
        warnings=[],
        metadata={"signals": {"trust": 0.5}},
    )


def _key(**overrides) -> str:
    params = {
        "normalized_query": "red shoes",
        "mode": "shopping",
        "filters": {"max_price": 250},
        "provider_prefs": None,
        "provider_names": ["custom_index"],
        "ranking_profile": "default",
        "fetch_limit": 10,
    }
    params.update(overrides)
    return build_cache_key(**params)  # type: ignore[arg-type]


def test_key_is_fixed_length_and_query_free() -> None:
    key = _key(normalized_query="a very long query " * 200)
    assert key.startswith("olcache:v1:")
    assert len(key) == len("olcache:v1:") + 64
    assert "very long query" not in key


def test_key_covers_every_ranking_input() -> None:
    # Case/whitespace folding is the query processor's job: the key hashes
    # the already-normalized query verbatim, so distinct inputs differ.
    base = _key()
    assert _key(normalized_query="RED SHOES") != base
    assert _key(normalized_query="red  shoes") != base
    assert _key(mode="news") != base
    assert _key(filters={"max_price": 100}) != base
    assert _key(provider_names=["other"]) != base
    assert _key(ranking_profile="news") != base
    assert _key(fetch_limit=20) != base
    assert (
        _key(provider_prefs={"include": ["a"], "exclude": [], "primary": None}) != base
    )


def test_page_round_trip_and_corrupt_values_miss() -> None:
    value = serialize_cached_page([_hit("a"), _hit("b")], 2, ["user-restrictions"])
    restored = restore_cached_page(value)
    assert restored is not None
    results, total, applied = restored
    assert [hit.id for hit in results] == ["a", "b"]
    assert results[0].metadata == {"signals": {"trust": 0.5}}
    assert total == 2
    assert applied == ["user-restrictions"]
    assert restore_cached_page("not-json{{{") is None
    assert (
        restore_cached_page('{"results": [], "total": "x", "applied_filters": []}')
        is None
    )
    assert (
        restore_cached_page(
            '{"results": [{"id": 1}], "total": 1, "applied_filters": []}'
        )
        is None
    )


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_backend_conformance(backend: str, tmp_path) -> None:
    if backend == "memory":
        provider = MemoryCacheProvider(max_entries=10)
    else:
        provider = SqliteCacheProvider(str(tmp_path / "cache.db"))
    try:
        assert provider.health_check() is True
        assert provider.get("missing") is None
        provider.set("k", "v", ttl_s=60)
        assert provider.get("k") == "v"
        provider.set("k", "v2", ttl_s=60)
        assert provider.get("k") == "v2"
        provider.invalidate("k")
        assert provider.get("k") is None
        provider.invalidate("missing")  # not an error
        provider.set("expiring", "v", ttl_s=60)
        # Forced expiry path: zero/negative TTL never stores.
        provider.set("never", "v", ttl_s=0)
        assert provider.get("never") is None
        provider.set("a", "1", ttl_s=60)
        removed = provider.clear()
        assert removed >= 1
        assert provider.get("a") is None
    finally:
        provider.close()


def test_memory_bound_evicts_oldest() -> None:
    provider = MemoryCacheProvider(max_entries=2)
    try:
        provider.set("a", "1", ttl_s=60)
        provider.set("b", "2", ttl_s=60)
        provider.set("c", "3", ttl_s=60)
        assert provider.get("a") is None
        assert provider.get("b") == "2"
        assert provider.get("c") == "3"
    finally:
        provider.close()


def test_memory_rejects_bad_bounds() -> None:
    with pytest.raises(ValueError):
        MemoryCacheProvider(max_entries=0)


def test_factory_validation() -> None:
    assert validate_cache_section(None) == {}
    assert build_cache_provider(None) is not None  # absent section: memory defaults
    assert build_cache_provider({"enabled": False}) is None
    with pytest.raises(ConfigError):
        validate_cache_section({"backend": "memcached"})
    with pytest.raises(ConfigError):
        validate_cache_section({"backend": "memory", "ttl_s": 0})
    with pytest.raises(ConfigError):
        validate_cache_section("memory")
    assert SUPPORTED_CACHE_BACKENDS == frozenset({"memory", "sqlite", "redis"})


def test_factory_redis_without_driver_is_unavailable() -> None:
    try:
        import redis  # noqa: F401
    except ImportError:
        with pytest.raises(ProviderError) as excinfo:
            build_cache_provider({"backend": "redis"})
        assert excinfo.value.code == ProviderError.UNAVAILABLE
    else:
        pytest.skip("redis driver installed; live suite covers this path")


def test_schema_carries_cache_status_to_the_wire() -> None:
    from openlens.api.schemas import DiagnosticsSchema

    hit = DiagnosticsSchema.from_core(
        {
            "fallback_used": False,
            "provider_latencies_ms": {},
            "plugin_executions": [],
            "errors": [],
            "cache": {"status": "hit"},
        }
    )
    assert hit.cache == {"status": "hit"}
    assert DiagnosticsSchema.from_core({}).cache == {}


def test_expiry_is_lazy_but_real(tmp_path) -> None:
    provider = SqliteCacheProvider(str(tmp_path / "cache.db"))
    try:
        provider.set("soon", "v", ttl_s=60)
        assert provider.get("soon") == "v"
        # Rewrite the row as expired behind the backend's back, then read.
        provider._conn.execute(
            "UPDATE cache_entries SET expires_at = ?", (time.time() - 1,)
        )
        provider._conn.commit()
        assert provider.get("soon") is None
    finally:
        provider.close()

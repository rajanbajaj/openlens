"""Redis cache backend conformance (Task 21).

Env-gated like the storage suites: runs only when
``OPENLENS_TEST_REDIS_URL`` is set (CI service containers); skips
without loopback so the offline suite stays green.
"""

from __future__ import annotations

import os

import pytest

from openlens.cache.factory import build_cache_provider
from openlens.core.interfaces import ProviderError

REDIS_URL = os.environ.get("OPENLENS_TEST_REDIS_URL", "").strip()


@pytest.mark.skipif(not REDIS_URL, reason="OPENLENS_TEST_REDIS_URL is not set")
def test_redis_conformance() -> None:
    try:
        provider = build_cache_provider(
            {"backend": "redis", "redis": {"url": REDIS_URL}}
        )
    except ProviderError as exc:
        pytest.skip(f"redis unavailable: {exc}")
    assert provider is not None
    try:
        assert provider.health_check() is True
        assert provider.get("ol-test-missing") is None
        provider.set("ol-test-k", "v", ttl_s=60)
        assert provider.get("ol-test-k") == "v"
        provider.invalidate("ol-test-k")
        assert provider.get("ol-test-k") is None
        provider.set("ol-test-a", "1", ttl_s=60)
        assert provider.clear() >= 1
        assert provider.get("ol-test-a") is None
    finally:
        provider.invalidate("ol-test-k")
        provider.invalidate("ol-test-a")
        provider.close()


def test_redis_unreachable_is_unavailable() -> None:
    with pytest.raises(ProviderError) as excinfo:
        build_cache_provider(
            {"backend": "redis", "redis": {"url": "redis://127.0.0.1:6399/0"}}
        )
    assert excinfo.value.code == ProviderError.UNAVAILABLE

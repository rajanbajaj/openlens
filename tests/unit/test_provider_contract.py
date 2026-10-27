"""Provider-conformance suite: every search backend must pass this.

Covers normalization shape, pagination, bounded timeouts, and failure
mapping. Providers that need the network or a cluster (OpenSearch, Bing)
are exercised through their offline paths: config validation,
response mapping, exception mapping, and offline-safe health checks.
Failures here name the owning provider module (see message prefixes).
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from openlens.core.interfaces import (
    ConfigError,
    ProviderError,
    SearchProvider,
    SearchRequest,
    SearchResult,
)
from openlens.providers import registry
from openlens.providers.base import BaseSearchProvider
from openlens.providers.custom_index import CustomIndexProvider
from openlens.providers.external import BingSearchProvider
from openlens.providers.manager import ProviderManager
from openlens.providers.opensearch import OpenSearchProvider
from tests.fixtures.corpus import fixture_documents, seed_provider


# ---------------------------------------------------------------------------
# Local fakes (timeout / broken-backend behavior)
# ---------------------------------------------------------------------------


class _SlowProvider(BaseSearchProvider):
    @property
    def name(self) -> str:
        return "contract_slow"

    def search(self, request: SearchRequest) -> list[SearchResult]:
        time.sleep(30)  # abandoned by the daemon worker past the timeout
        return []

    def health_check(self) -> bool:
        return True


class _BrokenProvider(BaseSearchProvider):
    @property
    def name(self) -> str:
        return "contract_broken"

    def search(self, request: SearchRequest) -> list[SearchResult]:
        raise ValueError("raw client boom")

    def health_check(self) -> bool:
        raise RuntimeError("health boom")


@pytest.fixture()
def provider_registry():
    registry.register_builtin_providers()
    registry.register("contract_slow", _SlowProvider)
    registry.register("contract_broken", _BrokenProvider)
    yield registry
    for name in ("contract_slow", "contract_broken"):
        registry.unregister(name)


@pytest.fixture()
def seeded_custom_index() -> CustomIndexProvider:
    provider = CustomIndexProvider()
    provider.configure({"db_path": ":memory:", "seed": False})
    seed_provider(provider)
    return provider


# ---------------------------------------------------------------------------
# Helpers: the shared conformance checks
# ---------------------------------------------------------------------------


def assert_normalized_shape(
    results: list[SearchResult], provider_name: str, *, require_reasons: bool = True
) -> None:
    """Every hit carries the normalized contract (providers/custom_index.py etc.).

    Adapter-mapped backends (providers/opensearch.py, providers/external.py)
    bypass retrieval/normalizer.py and currently emit empty `reasons`; those
    suites pass require_reasons=False and rely on the engine EXPLANATION
    stage (core/pipeline.py assemble_explanations) to fill reasons downstream.
    """
    assert results, f"[{provider_name}] expected at least one hit for a corpus query"
    for hit in results:
        assert hit.id, f"[{provider_name}] hit missing id: {hit!r}"
        assert hit.title, f"[{provider_name}] hit missing title: {hit!r}"
        assert isinstance(hit.snippet, str), (
            f"[{provider_name}] snippet must be a string: {hit!r}"
        )
        assert isinstance(hit.original_score, float), (
            f"[{provider_name}] original_score must be float: {hit!r}"
        )
        assert isinstance(hit.final_score, float), (
            f"[{provider_name}] final_score must be float: {hit!r}"
        )
        # Providers must init final_score to the provider signal; ranking
        # owns later changes (core/engine.py defends this too).
        assert hit.final_score == hit.original_score, (
            f"[{provider_name}] final_score must equal original_score pre-ranking"
        )
        assert hit.provider == provider_name, (
            f"[{provider_name}] hit names wrong provider: {hit.provider!r}"
        )
        if require_reasons:
            assert hit.reasons, f"[{provider_name}] hit missing reasons: {hit!r}"


# ---------------------------------------------------------------------------
# CustomIndexProvider: full live conformance
# ---------------------------------------------------------------------------


def test_conformance_normalization_shape(seeded_custom_index):
    results = seeded_custom_index.search(SearchRequest(query="running shoes"))
    assert_normalized_shape(results, seeded_custom_index.name)


def test_conformance_pagination_no_overlap(seeded_custom_index):
    page1 = seeded_custom_index.search(
        SearchRequest(query="shoes", limit=1, offset=0)
    )
    page2 = seeded_custom_index.search(
        SearchRequest(query="shoes", limit=1, offset=1)
    )
    assert len(page1) == 1, "[custom_index] first page must hold exactly 1 hit"
    assert len(page2) <= 1, "[custom_index] second page must hold at most 1 hit"
    if page2:
        assert page1[0].id != page2[0].id, (
            "[custom_index] pages overlap: pagination must slice distinct hits"
        )
    full = seeded_custom_index.search(SearchRequest(query="shoes", limit=10))
    assert len(full) >= len(page1) + len(page2), (
        "[custom_index] paged hits must be a subset of the full result set"
    )


def test_conformance_empty_query_maps_to_provider_error(seeded_custom_index):
    with pytest.raises(ProviderError) as excinfo:
        seeded_custom_index.search(SearchRequest(query="   "))
    assert excinfo.value.code == ProviderError.INVALID_REQUEST, (
        "[custom_index] empty query must map to INVALID_REQUEST, "
        f"got {excinfo.value.code!r}"
    )
    assert excinfo.value.provider_name == seeded_custom_index.name


def test_conformance_bad_pagination_maps_to_provider_error(seeded_custom_index):
    with pytest.raises(ProviderError) as excinfo:
        seeded_custom_index.search(SearchRequest(query="shoes", limit=-1))
    assert excinfo.value.code == ProviderError.INVALID_REQUEST, (
        "[custom_index] negative limit must map to INVALID_REQUEST"
    )


def test_conformance_raw_exceptions_never_leak(seeded_custom_index):
    # Punctuation-only queries parse to an empty FTS query and must return
    # [] rather than leak a sqlite error.
    assert seeded_custom_index.search(SearchRequest(query="!!!")) == []


def test_conformance_health_check_never_raises(seeded_custom_index):
    assert seeded_custom_index.health_check() is True


def test_conformance_bad_config_rejected_at_startup():
    provider = CustomIndexProvider()
    with pytest.raises(ConfigError):
        provider.configure({"db_path": "", "seed": False})
    with pytest.raises(ConfigError):
        provider.configure({"db_path": ":memory:", "seed": "yes"})  # type: ignore[dict-item]


# ---------------------------------------------------------------------------
# Timeout conformance (providers/base.py + providers/manager.py)
# ---------------------------------------------------------------------------


def test_conformance_run_bounded_times_out():
    provider = _SlowProvider()
    provider.configure({"timeout_s": 0.05})
    started = time.monotonic()
    with pytest.raises(ProviderError) as excinfo:
        provider.run_bounded(lambda: provider.search(SearchRequest(query="x")))
    assert excinfo.value.code == ProviderError.TIMEOUT, (
        "[base] overrun must map to ProviderError code 'timeout'"
    )
    assert time.monotonic() - started < 10, "[base] bounded run must not hang"


def test_conformance_manager_enforces_provider_timeout(provider_registry):
    manager = ProviderManager.from_config(
        {
            "providers": {
                "strategy": "single",
                "primary": "slow",
                "slow": {
                    "type": "contract_slow",
                    "enabled": True,
                    "timeout_s": 0.05,
                },
            }
        }
    )
    with pytest.raises(ProviderError) as excinfo:
        manager.search(SearchRequest(query="anything"))
    assert excinfo.value.code == ProviderError.TIMEOUT, (
        "[manager] hung backend must surface as ProviderError code 'timeout'"
    )
    assert excinfo.value.provider_name == "slow"


def test_conformance_manager_wraps_raw_exceptions(provider_registry):
    manager = ProviderManager.from_config(
        {
            "providers": {
                "strategy": "single",
                "primary": "broken",
                "broken": {"type": "contract_broken", "enabled": True},
            }
        }
    )
    with pytest.raises(ProviderError) as excinfo:
        manager.search(SearchRequest(query="anything"))
    # Contract (core/interfaces.py): raw client *types* never leak — the
    # caller sees ProviderError with attribution, never ValueError.
    assert type(excinfo.value) is ProviderError, (
        "[manager] raw client exceptions must surface as ProviderError"
    )
    assert excinfo.value.provider_name == "broken"
    assert excinfo.value.code, "[manager] wrapped errors must carry a code"


def test_conformance_health_check_never_raises_manager(provider_registry):
    manager = ProviderManager.from_config(
        {
            "providers": {
                "strategy": "single",
                "primary": "broken",
                "broken": {"type": "contract_broken", "enabled": True},
            }
        }
    )
    assert manager.health_check() == {"broken": False}


# ---------------------------------------------------------------------------
# OpenSearch adapter: offline conformance (providers/opensearch.py)
# ---------------------------------------------------------------------------


def _opensearch() -> OpenSearchProvider:
    provider = OpenSearchProvider()
    provider.configure({"hosts": ["http://localhost:9200"], "timeout_s": 1.0})
    return provider


def test_conformance_opensearch_normalization_shape():
    provider = _opensearch()
    payload = {
        "hits": {
            "hits": [
                {
                    "_id": "os-1",
                    "_score": 2.5,
                    "_source": {
                        "title": "OS hit",
                        "content": "body text",
                        "url": "https://example.com/os-1",
                    },
                },
                {
                    "_id": "os-2",
                    "_score": None,
                    "_source": {"title": "Second", "content": "more body"},
                },
            ]
        }
    }
    results = provider._normalize(payload, SearchRequest(query="q"))
    assert len(results) == 2
    assert_normalized_shape(results, "opensearch", require_reasons=False)
    assert results[0].original_score == pytest.approx(2.5)
    assert results[1].original_score == 0.0  # null scores coerce, never crash


def test_conformance_opensearch_failure_mapping():
    provider = _opensearch()
    assert provider._map_exception(TimeoutError("timed out")).code == (
        ProviderError.TIMEOUT
    ), "[opensearch] timeouts must map to code 'timeout'"
    assert provider._map_exception(Exception("401 Unauthorized")).code == (
        ProviderError.AUTH
    ), "[opensearch] 401/403 must map to code 'auth'"
    assert provider._map_exception(Exception("429 rate limit hit")).code == (
        ProviderError.RATE_LIMIT
    ), "[opensearch] 429 must map to code 'rate_limit'"
    assert provider._map_exception(Exception("conn refused")).code == (
        ProviderError.UNAVAILABLE
    ), "[opensearch] unknown failures must map to code 'unavailable'"


def test_conformance_opensearch_offline_safe():
    provider = _opensearch()
    assert provider.health_check() is False  # no cluster: False, never raise
    with pytest.raises(ProviderError):
        provider.search(SearchRequest(query="q"))  # never a raw client error


def test_conformance_opensearch_bad_config_rejected():
    with pytest.raises(ConfigError):
        OpenSearchProvider().configure({"hosts": []})
    with pytest.raises(ConfigError):
        OpenSearchProvider().configure({"hosts": ["http://h:9200"], "timeout_s": 0})


# ---------------------------------------------------------------------------
# External (Bing) adapter: offline conformance (providers/external.py)
# ---------------------------------------------------------------------------


def _bing() -> BingSearchProvider:
    provider = BingSearchProvider()
    provider.configure({"api_key": "test-key-not-a-secret", "timeout_s": 1.0})
    return provider


def test_conformance_bing_normalization_shape():
    provider = _bing()
    payload = {
        "webPages": {
            "value": [
                {
                    "id": "https://example.com/a",
                    "name": "A title",
                    "snippet": "A snippet",
                    "url": "https://example.com/a",
                },
                {"name": "B title", "url": "https://example.com/b"},
            ]
        }
    }
    results = provider.parse_payload(payload, SearchRequest(query="q"))
    assert_normalized_shape(results, "bing", require_reasons=False)
    assert results[0].original_score > results[1].original_score, (
        "[bing] rank decay must order earlier hits higher"
    )


def test_conformance_bing_failure_mapping():
    provider = _bing()
    assert provider._map_transport_error(TimeoutError("timeout")).code == (
        ProviderError.TIMEOUT
    ), "[external] timeouts must map to code 'timeout'"
    assert provider._map_transport_error(Exception("401 Unauthorized")).code == (
        ProviderError.AUTH
    ), "[external] 401/403 must map to code 'auth'"
    assert provider._map_transport_error(Exception("429 rate limit")).code == (
        ProviderError.RATE_LIMIT
    ), "[external] 429 must map to code 'rate_limit'"


def test_conformance_bing_requires_key_and_offline_safe():
    with pytest.raises(ConfigError):
        BingSearchProvider().configure({})  # api_key required at startup
    provider = _bing()
    assert provider.health_check() is False  # no network in tests: False


def test_conformance_fixture_corpus_is_stable():
    docs = fixture_documents()
    assert len(docs) == 8, "[fixtures/corpus] corpus size is a contract"
    assert len({d.id for d in docs}) == len(docs), (
        "[fixtures/corpus] document ids must be unique"
    )
    assert all(d.url for d in docs), "[fixtures/corpus] every doc needs a URL"

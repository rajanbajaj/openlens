"""Provider-conformance suite: every search backend must pass this.

Covers normalization shape, pagination, bounded timeouts, and failure
mapping for the local custom_index backend. Third-party search APIs and
the OpenSearch adapter were removed (offline-first). Failures here name
the owning provider module (see message prefixes).
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from openlens.core.interfaces import (
    ConfigError,
    ProviderError,
    SearchRequest,
    SearchResult,
)
from openlens.providers import registry
from openlens.providers.base import BaseSearchProvider
from openlens.providers.custom_index import CustomIndexProvider
from openlens.providers.manager import ProviderManager
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
    """Every hit carries the normalized contract (providers/custom_index.py)."""
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
    page1 = seeded_custom_index.search(SearchRequest(query="shoes", limit=1, offset=0))
    page2 = seeded_custom_index.search(SearchRequest(query="shoes", limit=1, offset=1))
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
# Removed backends: third-party search APIs (providers/external.py,
# providers/http.py) and the OpenSearch adapter
# (providers/opensearch.py, indexing/opensearch_stage.py) were deleted
# (offline-first, SQLite-only). The registry must not resolve their type
# names anymore.
# ---------------------------------------------------------------------------


def test_conformance_removed_external_types_unknown(
    provider_registry: Any,
) -> None:
    for removed in ("bing", "http", "external", "opensearch"):
        with pytest.raises(ConfigError):
            provider_registry.get(removed)


def test_conformance_fixture_corpus_is_stable():
    docs = fixture_documents()
    assert len(docs) == 8, "[fixtures/corpus] corpus size is a contract"
    assert len({d.id for d in docs}) == len(docs), (
        "[fixtures/corpus] document ids must be unique"
    )
    assert all(d.url for d in docs), "[fixtures/corpus] every doc needs a URL"

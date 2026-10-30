"""Smoke tests for the Wave-1b provider system.

All tests are offline: fakes stand in for every backend. Third-party
search APIs and the OpenSearch adapter were removed: only the local
custom_index backend ships.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from openlens.config import load_config
from openlens.core.context import SearchContext
from openlens.core.interfaces import (
    ConfigError,
    ProviderError,
    SearchRequest,
    SearchResult,
)
from openlens.providers import registry
from openlens.providers.base import BaseSearchProvider
from openlens.providers.capabilities import describe
from openlens.providers.manager import ProviderManager


# ---------------------------------------------------------------------------
# Fakes (registered under unique test-only type names)
# ---------------------------------------------------------------------------


class FakeFastProvider(BaseSearchProvider):
    @property
    def name(self) -> str:
        return "fake_fast"

    def search(self, request: SearchRequest) -> list[SearchResult]:
        return [
            SearchResult(
                id="fake-1",
                title="fast hit",
                snippet="canned",
                original_score=0.9,
                final_score=0.9,
                provider="fake_fast",
            )
        ]

    def health_check(self) -> bool:
        return True


class FakeSlowProvider(BaseSearchProvider):
    @property
    def name(self) -> str:
        return "fake_slow"

    def search(self, request: SearchRequest) -> list[SearchResult]:
        time.sleep(30)  # abandoned by the daemon worker past the timeout
        return []

    def health_check(self) -> bool:
        return True


class FakeBrokenProvider(BaseSearchProvider):
    @property
    def name(self) -> str:
        return "fake_broken"

    def search(self, request: SearchRequest) -> list[SearchResult]:
        raise ValueError("raw client boom")  # must be wrapped, never leak

    def health_check(self) -> bool:
        raise RuntimeError("health boom")  # must become False, never raise


@pytest.fixture(autouse=True)
def _test_provider_types():
    registry.register_builtin_providers()
    registry.register("fake_fast", FakeFastProvider)
    registry.register("fake_slow", FakeSlowProvider)
    registry.register("fake_broken", FakeBrokenProvider)
    yield
    for name in ("fake_fast", "fake_slow", "fake_broken"):
        registry.unregister(name)


def _config(**entries: Any) -> dict[str, Any]:
    return {"providers": {"strategy": "single", **entries}}


# ---------------------------------------------------------------------------
# Config toggles: only enabled providers instantiate
# ---------------------------------------------------------------------------


def test_disabled_providers_are_absent():
    manager = ProviderManager.from_config(
        _config(
            primary="fast",
            fast={"type": "fake_fast", "enabled": True},
            search_cluster={"type": "fake_fast", "enabled": False},
        )
    )
    assert manager.provider_names == ["fast"]
    assert manager.primary == "fast"
    with pytest.raises(ConfigError):
        manager.get("search_cluster")


def test_config_toggle_enables_provider_without_code_changes():
    base = {
        "primary": "fast",
        "fast": {"type": "fake_fast", "enabled": True},
        "search_cluster": {"type": "fake_fast", "enabled": False},
    }
    assert ProviderManager.from_config(_config(**base)).provider_names == ["fast"]
    base["search_cluster"]["enabled"] = True  # type: ignore[index]
    assert ProviderManager.from_config(_config(**base)).provider_names == [
        "fast",
        "search_cluster",
    ]


def test_single_strategy_routes_to_primary():
    manager = ProviderManager.from_config(
        _config(
            primary="second",
            first={"type": "fake_fast", "enabled": True},
            second={"type": "fake_fast", "enabled": True},
        )
    )
    results = manager.search(SearchRequest(query="q"))
    assert len(results) == 1 and results[0].provider == "fake_fast"
    assert manager.health_check() == {"first": True, "second": True}


def test_default_primary_is_first_enabled():
    manager = ProviderManager.from_config(
        _config(first={"type": "fake_fast", "enabled": True})
    )
    assert manager.primary == "first"


# ---------------------------------------------------------------------------
# Startup validation rejects bad config
# ---------------------------------------------------------------------------


def test_unknown_provider_type_rejected():
    with pytest.raises(ConfigError):
        ProviderManager.from_config(
            _config(primary="x", x={"type": "no_such_backend", "enabled": True})
        )


def test_unknown_strategy_rejected():
    with pytest.raises(ConfigError):
        ProviderManager.from_config(
            {
                "providers": {
                    "strategy": "round_robin",
                    "primary": "fast",
                    "fast": {"type": "fake_fast", "enabled": True},
                }
            }
        )


def test_cost_per_call_rejects_non_number():
    for bad in (True, -1.0, "0.5", None):
        with pytest.raises(ConfigError):
            ProviderManager.from_config(
                _config(
                    primary="fast",
                    fast={
                        "type": "fake_fast",
                        "enabled": True,
                        "cost_per_call": bad,
                    },
                )
            )


def test_cost_per_call_recorded_per_request():
    manager = ProviderManager.from_config(
        _config(
            primary="fast",
            fast={
                "type": "fake_fast",
                "enabled": True,
                "cost_per_call": 0.5,
            },
        )
    )
    context = SearchContext(request=SearchRequest(query="fast hit"))
    hits = manager.search(context.request, context)
    assert len(hits) == 1
    assert context.provider_result_counts == {"fast": 1}
    assert context.provider_costs_estimated == {"fast": 0.5}


def test_primary_must_be_enabled():
    with pytest.raises(ConfigError):
        ProviderManager.from_config(
            _config(
                primary="off",
                fast={"type": "fake_fast", "enabled": True},
                off={"type": "fake_fast", "enabled": False},
            )
        )


def test_no_enabled_providers_rejected():
    with pytest.raises(ConfigError):
        ProviderManager.from_config(
            _config(fast={"type": "fake_fast", "enabled": False})
        )


def test_providers_section_must_be_mapping():
    with pytest.raises(ConfigError):
        ProviderManager.from_config({"providers": ["not", "a", "mapping"]})


@pytest.mark.parametrize("bad_timeout", [0, -1, "5", None, True, 10_000])
def test_invalid_timeout_rejected(bad_timeout: Any):
    provider = FakeFastProvider()
    with pytest.raises(ConfigError):
        provider.configure({"timeout_s": bad_timeout})


# ---------------------------------------------------------------------------
# Bounded timeouts, error wrapping, health checks
# ---------------------------------------------------------------------------


def test_timeout_is_bounded():
    manager = ProviderManager.from_config(
        _config(
            primary="slow",
            slow={"type": "fake_slow", "enabled": True, "timeout_s": 0.2},
        )
    )
    start = time.monotonic()
    with pytest.raises(ProviderError) as exc_info:
        manager.search(SearchRequest(query="q"))
    elapsed = time.monotonic() - start
    assert exc_info.value.code == ProviderError.TIMEOUT
    assert exc_info.value.provider_name == "slow"
    assert elapsed < 10  # far below the fake's 30s hang


def test_run_bounded_helper_times_out():
    provider = FakeFastProvider()
    provider.configure({"timeout_s": 0.1})
    with pytest.raises(ProviderError) as exc_info:
        provider.run_bounded(lambda: time.sleep(30) or [])
    assert exc_info.value.code == ProviderError.TIMEOUT


def test_raw_backend_exceptions_are_wrapped():
    manager = ProviderManager.from_config(
        _config(
            primary="broken",
            broken={"type": "fake_broken", "enabled": True},
        )
    )
    with pytest.raises(ProviderError) as exc_info:
        manager.search(SearchRequest(query="q"))
    assert "raw client boom" in str(exc_info.value)
    assert exc_info.value.provider_name == "broken"
    assert not isinstance(exc_info.value, ValueError)


def test_health_check_never_raises():
    manager = ProviderManager.from_config(
        _config(
            primary="broken",
            broken={"type": "fake_broken", "enabled": True},
        )
    )
    assert manager.health_check() == {"broken": False}


def test_provider_latency_recorded_in_context():
    manager = ProviderManager.from_config(
        _config(
            primary="fast",
            fast={"type": "fake_fast", "enabled": True},
        )
    )
    ctx = SearchContext(request=SearchRequest(query="q"))
    manager.search(SearchRequest(query="q"), context=ctx)
    assert "fast" in ctx.provider_latencies_ms
    assert ctx.provider_latencies_ms["fast"] >= 0.0


# ---------------------------------------------------------------------------
# Adapter mapping + offline safety (no network, no cluster, no keys)
# ---------------------------------------------------------------------------


def test_capabilities_known_types():
    assert describe("custom_index").requires_network is False
    assert describe("whatever-unknown").name == "unknown"


def test_removed_external_types_unknown():
    # Third-party search APIs and the OpenSearch adapter were removed:
    # the registry must not resolve them anymore.
    from openlens.providers.registry import get

    for removed in ("bing", "http", "external", "opensearch"):
        with pytest.raises(ConfigError):
            get(removed)


def test_shipped_yaml_has_no_network_providers(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[2]
    yaml_path = repo_root / "configs" / "providers.yaml"
    loaded = load_config(yaml_path)
    section = loaded["providers"]
    assert section["strategy"] == "single"
    # OpenSearch and third-party search APIs were removed: no
    # opensearch/bing/http entries ship; custom_index serves alone.
    assert "opensearch" not in section
    assert "bing" not in section
    assert "http" not in section

    # Same file boots a manager once the retrieval wave's type exists;
    # until then, its entry name still resolves to a registered test fake.
    section["custom_index"]["type"] = "fake_fast"
    manager = ProviderManager.from_config(loaded)
    assert manager.primary == "custom_index"
    assert manager.provider_names == ["custom_index"]

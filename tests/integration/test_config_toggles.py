"""Config-toggle tests: providers and plugins switch via config only.

No test here edits source: every toggle goes through
ProviderManager.from_config or SearchEngine.configure with plain dicts,
proving enable/disable needs no core changes.
"""

from __future__ import annotations

import pytest

from openlens.core.engine import SearchEngine
from openlens.core.interfaces import (
    ConfigError,
    SearchRequest,
    SearchResult,
)
from openlens.providers import registry
from openlens.providers.base import BaseSearchProvider
from openlens.providers.custom_index import CustomIndexProvider
from openlens.providers.manager import ProviderManager
from tests.fixtures.corpus import seed_provider


class _ToggleFakeProvider(BaseSearchProvider):
    """Thread-safe in-memory provider for manager-level toggle tests.

    CustomIndexProvider is intentionally NOT searched through the manager
    here: storage/repository.py binds its sqlite connection to the creating
    thread while providers/manager.py searches on a worker thread (see
    Wave-3 note). Construction-only toggles below still use custom_index.
    """

    @property
    def name(self) -> str:
        return "toggle_fake"

    def search(self, request: SearchRequest) -> list[SearchResult]:
        return [
            SearchResult(
                id="t1",
                title="toggle hit",
                snippet="s",
                original_score=1.0,
                final_score=1.0,
                provider="toggle_fake",
                reasons=["toggle"],
            )
        ]

    def health_check(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def _builtins():
    registry.register_builtin_providers()
    registry.register("toggle_fake", _ToggleFakeProvider)
    yield
    registry.unregister("toggle_fake")


def _manager(entries: dict) -> ProviderManager:
    return ProviderManager.from_config({"providers": {"strategy": "single", **entries}})


def test_provider_toggle_disable_without_code_changes():
    on = _manager(
        {
            "primary": "main",
            "main": {"type": "toggle_fake", "enabled": True},
            "spare": {"type": "custom_index", "enabled": False},
        }
    )
    assert on.provider_names == ["main"]
    off = _manager(
        {
            "primary": "main",
            "main": {"type": "toggle_fake", "enabled": True},
        }
    )
    assert off.provider_names == ["main"]
    assert off.search(SearchRequest(query="shoes")) != [], (
        "[providers/manager.py] enabled provider must still serve after toggle"
    )


def test_provider_toggle_custom_index_construction():
    manager = _manager(
        {
            "primary": "main",
            "main": {"type": "custom_index", "enabled": True, "seed": True},
            "spare": {"type": "custom_index", "enabled": False},
        }
    )
    assert manager.provider_names == ["main"]
    assert manager.get("main").health_check() is True


def test_provider_primary_switch_without_code_changes():
    manager = _manager(
        {
            "primary": "second",
            "first": {"type": "toggle_fake", "enabled": True},
            "second": {"type": "toggle_fake", "enabled": True},
        }
    )
    assert manager.primary == "second"
    assert manager.search(SearchRequest(query="shoes")) != []


def test_provider_no_enabled_providers_rejected():
    with pytest.raises(ConfigError):
        _manager({"main": {"type": "custom_index", "enabled": False}})


def _engine_with_plugins(plugin_entries: list[dict]) -> SearchEngine:
    provider = CustomIndexProvider()
    provider.configure({"db_path": ":memory:", "seed": False})
    seed_provider(provider)
    engine = SearchEngine(providers=[provider], plugins=[])
    engine.configure({"plugins": plugin_entries, "ranking": {}})
    return engine


def test_plugin_toggle_filter_without_code_changes():
    unsafe_query = SearchRequest(query="malware", limit=10)
    unfiltered = _engine_with_plugins([]).search(unsafe_query)
    filtered = _engine_with_plugins(
        [{"name": "safety-filter", "enabled": True, "config": {}}]
    ).search(unsafe_query)
    assert len(filtered.results) < len(unfiltered.results), (
        "[core/engine.py] enabling safety-filter purely via config must "
        "remove unsafe hits"
    )
    assert "safety-filter" in filtered.applied_filters


def test_plugin_toggle_disable_restores_results():
    query = SearchRequest(query="coast", limit=10)
    with_filter = _engine_with_plugins(
        [
            {
                "name": "safety-filter",
                "enabled": True,
                "config": {"blocked_terms": ["coast"]},
            }
        ]
    ).search(query)
    without_filter = _engine_with_plugins(
        [
            {
                "name": "safety-filter",
                "enabled": False,
                "config": {"blocked_terms": ["coast"]},
            }
        ]
    ).search(query)
    assert len(without_filter.results) > len(with_filter.results), (
        "[core/registry.py] disabling a plugin via config must restore hits"
    )


def test_plugin_toggle_enricher_without_code_changes():
    query = SearchRequest(query="cure", limit=10)
    plain = _engine_with_plugins([]).search(query)
    enriched = _engine_with_plugins(
        [{"name": "trust-enricher", "enabled": True, "config": {}}]
    ).search(query)
    assert len(enriched.results) == len(plain.results), (
        "[plugins] enabling an enricher must not change hit count"
    )
    assert any(r.warnings for r in enriched.results), (
        "[plugins/trust_enricher.py] enabled enricher must add warnings"
    )


def test_engine_rejects_unknown_ranking_profile():
    engine = _engine_with_plugins([])
    with pytest.raises(ConfigError):
        engine.configure({"plugins": [], "ranking": {"default_profile": "nope"}})


# ---------------------------------------------------------------------------
# Generic HTTP provider: enable/disable via config dicts only
# ---------------------------------------------------------------------------


def _http_entry(*, enabled: bool, **overrides: object) -> dict:
    entry: dict = {
        "type": "http",
        "enabled": enabled,
        "endpoint": "https://api.example.com/search",
        "api_key": "toggle-key-not-a-secret",
        "timeout_s": 5,
    }
    entry.update(overrides)
    return entry


_HTTP_TOGGLE_PAYLOAD = {
    "results": [{"id": "h1", "title": "Toggle hit", "snippet": "served via http"}]
}


def test_provider_toggle_http_enable_disable_via_config_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openlens.providers.http import CustomApiSearchProvider

    monkeypatch.setattr(
        CustomApiSearchProvider, "_fetch", lambda self, request: _HTTP_TOGGLE_PAYLOAD
    )
    manager = _manager({"primary": "ext", "ext": _http_entry(enabled=True)})
    assert manager.provider_names == ["ext"], (
        "[providers/manager.py] enabled http entry must be constructed from config"
    )
    results = manager.search(SearchRequest(query="shoes"))
    assert [r.id for r in results] == ["h1"], (
        "[providers/http.py] enabled http entry must serve searches "
        "with no code changes"
    )
    mixed = _manager(
        {
            "primary": "main",
            "main": {"type": "toggle_fake", "enabled": True},
            "ext": _http_entry(enabled=False),
        }
    )
    assert mixed.provider_names == ["main"], (
        "[providers/manager.py] disabled http entry must be skipped "
        "without code changes"
    )
    with pytest.raises(ConfigError):
        mixed.get("ext")


def test_provider_toggle_http_disabled_needs_no_key() -> None:
    manager = _manager(
        {
            "primary": "main",
            "main": {"type": "toggle_fake", "enabled": True},
            "ext": {"type": "http", "enabled": False},
        }
    )
    assert manager.provider_names == ["main"], (
        "[providers/manager.py] disabled http entry must not fail startup validation"
    )


def test_provider_toggle_http_enabled_requires_key() -> None:
    with pytest.raises(ConfigError):
        _manager(
            {
                "primary": "ext",
                "ext": {
                    "type": "http",
                    "enabled": True,
                    "endpoint": "https://api.example.com/search",
                },
            }
        )

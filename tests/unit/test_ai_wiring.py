"""Step-1 wiring: optional ModelManager, manager-primary injection, models.yaml.

Offline only: fake backend, in-memory provider, no network or keys.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from openlens.core.engine import SearchEngine
from openlens.core.interfaces import ConfigError, SearchRequest, SearchResult
from openlens.core.registry import AI_PLUGIN_NAMES, BUILTIN_PLUGINS, PluginRegistry
from openlens.main import build_engine, load_app_config
from openlens.models.fake import FakeModelProvider
from openlens.models.registry import register, register_builtin_models, unregister
from openlens.plugins.ai_summary import AISummaryPlugin
from openlens.providers.base import BaseSearchProvider

REPO_ROOT = Path(__file__).resolve().parents[2]

ALL_DISABLED: dict[str, Any] = {
    "primary": "fake",
    "fake": {"enabled": False, "type": "fake"},
    "claude": {"enabled": False, "type": "claude"},
}


def _hits() -> list[SearchResult]:
    return [
        SearchResult(
            id="1",
            title="Cats",
            snippet="Cats nap.",
            original_score=2.0,
            final_score=2.0,
            provider="fake-search",
        ),
        SearchResult(
            id="2",
            title="Dogs",
            snippet="Dogs bark.",
            original_score=1.0,
            final_score=1.0,
            provider="fake-search",
        ),
    ]


class _FakeSearch(BaseSearchProvider):
    @property
    def name(self) -> str:
        return "fake-search"

    def search(self, request: SearchRequest) -> list[SearchResult]:
        # Fresh copies per call: enrichment mutates result metadata.
        return _hits()[: request.limit]

    def health_check(self) -> bool:
        return True


def _engine(models: Any, plugins: list[dict[str, Any]]) -> SearchEngine:
    engine = SearchEngine(providers=[_FakeSearch()])
    engine.configure(
        {
            "plugins": plugins,
            "ranking": {"default_profile": "default"},
            "models": models,
        }
    )
    return engine


def _app_config(models: Any, plugins: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "providers": {
            "strategy": "single",
            "primary": "custom",
            "custom": {
                "enabled": True,
                "type": "custom_index",
                "db_path": ":memory:",
            },
        },
        "plugins": plugins,
        "ranking": {"default_profile": "default"},
        "models": models,
    }


# -- registry -----------------------------------------------------------------


def test_registry_registers_ai_plugins() -> None:
    assert (
        BUILTIN_PLUGINS["ai-summary"] == "openlens.plugins.ai_summary:AISummaryPlugin"
    )
    assert (
        BUILTIN_PLUGINS["ai-understanding"]
        == "openlens.plugins.ai_understanding:AIUnderstandingPlugin"
    )
    assert (
        BUILTIN_PLUGINS["ai-classification"]
        == "openlens.plugins.ai_classification:AIClassificationPlugin"
    )
    assert AI_PLUGIN_NAMES == {"ai-summary", "ai-understanding", "ai-classification"}
    registry = PluginRegistry()
    assert {"ai-summary", "ai-understanding", "ai-classification"} <= set(
        registry.registered_names()
    )
    assert registry.resolve("ai-summary") is AISummaryPlugin


# -- all-disabled boots -------------------------------------------------------


def test_all_disabled_boots_with_no_manager() -> None:
    engine = build_engine(_app_config(ALL_DISABLED, []))
    assert engine.model_manager is None
    assert engine._model_manager is None


def test_all_disabled_searches_identical_minus_ai_fields() -> None:
    plugins = [{"name": "ai-summary", "enabled": True, "config": {}}]
    disabled = _engine(ALL_DISABLED, plugins)
    assert disabled.model_manager is None
    enabled = _engine(
        {
            "primary": "fake",
            "fake": {"enabled": True, "type": "fake", "response_prefix": "routed"},
        },
        plugins,
    )
    off = disabled.search(SearchRequest(query="cats", limit=10, offset=0))
    on = enabled.search(SearchRequest(query="cats", limit=10, offset=0))
    assert [r.id for r in off.results] == [r.id for r in on.results] == ["1", "2"]
    assert [r.title for r in off.results] == [r.title for r in on.results]
    assert off.total == on.total == 2
    assert all("ai_summary" not in r.metadata for r in off.results)
    assert all("ai_model" not in r.metadata for r in off.results)
    assert all("ai_summary" in r.metadata for r in on.results)


def test_shipped_models_yaml_boots_without_ai() -> None:
    config = load_app_config(REPO_ROOT / "configs" / "default.yaml")
    assert "models" in config
    assert all(
        not entry.get("enabled", False)
        for key, entry in config["models"].items()
        if key != "primary" and isinstance(entry, dict)
    )
    engine = build_engine(config)
    assert engine.model_manager is None


# -- fake-enabled routing -----------------------------------------------------


def test_fake_enabled_routes_through_primary() -> None:
    plugins = [{"name": "ai-summary", "enabled": True, "config": {}}]
    engine = _engine(
        {
            "primary": "fake",
            "fake": {"enabled": True, "type": "fake", "response_prefix": "routed"},
        },
        plugins,
    )
    assert engine.model_manager is not None
    assert engine.model_manager.primary == "fake"
    plugin = next(p for p in engine.pipeline.plugins if p.name == "ai-summary")
    assert isinstance(plugin, AISummaryPlugin)
    # Same instance as the manager's primary: one manager, one backend.
    assert plugin.model is engine.model_manager.get("fake")
    response = engine.search(SearchRequest(query="cats", limit=10, offset=0))
    assert response.results
    for result in response.results:
        assert result.metadata["ai_summary"].startswith("routed completion:")
        assert result.metadata["ai_model"] == "fake"


def test_manager_primary_injected_via_build_engine() -> None:
    engine = build_engine(
        _app_config(
            {
                "primary": "fake",
                "fake": {"enabled": True, "type": "fake", "response_prefix": "wired"},
            },
            [{"name": "ai-summary", "enabled": True, "config": {}}],
        )
    )
    assert engine.model_manager is not None
    assert engine.model_manager.primary == "fake"


def test_constructor_injection_still_works() -> None:
    # Existing AISummaryPlugin(model=...) injection used by test_ai_seams.
    fake = FakeModelProvider()
    plugin = AISummaryPlugin(model=fake)
    assert plugin.model is fake


# -- unknown primary ----------------------------------------------------------


def test_unknown_primary_fails_startup() -> None:
    bad = {
        "primary": "ghost",
        "fake": {"enabled": True, "type": "fake"},
    }
    with pytest.raises(ConfigError):
        _engine(bad, [])
    with pytest.raises(ConfigError):
        build_engine(_app_config(bad, []))


# -- disabled never instantiated ----------------------------------------------


def test_disabled_backends_never_instantiated() -> None:
    register_builtin_models()

    class Boom(FakeModelProvider):
        def __init__(self) -> None:  # pragma: no cover - must never run
            raise AssertionError("disabled backend was instantiated")

        @property
        def name(self) -> str:
            return "boom-wiring-only"

    register("boom-wiring-only", Boom)
    try:
        engine = _engine(
            {
                "primary": "fake",
                "fake": {"enabled": True, "type": "fake"},
                "boom": {"enabled": False, "type": "boom-wiring-only"},
                "ghost": {"enabled": False, "type": "no-such-type"},
            },
            [],
        )
        assert engine.model_manager is not None
        assert engine.model_manager.backend_names == ["fake"]
        # All-disabled variant also never touches Boom.
        quiet = _engine(
            {
                "primary": "fake",
                "fake": {"enabled": False, "type": "fake"},
                "boom": {"enabled": False, "type": "boom-wiring-only"},
            },
            [],
        )
        assert quiet.model_manager is None
    finally:
        unregister("boom-wiring-only")


# -- second-file models source -------------------------------------------------


def test_models_yaml_second_file_merges_and_resolves_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    (cfg_dir / "default.yaml").write_text(
        "providers:\n"
        "  strategy: single\n"
        "  primary: custom\n"
        "  custom:\n"
        "    enabled: true\n"
        "    type: custom_index\n"
        "    db_path: ':memory:'\n"
        "plugins: []\n"
        "ranking:\n"
        "  default_profile: default\n"
    )
    (cfg_dir / "models.yaml").write_text(
        "models:\n"
        "  primary: fake\n"
        "  fake:\n"
        "    enabled: true\n"
        "    type: fake\n"
        "    response_prefix: ${OPENLENS_WIRING_PREFIX:-fromfile}\n"
    )
    monkeypatch.setenv("OPENLENS_WIRING_PREFIX", "fromenv")
    config = load_app_config(cfg_dir / "default.yaml")
    assert config["models"]["fake"]["enabled"] is True
    assert config["models"]["fake"]["response_prefix"] == "fromenv"
    engine = build_engine(config)
    assert engine.model_manager is not None
    assert "fromenv" in engine.model_manager.complete("hello")

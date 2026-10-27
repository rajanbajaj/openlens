"""Smoke tests for the AI seams: registry, fake backend, models.yaml, plugin stub.

Offline only: no network, no API keys, no live model calls.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openlens.config import load_config
from openlens.core.context import SearchContext
from openlens.core.interfaces import ConfigError, ProviderError, SearchRequest, SearchResult
from openlens.models.backends import (
    ClaudeModelProvider,
    LocalModelProvider,
    MuseModelProvider,
)
from openlens.models.fake import FakeModelProvider
from openlens.models.interfaces import AIModelProvider
from openlens.models.manager import ModelManager
from openlens.models.registry import (
    available,
    create,
    get,
    register,
    register_builtin_models,
    unregister,
)
from openlens.plugins.ai_summary import AISummaryPlugin

REPO_ROOT = Path(__file__).resolve().parents[2]


def _result(id: str = "1", title: str = "Cats", snippet: str = "Cats nap.") -> SearchResult:
    return SearchResult(
        id=id, title=title, snippet=snippet, original_score=1.0, final_score=1.0
    )


def _context() -> SearchContext:
    return SearchContext(
        request=SearchRequest(query="cats", mode="default", limit=10, offset=0),
        results=[_result("1"), _result("2", title="Dogs", snippet="Dogs bark.")],
    )


# -- interface + fake backend ------------------------------------------------


def test_fake_implements_full_interface() -> None:
    model = FakeModelProvider()
    assert isinstance(model, AIModelProvider)
    assert model.health_check() is True
    completion = model.complete("hello world")
    assert "hello world" in completion
    vectors = model.embed(["alpha", "beta"])
    assert len(vectors) == 2 and all(len(v) == 8 for v in vectors)
    assert model.classify("I love sports", ["sports", "news"]) == "sports"
    assert model.classify("something else", ["sports", "news"]) == "sports"


def test_fake_is_deterministic_offline() -> None:
    model = FakeModelProvider()
    model.configure({})
    assert model.embed(["same input"]) == model.embed(["same input"])
    assert model.embed(["a"]) != model.embed(["b"])
    assert model.complete("prompt") == model.complete("prompt")


def test_fake_rejects_bad_calls_with_provider_error() -> None:
    model = FakeModelProvider()
    with pytest.raises(ProviderError):
        model.complete("   ")
    with pytest.raises(ProviderError):
        model.classify("text", [])
    with pytest.raises(ConfigError):
        model.configure({"embedding_dim": 9999})


def test_real_backend_stubs_never_call_live() -> None:
    claude = ClaudeModelProvider()
    claude.configure({"api_key": "test-key"})
    assert claude.health_check() is False  # offline-safe, no network touched
    with pytest.raises(ProviderError) as exc_info:
        claude.complete("hi")
    assert exc_info.value.code == ProviderError.UNAVAILABLE

    muse = MuseModelProvider()
    muse.configure({"api_key": "test-key"})
    assert muse.health_check() is False
    with pytest.raises(ProviderError):
        muse.embed(["hi"])

    local = LocalModelProvider()
    local.configure({"endpoint": "http://localhost:9"})
    assert local.health_check() is False
    with pytest.raises(ProviderError):
        local.classify("hi", ["a"])


def test_real_backend_stubs_require_credentials_when_enabled() -> None:
    with pytest.raises(ConfigError):
        ClaudeModelProvider().configure({})
    with pytest.raises(ConfigError):
        MuseModelProvider().configure({})
    with pytest.raises(ConfigError):
        LocalModelProvider().configure({})


# -- registry ----------------------------------------------------------------


def test_registry_lists_all_builtin_types() -> None:
    register_builtin_models()
    assert {"fake", "claude", "muse", "local"} <= set(available())
    assert issubclass(get("fake"), AIModelProvider)


def test_registry_rejects_unknown_and_invalid() -> None:
    register_builtin_models()
    with pytest.raises(ConfigError):
        get("no-such-backend")
    with pytest.raises(ConfigError):
        register("bad", object)  # type: ignore[arg-type]


def test_registry_custom_backend_round_trip() -> None:
    register_builtin_models()

    class Custom(FakeModelProvider):
        @property
        def name(self) -> str:
            return "custom-test-only"

    register("custom-test-only", Custom)
    try:
        assert isinstance(create("custom-test-only", {}), Custom)
    finally:
        unregister("custom-test-only")


# -- manager: config swaps + disabled-never-instantiate ----------------------


def test_manager_fake_backend_swaps_via_config() -> None:
    manager_a = ModelManager.from_config(
        {"models": {"primary": "fake", "fake": {"enabled": True, "type": "fake",
                                                "response_prefix": "alpha"}}}
    )
    manager_b = ModelManager.from_config(
        {"models": {"primary": "fake", "fake": {"enabled": True, "type": "fake",
                                                "response_prefix": "beta"}}}
    )
    assert "alpha" in manager_a.complete("hello")
    assert "beta" in manager_b.complete("hello")
    assert manager_a.health_check() == {"fake": True}


def test_manager_disabled_backends_never_instantiate() -> None:
    register_builtin_models()

    class Boom(FakeModelProvider):
        def __init__(self) -> None:  # pragma: no cover - must never run
            raise AssertionError("disabled backend was instantiated")

        @property
        def name(self) -> str:
            return "boom-test-only"

    register("boom-test-only", Boom)
    try:
        # Unknown types, missing keys, and exploding constructors are all fine
        # while disabled: the manager must skip them without a lookup or build.
        manager = ModelManager.from_config(
            {
                "models": {
                    "primary": "fake",
                    "fake": {"enabled": True, "type": "fake"},
                    "claude": {"enabled": False},
                    "ghost": {"enabled": False, "type": "no-such-type"},
                    "boom": {"enabled": False, "type": "boom-test-only"},
                }
            }
        )
    finally:
        unregister("boom-test-only")
    assert manager.backend_names == ["fake"]


def test_manager_rejects_empty_and_bad_primary() -> None:
    with pytest.raises(ConfigError):
        ModelManager.from_config({"models": {"primary": "fake"}})
    with pytest.raises(ConfigError):
        ModelManager.from_config(
            {"models": {"primary": "ghost",
                        "fake": {"enabled": True, "type": "fake"}}}
        )


def test_models_yaml_loads_offline_with_everything_disabled() -> None:
    config = load_config(REPO_ROOT / "configs" / "models.yaml")
    section = config["models"]
    entries = {k: v for k, v in section.items() if k != "primary"}
    assert {"fake", "claude", "muse", "local"} <= set(entries)
    assert all(not entry.get("enabled", False) for entry in entries.values())
    with pytest.raises(ConfigError):
        ModelManager.from_config(config)


def test_models_yaml_enabling_fake_only_works() -> None:
    config = load_config(REPO_ROOT / "configs" / "models.yaml")
    config["models"]["fake"]["enabled"] = True
    manager = ModelManager.from_config(config)
    assert manager.primary == "fake"
    assert "fake completion" in manager.complete("smoke test")


# -- AI plugin stub ----------------------------------------------------------


def test_ai_plugin_stub_runs_offline_with_fake() -> None:
    plugin = AISummaryPlugin(model=FakeModelProvider())
    context = plugin.run(_context())
    for result in context.results:
        assert result.metadata["ai_summary"].startswith("fake completion:")
        assert result.metadata["ai_model"] == "fake"
    # Source results untouched apart from enrichment.
    assert [r.id for r in context.results] == ["1", "2"]


def test_ai_plugin_stub_wires_backend_through_config() -> None:
    plugin = AISummaryPlugin(
        {"model": {"type": "fake", "response_prefix": "cfg"}, "max_chars": 50}
    )
    assert isinstance(plugin.model, FakeModelProvider)
    context = plugin.run(_context())
    assert all(
        r.metadata["ai_summary"].startswith("cfg completion:")
        for r in context.results
    )


def test_ai_plugin_stub_rejects_bad_config() -> None:
    with pytest.raises(ConfigError):
        AISummaryPlugin({"max_chars": "many"})  # type: ignore[dict-item]
    with pytest.raises(ConfigError):
        AISummaryPlugin({"model": "fake"})  # type: ignore[dict-item]


def test_ai_plugin_stub_backend_failure_isolates() -> None:
    claude = ClaudeModelProvider()
    claude.configure({"api_key": "test-key"})
    plugin = AISummaryPlugin(model=claude)
    from openlens.core.interfaces import PluginError

    with pytest.raises(PluginError):
        plugin.run(_context())

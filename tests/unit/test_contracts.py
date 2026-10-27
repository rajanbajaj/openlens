"""Smoke tests for the Wave-0 core contracts.

Proves the interfaces are abstract (cannot instantiate the ABCs directly),
the error taxonomy carries its metadata, the pipeline enum has 10 ordered
stages, and the config loader resolves env vars while failing loudly on
unresolved secrets.
"""

import pytest

from openlens.config import ConfigError, load_config, validate_config
from openlens.core.context import SearchContext
from openlens.core.interfaces import (
    Engine,
    PipelineStage,
    PluginError,
    ProviderError,
    SearchPlugin,
    SearchProvider,
    SearchRequest,
    SearchResponse,
    SearchResult,
)


def test_interfaces_are_abstract():
    for cls in (SearchProvider, SearchPlugin, Engine):
        with pytest.raises(TypeError):
            cls()  # type: ignore[abstract]


def test_pipeline_has_ten_ordered_stages():
    assert len(PipelineStage.ordered()) == 10
    assert PipelineStage.ordered()[0] is PipelineStage.QUERY_PARSING
    assert PipelineStage.ordered()[-1] is PipelineStage.EXPLANATION


def test_provider_error_carries_metadata():
    err = ProviderError.timeout("custom_index")
    assert err.provider_name == "custom_index"
    assert err.retryable is True
    assert err.code == ProviderError.TIMEOUT
    assert not ProviderError.auth("x").retryable


def test_plugin_error_carries_metadata():
    err = PluginError("boom", plugin_name="safe", stage=PipelineStage.FILTERING, mandatory=True)
    assert err.plugin_name == "safe"
    assert err.stage is PipelineStage.FILTERING
    assert err.mandatory is True


def test_search_context_diagnostics_roundtrip():
    ctx = SearchContext(request=SearchRequest(query="q"))
    ctx.record_provider_latency("custom_index", 3.5)
    ctx.record_plugin_execution("safe-filter", PipelineStage.FILTERING, "ok", 0.5)
    diag = ctx.diagnostics()
    assert diag["provider_latencies_ms"] == {"custom_index": 3.5}
    assert diag["plugin_executions"][0]["plugin"] == "safe-filter"
    assert diag["errors"] == []


def test_result_keeps_original_and_final_scores():
    res = SearchResult(id="1", title="t", snippet="s", original_score=0.8, final_score=0.9)
    assert res.original_score != res.final_score
    assert res.reasons == [] and res.warnings == []


def test_response_defaults():
    resp = SearchResponse(query="q", mode="default")
    assert resp.results == [] and resp.total == 0
    assert resp.applied_filters == [] and resp.diagnostics == {}


def test_load_config_resolves_env_and_defaults(tmp_path, monkeypatch):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("a: ${OPENLENS_TEST_KEY}\nb: ${OPENLENS_MISSING:-fallback}\n")
    monkeypatch.setenv("OPENLENS_TEST_KEY", "secret")
    loaded = load_config(cfg)
    assert loaded == {"a": "secret", "b": "fallback"}


def test_load_config_unresolved_secret_fails_loudly(tmp_path, monkeypatch):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("key: ${OPENLENS_DEFINITELY_UNSET_VAR}\n")
    monkeypatch.delenv("OPENLENS_DEFINITELY_UNSET_VAR", raising=False)
    with pytest.raises(ConfigError):
        load_config(cfg)


def test_load_config_missing_file_raises_config_error(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.yaml")


def test_validate_config_requires_sections():
    with pytest.raises(ConfigError):
        validate_config({}, ["providers"])
    validate_config({"providers": {}}, ["providers"])  # no raise

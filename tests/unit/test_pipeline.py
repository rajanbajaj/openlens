"""Smoke tests for the plugin pipeline + ranking (plan steps 5-6).

Covers the Done gate: filter excludes, scorer reorders, mandatory-filter
outage fails closed, optional-plugin failure isolated. Plus registry
validation, per-mode profiles, and explanation assembly.
"""

from typing import Any

import pytest

from openlens.core.context import SearchContext
from openlens.core.engine import SearchEngine
from openlens.core.interfaces import (
    ConfigError,
    PipelineStage,
    PluginError,
    PluginType,
    SearchProvider,
    SearchRequest,
    SearchResult,
)
from openlens.core.pipeline import Pipeline, assemble_explanations
from openlens.core.registry import PluginRegistry
from openlens.plugins.base import BasePlugin
from openlens.plugins.relevance_scorer import BaselineRelevanceScorer
from openlens.plugins.safety_filter import SafetyFilter
from openlens.plugins.trust_enricher import TrustEnricher
from openlens.ranking.profiles import get_profile, list_profiles, load_profiles
from openlens.ranking.ranker import Ranker


def _hit(
    id: str,
    title: str,
    snippet: str = "",
    original: float = 1.0,
    url: str | None = None,
) -> SearchResult:
    return SearchResult(
        id=id,
        title=title,
        snippet=snippet,
        original_score=original,
        final_score=original,
        url=url,
    )


class FakeProvider(SearchProvider):
    """Offline provider returning canned hits."""

    def __init__(self, hits: list[SearchResult]) -> None:
        self._hits = hits

    @property
    def name(self) -> str:
        return "fake"

    def configure(self, config: dict[str, Any]) -> None:
        pass

    def search(self, request: SearchRequest) -> list[SearchResult]:
        return list(self._hits)

    def health_check(self) -> bool:
        return True


class BoomOptional(BasePlugin):
    """Optional plugin that always fails (isolation probe)."""

    @property
    def name(self) -> str:
        return "boom-optional"

    @property
    def plugin_type(self) -> PluginType:
        return PluginType.ENRICHER

    @property
    def stage(self) -> PipelineStage:
        return PipelineStage.ENRICHMENT

    def run(self, context: SearchContext) -> SearchContext:
        raise PluginError("boom", plugin_name=self.name, stage=self.stage)


def _engine(hits: list[SearchResult], plugins: list[Any]) -> SearchEngine:
    return SearchEngine(providers=[FakeProvider(hits)], plugins=plugins)


# --- Done gate -------------------------------------------------------------


def test_safety_filter_excludes_blocked():
    engine = _engine(
        [
            _hit("ok", "Cute kittens playing"),
            _hit("bad", "Explicit adult content here"),
        ],
        [SafetyFilter()],
    )
    resp = engine.search(SearchRequest(query="kittens"))
    assert [r.id for r in resp.results] == ["ok"]
    assert resp.applied_filters == ["safety-filter"]


def test_baseline_scorer_reorders_by_term_overlap():
    engine = _engine(
        [
            _hit("generic", "Unrelated news roundup", original=2.0),
            _hit("match", "Kitten care guide for new kittens", original=1.0),
        ],
        [BaselineRelevanceScorer(), Ranker()],
    )
    resp = engine.search(SearchRequest(query="kittens"))
    assert [r.id for r in resp.results] == ["match", "generic"]
    by_id = {r.id: r for r in resp.results}
    assert by_id["match"].original_score == 1.0  # provider signal preserved
    assert by_id["match"].final_score > by_id["generic"].final_score
    assert by_id["match"].reasons  # explanation assembled


def test_mandatory_filter_outage_fails_closed():
    engine = _engine(
        [_hit("ok", "Cute kittens")],
        [SafetyFilter({"simulate_outage": True})],
    )
    with pytest.raises(PluginError):
        engine.search(SearchRequest(query="kittens"))


def test_optional_plugin_failure_isolated():
    engine = _engine(
        [_hit("ok", "Cute kittens", url="https://example.com/k")],
        [BoomOptional(), Ranker()],
    )
    resp = engine.search(SearchRequest(query="kittens"))
    # Results still served despite the optional outage.
    assert [r.id for r in resp.results] == ["ok"]
    statuses = {
        e["plugin"]: e["status"] for e in resp.diagnostics["plugin_executions"]
    }
    assert statuses["boom-optional"] == "failed"
    assert statuses["ranker"] == "ok"
    assert resp.diagnostics["errors"] == ["boom-optional: boom"]


# --- Registry / ordering validation ----------------------------------------


def test_registry_builds_from_config_and_rejects_unknown():
    registry = PluginRegistry()
    plugins = registry.build(
        [
            {"name": "safety-filter", "config": {"blocked_terms": ["spam"]}},
            {"name": "off", "class": "openlens.plugins.safety_filter:SafetyFilter",
             "enabled": False},
        ]
    )
    assert [p.name for p in plugins] == ["safety-filter"]
    with pytest.raises(ConfigError):
        registry.build([{"name": "nope"}])
    with pytest.raises(ConfigError):
        registry.build([{"no-name": True}])


def test_pipeline_rejects_missing_dependency_and_cycle():
    scorer = BaselineRelevanceScorer()

    class Needy(BasePlugin):
        @property
        def name(self) -> str:
            return "needy"

        @property
        def plugin_type(self) -> PluginType:
            return PluginType.SCORER

        @property
        def stage(self) -> PipelineStage:
            return PipelineStage.SCORING

        @property
        def dependencies(self) -> tuple[str, ...]:
            return ("ghost",)

        def run(self, context: SearchContext) -> SearchContext:
            return context

    with pytest.raises(ConfigError):
        Pipeline([scorer, Needy()])

    class Late(BasePlugin):
        @property
        def name(self) -> str:
            return "late"

        @property
        def plugin_type(self) -> PluginType:
            return PluginType.FILTER

        @property
        def stage(self) -> PipelineStage:
            return PipelineStage.FILTERING

        @property
        def dependencies(self) -> tuple[str, ...]:
            return ("baseline-scorer",)  # scorer runs in a later stage

        def run(self, context: SearchContext) -> SearchContext:
            return context

    with pytest.raises(ConfigError):
        Pipeline([scorer, Late()])


# --- Ranking profiles ------------------------------------------------------


def test_ranking_yaml_loads_five_profiles():
    profiles = load_profiles("configs/ranking.yaml")  # repo-relative
    assert set(profiles) == {"default", "child-safe", "news", "shopping", "travel"}
    assert set(list_profiles()) == set(profiles)
    with pytest.raises(ConfigError):
        get_profile("nope")


def test_child_safe_profile_demotes_warned_results():
    hits = [
        _hit("shady", "Kittens guide", original=5.0, url="https://shady.example/x"),
        _hit("trusted", "Kittens guide", original=1.0, url="https://safe.example/y"),
    ]
    enricher = TrustEnricher({"trusted_domains": ["safe.example"]})
    default_engine = _engine(hits, [enricher, Ranker()])
    safe_engine = _engine(hits, [enricher, Ranker()])

    default_resp = default_engine.search(SearchRequest(query="kittens", mode="default"))
    safe_resp = safe_engine.search(SearchRequest(query="kittens", mode="child-safe"))

    by_id = {r.id: r for r in default_resp.results}
    assert by_id["shady"].warnings  # enricher annotated the untrusted hit
    assert not by_id["trusted"].warnings
    # Under child-safe the warned hit must rank last regardless of provider score.
    assert safe_resp.results[-1].id == "shady"


def test_explanation_assembly_keeps_original_and_reasons():
    ctx = SearchContext(request=SearchRequest(query="q"))
    ctx.results = [_hit("a", "Alpha", original=0.7)]
    assemble_explanations(ctx)
    assert ctx.results[0].original_score == 0.7
    assert ctx.results[0].reasons

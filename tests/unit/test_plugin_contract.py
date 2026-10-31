"""Plugin-conformance suite: every search plugin must pass this.

Covers stage declaration, mandatory fail-closed behavior, optional-plugin
isolation, and registry validation. Failures name the owning plugin or
core module (see message prefixes).
"""

from __future__ import annotations

from typing import Any

import pytest

from openlens.core.context import SearchContext
from openlens.core.interfaces import (
    ConfigError,
    PipelineStage,
    PluginError,
    PluginType,
    SearchPlugin,
    SearchRequest,
    SearchResult,
)
from openlens.core.pipeline import Pipeline, assemble_explanations, validate_and_order
from openlens.core.registry import PluginRegistry
from openlens.plugins.relevance_scorer import BaselineRelevanceScorer
from openlens.plugins.safety_filter import SafetyFilter
from openlens.plugins.trust_enricher import TrustEnricher


def _hit(
    hit_id: str = "h1", title: str = "clean title", url: str = "https://example.com/a"
) -> SearchResult:
    return SearchResult(
        id=hit_id,
        title=title,
        snippet="a snippet",
        original_score=1.0,
        final_score=1.0,
        url=url,
        provider="test",
    )


def _context(n: int = 2) -> SearchContext:
    ctx = SearchContext(request=SearchRequest(query="q"))
    ctx.results = [_hit(f"h{i}") for i in range(n)]
    return ctx


def _builtin_plugins() -> list[SearchPlugin]:
    return [SafetyFilter(), BaselineRelevanceScorer(), TrustEnricher()]


# ---------------------------------------------------------------------------
# Stage declaration: every builtin plugin declares a valid stage + type
# ---------------------------------------------------------------------------


def test_conformance_all_plugins_declare_valid_stage_and_type():
    for plugin in _builtin_plugins():
        assert plugin.name, f"[{type(plugin).__name__}] plugin must have a name"
        assert isinstance(plugin.stage, PipelineStage), (
            f"[{plugin.name}] stage must be a PipelineStage, "
            f"got {plugin.stage!r} (core/interfaces.py contract)"
        )
        assert isinstance(plugin.plugin_type, PluginType), (
            f"[{plugin.name}] plugin_type must be a PluginType, "
            f"got {plugin.plugin_type!r}"
        )


def test_conformance_stage_order_matches_pipeline():
    stages = [p.stage for p in Pipeline(_builtin_plugins()).plugins]
    order = PipelineStage.ordered()
    assert [order.index(s) for s in stages] == sorted(order.index(s) for s in stages), (
        "[core/pipeline.py] plugins must execute in PipelineStage order"
    )


def test_conformance_invalid_stage_rejected_at_startup():
    class BadStagePlugin(BaselineRelevanceScorer):
        @property
        def stage(self):  # type: ignore[override]
            return "not-a-stage"

    with pytest.raises(ConfigError):
        validate_and_order([BadStagePlugin()])


def test_conformance_bad_config_rejected():
    with pytest.raises(ConfigError):
        SafetyFilter().configure("not-a-mapping")  # type: ignore[arg-type]
    with pytest.raises(ConfigError):
        TrustEnricher({"trusted_domains": "example.com"})  # type: ignore[dict-item]


# ---------------------------------------------------------------------------
# Mandatory fail-closed (plugins/safety_filter.py + core/pipeline.py)
# ---------------------------------------------------------------------------


def test_conformance_mandatory_filter_excludes():
    plugin = SafetyFilter()
    ctx = _context()
    ctx.results.append(_hit("bad", title="free malware download"))
    out = plugin.run(ctx)
    assert [r.id for r in out.results] == ["h0", "h1"], (
        "[plugins/safety_filter.py] mandatory filter must exclude blocked hits"
    )
    assert "safety-filter" in out.applied_filters


def test_conformance_mandatory_outage_fails_closed():
    plugin = SafetyFilter({"simulate_outage": True})
    ctx = _context()
    before = list(ctx.results)
    with pytest.raises(PluginError) as excinfo:
        Pipeline([plugin]).run(ctx)
    assert excinfo.value.mandatory, (
        "[plugins/safety_filter.py] outage must raise PluginError(mandatory=True)"
    )
    # Fail-closed: the exception propagates, so the caller can never mistake
    # the unfiltered `before` list for a filtered result.
    assert len(before) == 2


def test_conformance_mandatory_raw_exception_converted():
    class RawBoomFilter(SafetyFilter):
        def run(self, context: SearchContext) -> SearchContext:
            raise RuntimeError("backend exploded")

    with pytest.raises(PluginError):
        Pipeline([RawBoomFilter()]).run(_context())


# ---------------------------------------------------------------------------
# Optional isolation (plugins/trust_enricher.py + core/pipeline.py)
# ---------------------------------------------------------------------------


def test_conformance_optional_failure_isolated():
    plugin = TrustEnricher({"simulate_failure": True})
    ctx = _context()
    out = Pipeline([plugin]).run(ctx)  # must NOT raise
    assert len(out.results) == 2, (
        "[core/pipeline.py] optional failure must preserve all results"
    )
    failures = [e for e in out.plugin_executions if e.status == "failed"]
    assert failures and failures[0].plugin_name == "trust-enricher", (
        "[core/pipeline.py] optional failure must be recorded in diagnostics"
    )
    assert out.errors, "[core/pipeline.py] optional failure must add an error entry"


def test_conformance_optional_raw_exception_isolated():
    class RawBoomEnricher(TrustEnricher):
        def run(self, context: SearchContext) -> SearchContext:
            raise RuntimeError("trust backend exploded")

    out = Pipeline([RawBoomEnricher()]).run(_context())
    assert len(out.results) == 2


def test_conformance_optional_enricher_never_filters():
    plugin = TrustEnricher()
    ctx = _context()
    out = plugin.run(ctx)
    assert len(out.results) == 2, (
        "[plugins/trust_enricher.py] enricher must annotate, never remove"
    )
    assert any(r.warnings for r in out.results), (
        "[plugins/trust_enricher.py] untrusted domains must gain warnings"
    )


def test_conformance_pipeline_continues_after_optional_failure():
    order: list[str] = []

    class Recorder(BaselineRelevanceScorer):
        @property
        def name(self) -> str:
            return "recorder"

        def run(self, context: SearchContext) -> SearchContext:
            order.append("recorder")
            return context

    failing = TrustEnricher({"simulate_failure": True})
    out = Pipeline([failing, Recorder()]).run(_context())
    assert order == ["recorder"], (
        "[core/pipeline.py] later plugins must still run after an optional failure"
    )
    assert len(out.results) == 2


# ---------------------------------------------------------------------------
# Registry validation (core/registry.py)
# ---------------------------------------------------------------------------


def test_conformance_registry_builds_and_skips_disabled():
    built = PluginRegistry().build(
        [
            {"name": "safety-filter", "enabled": True, "config": {}},
            {"name": "trust-enricher", "enabled": False},
        ]
    )
    assert [p.name for p in built] == ["safety-filter"], (
        "[core/registry.py] disabled plugins must be skipped without construction"
    )


def test_conformance_registry_rejects_unknown_and_bad_entries():
    with pytest.raises(ConfigError):
        PluginRegistry().build([{"name": "no-such-plugin"}])
    with pytest.raises(ConfigError):
        PluginRegistry().build([{"class": "x:Y"}])  # missing name
    with pytest.raises(ConfigError):
        PluginRegistry().build([{"name": "safety-filter", "config": "not-a-mapping"}])


def test_conformance_duplicate_plugin_names_rejected():
    with pytest.raises(ConfigError):
        validate_and_order([SafetyFilter(), SafetyFilter()])


def test_conformance_unknown_dependency_rejected():
    class Lonely(BaselineRelevanceScorer):
        @property
        def name(self) -> str:
            return "lonely"

        @property
        def dependencies(self) -> tuple[str, ...]:
            return ("ghost",)

    with pytest.raises(ConfigError):
        validate_and_order([Lonely()])


def test_conformance_explanation_stage_guarantees_reasons():
    # Even when a provider emits empty reasons, the EXPLANATION stage
    # must fill them so API responses always explain.
    ctx = _context()
    for hit in ctx.results:
        hit.reasons = []
    assemble_explanations(ctx)
    for hit in ctx.results:
        assert hit.reasons, (
            "[core/pipeline.py] EXPLANATION stage must guarantee per-result reasons"
        )
        assert hit.original_score == 1.0 and hit.final_score == 1.0, (
            "[core/pipeline.py] EXPLANATION must never touch scores"
        )


def test_conformance_plugin_type_matches_stage():
    expectations: dict[str, Any] = {
        "safety-filter": (PluginType.FILTER, PipelineStage.FILTERING),
        "trust-enricher": (PluginType.ENRICHER, PipelineStage.ENRICHMENT),
    }
    instances = {"safety-filter": SafetyFilter(), "trust-enricher": TrustEnricher()}
    for name, (ptype, stage) in expectations.items():
        plugin = instances[name]
        assert plugin.plugin_type is ptype, (
            f"[plugins] {name} must be {ptype}, got {plugin.plugin_type}"
        )
        assert plugin.stage is stage, (
            f"[plugins] {name} must run at {stage}, got {plugin.stage}"
        )


# ---------------------------------------------------------------------------
# safety-filter Step 2: host parsing, URL patterns, child-safe allowlist
# (plugins/safety_filter.py)
# ---------------------------------------------------------------------------


def _filtered_ids(
    plugin: SafetyFilter, hits: list[SearchResult], mode: str = "default"
) -> list[str]:
    ctx = SearchContext(request=SearchRequest(query="q", mode=mode))
    ctx.results = hits
    return [r.id for r in plugin.run(ctx).results]


def test_safety_blocked_domain_matches_bare_host():
    plugin = SafetyFilter({"blocked_domains": ["example.com"]})
    hits = [
        _hit("exact", url="https://example.com/a"),
        _hit("upper", url="https://EXAMPLE.COM/a"),
        _hit("other", url="https://other.test/a"),
    ]
    assert _filtered_ids(plugin, hits) == ["other"]


def test_safety_evil_prefix_domain_not_blocked():
    # Regression: substring matching blocked evil-example.com on example.com.
    plugin = SafetyFilter({"blocked_domains": ["example.com"]})
    hits = [_hit("evil", url="https://evil-example.com/a")]
    assert _filtered_ids(plugin, hits) == ["evil"]


def test_safety_suffix_domain_not_blocked():
    # Regression: example.com.evil.test is not example.com.
    plugin = SafetyFilter({"blocked_domains": ["example.com"]})
    hits = [_hit("suffix", url="https://example.com.evil.test/a")]
    assert _filtered_ids(plugin, hits) == ["suffix"]


def test_safety_subdomain_not_blocked_by_plain_entry():
    plugin = SafetyFilter({"blocked_domains": ["example.com"]})
    hits = [_hit("sub", url="https://sub.example.com/a")]
    assert _filtered_ids(plugin, hits) == ["sub"]


def test_safety_host_parsing_strips_port_and_userinfo():
    plugin = SafetyFilter({"blocked_domains": ["example.com"]})
    hits = [
        _hit("port", url="https://example.com:8443/a"),
        _hit("userinfo", url="https://user:secret@example.com/a"),
        _hit("both", url="https://user@example.com:8443/a"),
        # Host is evil.test; example.com is only the userinfo.
        _hit("trick", url="https://example.com@evil.test/a"),
    ]
    assert _filtered_ids(plugin, hits) == ["trick"]


def test_safety_wildcard_domain_matches_subdomains():
    plugin = SafetyFilter({"blocked_domains": ["*.example.com"]})
    hits = [
        _hit("sub", url="https://sub.example.com/a"),
        _hit("other", url="https://other.test/a"),
    ]
    assert _filtered_ids(plugin, hits) == ["other"]


def test_safety_blocked_url_glob_pattern():
    plugin = SafetyFilter({"blocked_url_globs": ["https://ads.example.com/*"]})
    hits = [
        _hit("ad", url="https://ads.example.com/banner"),
        _hit("page", url="https://example.com/article"),
    ]
    assert _filtered_ids(plugin, hits) == ["page"]


def test_safety_blocked_url_regex_pattern():
    plugin = SafetyFilter({"blocked_url_regexes": [r"/track(ing)?/"]})
    hits = [
        _hit("track", url="https://example.com/tracking/pixel"),
        _hit("page", url="https://example.com/article"),
    ]
    assert _filtered_ids(plugin, hits) == ["page"]


def test_safety_blocked_url_patterns_typed_entries():
    plugin = SafetyFilter(
        {
            "blocked_url_patterns": [
                "glob:https://ads.example.com/*",
                {"pattern": r"/popups?/", "kind": "regex"},
            ]
        }
    )
    hits = [
        _hit("ad", url="https://ads.example.com/banner"),
        _hit("popup", url="https://example.com/popup/"),
        _hit("page", url="https://example.com/article"),
    ]
    assert _filtered_ids(plugin, hits) == ["page"]


def test_safety_url_pattern_caps_and_invalid_regex_rejected():
    with pytest.raises(ConfigError):
        SafetyFilter({"blocked_url_regexes": [r"[unclosed"]})
    with pytest.raises(ConfigError):
        SafetyFilter({"blocked_url_globs": ["*x*"] * 101})
    with pytest.raises(ConfigError):
        SafetyFilter({"blocked_url_regexes": ["a" * 513]})
    with pytest.raises(ConfigError):
        SafetyFilter({"blocked_url_globs": "not-a-list"})  # type: ignore[dict-item]


def test_safety_allowlist_child_safe_unlisted_excluded_listed_passes():
    plugin = SafetyFilter({"allowed_domains": ["kids.example"]})
    hits = [
        _hit("listed", url="https://kids.example/story"),
        _hit("unlisted", url="https://other.test/story"),
    ]
    assert _filtered_ids(plugin, hits, mode="child-safe") == ["listed"]


def test_safety_allowlist_inert_outside_child_safe():
    plugin = SafetyFilter({"allowed_domains": ["kids.example"]})
    hits = [_hit("unlisted", url="https://other.test/story")]
    for mode in ("default", "news", "shopping", "travel"):
        assert _filtered_ids(plugin, list(hits), mode=mode) == ["unlisted"]


def test_safety_child_safe_without_allowlist_denies_all():
    plugin = SafetyFilter()
    hits = [_hit("any", url="https://example.com/a")]
    assert _filtered_ids(plugin, hits, mode="child-safe") == []


def test_safety_child_safe_blocklist_still_wins_over_allowlist():
    plugin = SafetyFilter(
        {"allowed_domains": ["kids.example"], "blocked_domains": ["kids.example"]}
    )
    hits = [_hit("listed-but-blocked", url="https://kids.example/story")]
    assert _filtered_ids(plugin, hits, mode="child-safe") == []


def test_safety_child_safe_allow_via_url_pattern():
    plugin = SafetyFilter({"allowed_url_globs": ["https://*.kids.example/*"]})
    hits = [
        _hit("pattern", url="https://stories.kids.example/a"),
        _hit("unlisted", url="https://other.test/a"),
    ]
    assert _filtered_ids(plugin, hits, mode="child-safe") == ["pattern"]


def _kids_hit(hit_id: str, url: str = "https://stories.example/a") -> SearchResult:
    return SearchResult(
        id=hit_id,
        title="a kids story",
        snippet="a snippet",
        original_score=1.0,
        final_score=1.0,
        url=url,
        provider="test",
        metadata={"site_category": "kids"},
    )


def test_safety_child_safe_kids_category_passes_without_allowlist():
    plugin = SafetyFilter()
    hits = [
        _kids_hit("kids"),
        _hit("plain", url="https://other.test/a"),
    ]
    assert _filtered_ids(plugin, hits, mode="child-safe") == ["kids"]


def test_safety_child_safe_kids_domain_hit_passes():
    plugin = SafetyFilter()
    hit = _kids_hit("hit")
    hit.metadata = {"kids_domain_hit": 1}
    assert _filtered_ids(plugin, [hit], mode="child-safe") == ["hit"]


def test_safety_child_safe_blocklist_still_wins_over_kids_category():
    plugin = SafetyFilter({"blocked_domains": ["stories.example"]})
    hits = [_kids_hit("blocked", url="https://stories.example/a")]
    assert _filtered_ids(plugin, hits, mode="child-safe") == []

"""Filter-pack integration: the three mandatory FILTERING filters wired end to end.

Covers filter-pack plan Step 4: ``user-restrictions`` ->
``safety-filter`` (blocklist/allowlist) -> ``privacy-filter`` execution
order enforced by plugin dependencies (asserted via
``plugin_executions``, not declaration order), a fixture hit-list
reduced to exactly the compliant subset with per-filter attribution in
``applied_filters`` + removed counts in ``context.metadata``, and
fail-closed behavior when any one filter's outage is simulated.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from openlens.config import load_config
from openlens.core.context import SearchContext
from openlens.core.engine import SearchEngine
from openlens.core.interfaces import (
    PluginError,
    SearchProvider,
    SearchRequest,
    SearchResult,
)
from openlens.core.pipeline import Pipeline
from openlens.core.registry import FILTER_PACK_ORDER, PluginRegistry

# ---------------------------------------------------------------------------
# Wave-2F conformance handoff note (filter-pack plan Step 4).
#
# Wave 2F owns the conformance suites; this task ships ordinary unit +
# integration tests only and does NOT create that suite. When 2F builds
# the filter conformance suite, it should assert, for every mandatory
# FILTER plugin (starting with the three wired here):
#
# - Stage: each filter declares FILTER / FILTERING / mandatory=True, and
#   the pack executes in the mandated order user-restrictions ->
#   safety-filter -> privacy-filter (dependencies declared at build time
#   in core/registry.py; Pipeline topological sort enforces it).
# - Isolation: a mandatory filter outage (simulate_outage) fails the
#   whole request closed with PluginError(mandatory=True); no partial
#   or unfiltered result list ever escapes to the caller.
# - Explanation: every removal is attributed — the filter's name appears
#   in applied_filters and its removed count in context.metadata under
#   "<filter>-removed". Counts are in-process evidence only today: API
#   surfacing needs the deferred diagnostics extension
#   (context.metadata -> SearchContext.diagnostics() ->
#   DiagnosticsSchema), which no plan owns yet.
# ---------------------------------------------------------------------------

CONFIG_PATH = Path(__file__).resolve().parents[2] / "configs" / "default.yaml"

EXPECTED_ORDER = ["user-restrictions", "safety-filter", "privacy-filter"]

_PRIVACY_PATTERNS: dict[str, list[str]] = {
    "email": [r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"],
    "phone": [r"\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b"],
    "government_id": [r"\b\d{3}-\d{2}-\d{4}\b"],
    "street_address": [r"\b\d+\s+Main\s+Street\b"],
}

_SAFETY_CONFIG: dict[str, Any] = {
    "blocked_domains": ["blocked.test"],
    "blocked_url_globs": ["*banned-path*"],
}

_USER_FILTERS: dict[str, Any] = {
    "must_not": ["userbanned"],
    "exclude_domains": ["userblocked.test"],
}


def _hit(
    hit_id: str,
    snippet: str = "a clean snippet",
    url: str = "https://clean.test/page",
    title: str = "clean title",
) -> SearchResult:
    return SearchResult(
        id=hit_id,
        title=title,
        snippet=snippet,
        original_score=1.0,
        final_score=1.0,
        url=url,
        provider="test",
    )


def _fixture_hits() -> list[SearchResult]:
    """One violator per rule, plus two compliant hits (10 total).

    Attribution by construction: user violators carry no safety/privacy
    triggers, safety hits carry no PII, and PII hits sit on clean
    domains with clean wording — so each filter's removed count is
    attributable to exactly its own rules.
    """
    return [
        _hit(
            "keep-1",
            snippet="A clean guide to alpine trails",
            url="https://clean.test/trails",
            title="Alpine trail guide",
        ),
        _hit(
            "user-term", snippet="userbanned offer here", url="https://clean.test/offer"
        ),
        _hit("blocked-domain", url="https://blocked.test/page"),
        _hit(
            "pii-email",
            snippet="Contact john.doe@example.com for details",
            url="https://clean.test/email",
        ),
        _hit(
            "keep-2",
            snippet="Opening hours and ticket prices",
            url="https://clean.test/museum",
            title="City museum hours",
        ),
        _hit("user-domain", url="https://userblocked.test/page"),
        _hit("blocked-pattern", url="https://clean.test/banned-path/page"),
        _hit(
            "pii-phone",
            snippet="Call 555-123-4567 for details",
            url="https://clean.test/phone",
        ),
        _hit(
            "pii-government-id",
            snippet="SSN 123-45-6789 on file",
            url="https://clean.test/id",
        ),
        _hit(
            "pii-street",
            snippet="Meet at 123 Main Street today",
            url="https://clean.test/meet",
        ),
    ]


def _entries(outage: str | None = None) -> list[dict[str, Any]]:
    """Registry entries in scrambled (reverse-mandated) declaration order.

    Declaration order must NOT be what makes the pack run in the
    mandated order — the dependency chain does.
    """
    safety = dict(_SAFETY_CONFIG)
    privacy: dict[str, Any] = {"patterns": dict(_PRIVACY_PATTERNS)}
    user: dict[str, Any] = {}
    configs = {
        "user-restrictions": user,
        "safety-filter": safety,
        "privacy-filter": privacy,
    }
    if outage is not None:
        configs[outage]["simulate_outage"] = True
    return [
        {"name": "privacy-filter", "enabled": True, "config": privacy},
        {"name": "safety-filter", "enabled": True, "config": safety},
        {"name": "user-restrictions", "enabled": True, "config": user},
    ]


def _build(outage: str | None = None) -> list[Any]:
    return PluginRegistry().build(_entries(outage=outage))


def _context(hits: list[SearchResult], **request_kwargs: Any) -> SearchContext:
    kwargs: dict[str, Any] = {"query": "trails", "filters": dict(_USER_FILTERS)}
    kwargs.update(request_kwargs)
    context = SearchContext(request=SearchRequest(**kwargs))
    context.results = list(hits)
    return context


def _execution_names(context: SearchContext) -> list[str]:
    return [execution.plugin_name for execution in context.plugin_executions]


class _FakeProvider(SearchProvider):
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


# ---------------------------------------------------------------------------
# Ordering: dependencies + topological sort, proven via plugin_executions
# ---------------------------------------------------------------------------


def test_filter_pack_order_enforced_by_dependencies():
    assert list(FILTER_PACK_ORDER) == EXPECTED_ORDER
    plugins = _build()
    by_name = {plugin.name: plugin for plugin in plugins}
    assert by_name["user-restrictions"].dependencies == ()
    assert by_name["safety-filter"].dependencies == ("user-restrictions",)
    assert by_name["privacy-filter"].dependencies == ("safety-filter",)

    pipeline = Pipeline(plugins)
    assert [plugin.name for plugin in pipeline.plugins] == EXPECTED_ORDER

    context = _context(_fixture_hits())
    pipeline.run(context)
    assert _execution_names(context) == EXPECTED_ORDER, (
        "[core/pipeline.py] scrambled declaration order must still execute "
        "user-restrictions -> safety-filter -> privacy-filter"
    )
    assert context.applied_filters == EXPECTED_ORDER


def test_single_filter_build_stays_standalone():
    # Dependencies are stamped only among co-built pack members, so a
    # lone safety-filter pipeline still validates (no unknown dep).
    plugins = PluginRegistry().build(
        [{"name": "safety-filter", "enabled": True, "config": {}}]
    )
    assert plugins[0].dependencies == ()
    context = _context([_hit("ok")], filters={})
    Pipeline(plugins).run(context)
    assert [result.id for result in context.results] == ["ok"]


# ---------------------------------------------------------------------------
# End-to-end reduction: exactly the compliant subset, attributed removals
# ---------------------------------------------------------------------------


def test_filter_pack_reduces_fixture_to_compliant_subset():
    context = _context(_fixture_hits())
    Pipeline(_build()).run(context)

    assert [result.id for result in context.results] == ["keep-1", "keep-2"], (
        "[integration] fixture must reduce to exactly the compliant subset"
    )
    # Attribution: every filter ran, in order, and each removed count
    # matches the violators constructed for that filter alone.
    assert context.applied_filters == EXPECTED_ORDER
    assert context.metadata["user-restrictions-removed"] == 2
    assert context.metadata["safety-filter-removed"] == 2
    assert context.metadata["privacy-filter-removed"] == 4
    assert _execution_names(context) == EXPECTED_ORDER
    assert all(execution.status == "ok" for execution in context.plugin_executions)


def test_filter_pack_child_safe_allowlist_end_to_end():
    entries = _entries()
    for entry in entries:
        if entry["name"] == "safety-filter":
            entry["config"] = {"allowed_domains": ["clean.test"]}
    plugins = PluginRegistry().build(entries)
    hits = [
        _hit("listed", url="https://clean.test/page"),
        _hit("unlisted", url="https://other.test/page"),
    ]
    context = _context(hits, filters={}, mode="child-safe")
    Pipeline(plugins).run(context)
    assert [result.id for result in context.results] == ["listed"], (
        "[plugins/safety_filter.py] child-safe must default-deny unlisted hosts"
    )
    assert context.metadata["safety-filter-removed"] == 1


# ---------------------------------------------------------------------------
# Fail closed: any one filter's outage fails the request, nothing escapes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "outage", ["user-restrictions", "safety-filter", "privacy-filter"]
)
def test_any_filter_outage_fails_request_closed(outage: str):
    engine = SearchEngine(
        providers=[_FakeProvider(_fixture_hits())], plugins=_build(outage=outage)
    )
    with pytest.raises(PluginError) as excinfo:
        engine.search(SearchRequest(query="trails", filters=dict(_USER_FILTERS)))
    assert excinfo.value.mandatory is True
    assert excinfo.value.plugin_name == outage, (
        "[integration] the simulated outage must be the failure that "
        "propagates; no partial result list is returned"
    )


# ---------------------------------------------------------------------------
# Default config wiring: configs/default.yaml builds the ordered pack
# ---------------------------------------------------------------------------


def test_default_yaml_wires_ordered_filter_pack():
    config = load_config(CONFIG_PATH)
    entries = config["plugins"]
    assert isinstance(entries, list)
    plugins = PluginRegistry().build(entries)
    names = [plugin.name for plugin in plugins]
    for filter_name in EXPECTED_ORDER:
        assert filter_name in names, (
            f"[configs/default.yaml] filter pack entry '{filter_name}' missing"
        )

    hits = [
        _hit("keep", url="https://clean.test/page"),
        _hit("unsafe", title="free malware download", url="https://clean.test/bad"),
        _hit(
            "pii",
            snippet="Contact john.doe@example.com for details",
            url="https://clean.test/mail",
        ),
    ]
    context = _context(hits, filters={})
    Pipeline(plugins).run(context)
    filtering_executions = [
        execution.plugin_name
        for execution in context.plugin_executions
        if execution.stage == "filtering"
    ]
    assert filtering_executions == EXPECTED_ORDER
    assert [result.id for result in context.results] == ["keep"]
    assert context.metadata["user-restrictions-removed"] == 0
    assert context.metadata["safety-filter-removed"] == 1
    assert context.metadata["privacy-filter-removed"] == 1

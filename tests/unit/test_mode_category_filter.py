"""Mode-category filter tests: modes return categorical results.

Covers ``plugins/mode_category_filter.py``: news/shopping modes keep only
hits with the matching indexed ``site_category``; travel matches the
ranking profile's keywords (no travel category exists); default/agent
modes are no-ops; an empty filter falls back to the original results with
a metadata flag instead of serving a blank page.
"""

from __future__ import annotations

from openlens.core.context import SearchContext
from openlens.core.interfaces import (
    PipelineStage,
    PluginType,
    SearchRequest,
    SearchResult,
)
from openlens.core.registry import PluginRegistry
from openlens.plugins.mode_category_filter import ModeCategoryFilter


def _hit(
    hit_id: str,
    category: str | None = None,
    title: str = "title",
    snippet: str = "snippet",
    url: str = "https://example.com/a",
) -> SearchResult:
    metadata = {"site_category": category} if category is not None else {}
    return SearchResult(
        id=hit_id,
        title=title,
        snippet=snippet,
        original_score=1.0,
        final_score=1.0,
        url=url,
        provider="test",
        metadata=metadata,
    )


def _context(results: list[SearchResult], mode: str) -> SearchContext:
    ctx = SearchContext(request=SearchRequest(query="python", mode=mode))
    ctx.results = list(results)
    return ctx


def test_declaration() -> None:
    plugin = ModeCategoryFilter()
    assert plugin.name == "mode-category-filter"
    assert plugin.plugin_type is PluginType.FILTER
    assert plugin.stage is PipelineStage.FILTERING
    assert plugin.mandatory is False


def test_registry_resolves_short_name() -> None:
    built = PluginRegistry().build([{"name": "mode-category-filter"}])
    assert [p.name for p in built] == ["mode-category-filter"]


def test_news_mode_keeps_only_news() -> None:
    plugin = ModeCategoryFilter()
    out = plugin.run(
        _context([_hit("n", "news"), _hit("s", "shopping"), _hit("o", "other")], "news")
    )
    assert [r.id for r in out.results] == ["n"]
    assert out.metadata["mode-category-filter-removed"] == 2
    assert "mode-category-filter" in out.applied_filters
    assert "mode-category-filter-fallback" not in out.metadata


def test_shopping_mode_keeps_only_shopping() -> None:
    plugin = ModeCategoryFilter()
    out = plugin.run(_context([_hit("n", "news"), _hit("s", "shopping")], "shopping"))
    assert [r.id for r in out.results] == ["s"]


def test_travel_mode_matches_keywords() -> None:
    plugin = ModeCategoryFilter()
    out = plugin.run(
        _context(
            [
                _hit("t", "other", title="Cheap flights guide"),
                _hit("code", "other", title="Python github repo"),
            ],
            "travel",
        )
    )
    assert [r.id for r in out.results] == ["t"]


def test_default_and_agent_modes_are_noops() -> None:
    plugin = ModeCategoryFilter()
    for mode in ("default", "agent"):
        out = plugin.run(_context([_hit("a", "other")], mode))
        assert [r.id for r in out.results] == ["a"]
        assert "mode-category-filter" not in out.applied_filters
        assert "mode-category-filter-removed" not in out.metadata


def test_empty_filter_falls_back_with_flag() -> None:
    plugin = ModeCategoryFilter()
    out = plugin.run(_context([_hit("g", "other", title="github")], "news"))
    assert [r.id for r in out.results] == ["g"]
    assert out.metadata["mode-category-filter-fallback"] is True
    assert out.metadata["mode-category-filter-removed"] == 1


def test_empty_results_stay_empty() -> None:
    plugin = ModeCategoryFilter()
    out = plugin.run(_context([], "news"))
    assert out.results == []
    assert "mode-category-filter-fallback" not in out.metadata

"""Date-range filter tests: time-boxed searches keep exact totals.

Covers ``plugins/date_range_filter.py``: ``date_from``/``date_to``
(``YYYY-MM-DD``, inclusive) keep hits whose ``published_at`` /
``published`` / ``date`` falls inside the window; undated hits are
excluded while a bound is set; missing or malformed bounds pass
results through untouched with no applied-filter entry; an empty
result is honest (no fallback).
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
from openlens.plugins.date_range_filter import DateRangeFilter


def _hit(
    hit_id: str,
    published: str | None = None,
    url: str = "https://example.com/a",
) -> SearchResult:
    metadata = {"published_at": published} if published is not None else {}
    return SearchResult(
        id=hit_id,
        title="title",
        snippet="snippet",
        original_score=1.0,
        final_score=1.0,
        url=url,
        provider="test",
        metadata=metadata,
    )


def _context(results: list[SearchResult], filters: dict | None = None) -> SearchContext:
    ctx = SearchContext(request=SearchRequest(query="q", filters=dict(filters or {})))
    ctx.results = list(results)
    return ctx


def test_declaration() -> None:
    plugin = DateRangeFilter()
    assert plugin.name == "date-range-filter"
    assert plugin.plugin_type is PluginType.FILTER
    assert plugin.stage is PipelineStage.FILTERING
    assert plugin.mandatory is False


def test_registry_resolves_short_name() -> None:
    built = PluginRegistry().build([{"name": "date-range-filter"}])
    assert [p.name for p in built] == ["date-range-filter"]


def test_window_keeps_only_in_range() -> None:
    ctx = _context(
        [
            _hit("in", "2026-10-25T12:00:00Z"),
            _hit("before", "2026-10-19"),
            _hit("after", "2026-11-01"),
        ],
        {"date_from": "2026-10-20", "date_to": "2026-10-31"},
    )
    out = DateRangeFilter().run(ctx)
    assert [r.id for r in out.results] == ["in"]
    assert "date-range" in out.applied_filters


def test_bounds_are_inclusive() -> None:
    ctx = _context(
        [_hit("from", "2026-10-20"), _hit("to", "2026-10-31T23:59:59Z")],
        {"date_from": "2026-10-20", "date_to": "2026-10-31"},
    )
    out = DateRangeFilter().run(ctx)
    assert [r.id for r in out.results] == ["from", "to"]


def test_single_sided_bounds() -> None:
    ctx = _context(
        [_hit("old", "2020-01-01"), _hit("new", "2026-10-25")],
        {"date_from": "2026-01-01"},
    )
    assert [r.id for r in DateRangeFilter().run(ctx).results] == ["new"]
    ctx = _context(
        [_hit("old", "2020-01-01"), _hit("new", "2026-10-25")],
        {"date_to": "2021-01-01"},
    )
    assert [r.id for r in DateRangeFilter().run(ctx).results] == ["old"]


def test_undated_hits_excluded_while_range_active() -> None:
    ctx = _context(
        [_hit("dated", "2026-10-25"), _hit("undated", None)],
        {"date_from": "2026-10-01"},
    )
    out = DateRangeFilter().run(ctx)
    assert [r.id for r in out.results] == ["dated"]


def test_no_bounds_passes_through_untouched() -> None:
    results = [_hit("a", "2026-10-25"), _hit("b", None)]
    out = DateRangeFilter().run(_context(results, {}))
    assert [r.id for r in out.results] == ["a", "b"]
    assert "date-range" not in out.applied_filters


def test_malformed_bounds_degrade_to_unset() -> None:
    results = [_hit("a", "2026-10-25"), _hit("b", None)]
    for filters in (
        {"date_from": "not-a-date", "date_to": "2026-10-31"},
        {"date_from": "2026-10-31", "date_to": "garbage"},
        {"date_from": 20261025},
        {"date_from": "  "},
    ):
        out = DateRangeFilter().run(_context(results, filters))
        assert [r.id for r in out.results] == ["a", "b"]
        assert "date-range" not in out.applied_filters


def test_empty_result_is_honest_no_fallback() -> None:
    ctx = _context([_hit("old", "2020-01-01")], {"date_from": "2026-01-01"})
    out = DateRangeFilter().run(ctx)
    assert out.results == []
    assert "date-range" in out.applied_filters

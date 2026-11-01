"""Date-range acceptance: config entry, registry wiring, pipeline effect.

Proves the end-to-end seam: ``configs/default.yaml`` declares the
``date-range-filter`` entry, the registry builds it from the short
name, and a ``user-restrictions`` + ``date-range-filter`` pipeline
reduces a fixture hit-list to exactly the in-window subset with the
``date-range`` applied-filter label (exact totals flow from the
engine fill loop, covered by the engine unit tests).
"""

from __future__ import annotations

from pathlib import Path

from openlens.config import load_config
from openlens.core.context import SearchContext
from openlens.core.interfaces import SearchRequest, SearchResult
from openlens.core.pipeline import Pipeline
from openlens.core.registry import PluginRegistry

CONFIG_PATH = Path(__file__).resolve().parents[2] / "configs" / "default.yaml"


def _hit(hit_id: str, published: str | None) -> SearchResult:
    metadata = {"published_at": published} if published is not None else {}
    return SearchResult(
        id=hit_id,
        title=f"title-{hit_id}",
        snippet=f"snippet-{hit_id}",
        original_score=1.0,
        final_score=1.0,
        url=f"https://example.com/{hit_id}",
        provider="test",
        metadata=metadata,
    )


def test_default_config_declares_date_range_filter() -> None:
    config = load_config(str(CONFIG_PATH))
    names = [entry.get("name") for entry in config.get("plugins", [])]
    assert "date-range-filter" in names


def test_registry_builds_date_range_filter_from_short_name() -> None:
    built = PluginRegistry().build([{"name": "date-range-filter"}])
    assert [p.name for p in built] == ["date-range-filter"]


def test_pipeline_applies_range_with_attribution() -> None:
    plugins = PluginRegistry().build(
        [
            {"name": "user-restrictions", "enabled": True, "config": {}},
            {"name": "date-range-filter", "enabled": True, "config": {}},
        ]
    )
    request = SearchRequest(
        query="q",
        filters={"date_from": "2026-10-20", "date_to": "2026-10-31"},
    )
    ctx = SearchContext(
        request=request,
        results=[
            _hit("in", "2026-10-25T12:00:00Z"),
            _hit("out", "2026-09-01"),
            _hit("undated", None),
        ],
    )
    out = Pipeline(plugins).run(ctx)
    assert [r.id for r in out.results] == ["in"]
    assert "date-range" in out.applied_filters
    assert out.metadata["date-range-filter-removed"] == 2
    assert out.metadata["date-range-filter-kept"] == 1


def test_pipeline_passes_range_keys_through_restrictions() -> None:
    """Reserved consumer keys survive user-restrictions untouched."""
    plugins = PluginRegistry().build(
        [
            {"name": "user-restrictions", "enabled": True, "config": {}},
            {"name": "date-range-filter", "enabled": True, "config": {}},
        ]
    )
    request = SearchRequest(
        query="q",
        filters={"date_from": "2026-01-01", "sort": "newest"},
    )
    ctx = SearchContext(request=request, results=[_hit("a", "2026-06-01")])
    out = Pipeline(plugins).run(ctx)
    assert [r.id for r in out.results] == ["a"]
    assert out.request.filters["sort"] == "newest"

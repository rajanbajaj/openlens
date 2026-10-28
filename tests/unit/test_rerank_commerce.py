"""Commerce-reranker constraint ordering proof (rerank-pack plan step 4).

Falsifiable checks on hand-built hits carrying ``price``/``currency``/
``location``/``facilities`` metadata: budget/location/facilities
constraints reorder hits by the ``commerce`` signal with cited reasons,
currency mismatch degrades to neutral with a reason, missing attributes
fall back to keyword behavior with a reason saying so, and unsatisfied
constraints demote without ever excluding a hit.
"""

from __future__ import annotations

from typing import Any

from openlens.core.context import SearchContext
from openlens.core.interfaces import SearchRequest, SearchResult
from openlens.plugins._signals import get_signal
from openlens.plugins.commerce_reranker import CommerceReranker


def _hit(
    hit_id: str, title: str, snippet: str, metadata: dict[str, Any]
) -> SearchResult:
    return SearchResult(
        id=hit_id,
        title=title,
        snippet=snippet,
        original_score=1.0,
        final_score=1.0,
        url=f"https://shop.example.com/{hit_id}",
        provider="test",
        metadata=dict(metadata),
    )


def _run(
    results: list[SearchResult], query: str, filters: dict[str, Any]
) -> SearchContext:
    ctx = SearchContext(request=SearchRequest(query=query, filters=filters))
    ctx.results = results
    CommerceReranker({}).run(ctx)
    return ctx


def _by_signal(ctx: SearchContext) -> list[str]:
    ordered = sorted(ctx.results, key=lambda r: get_signal(r, "commerce"), reverse=True)
    return [r.id for r in ordered]


def test_budget_reorders_with_cited_reason() -> None:
    ctx = _run(
        [
            _hit("over", "Hotel deal downtown", "Great hotel deal", {"price": 300.0}),
            _hit("under", "Hotel deal uptown", "Great hotel deal", {"price": 80.0}),
        ],
        query="hotel deal",
        filters={"max_price": 100},
    )
    assert _by_signal(ctx) == ["under", "over"]
    assert get_signal(ctx.results[1], "commerce") == 1.0
    assert get_signal(ctx.results[0], "commerce") == 0.0
    assert "within your budget" in ctx.results[1].reasons[-1]
    assert "over your budget" in ctx.results[0].reasons[-1]
    assert len(ctx.results) == 2  # demote, never exclude


def test_location_match_is_case_insensitive() -> None:
    ctx = _run(
        [
            _hit("far", "Beach resort", "Sunny beach resort", {"location": "Miami"}),
            _hit(
                "near",
                "Beach resort",
                "Sunny beach resort",
                {"location": "  LISBON "},
            ),
        ],
        query="beach resort",
        filters={"location": "lisbon"},
    )
    assert _by_signal(ctx) == ["near", "far"]
    assert "matches location" in ctx.results[1].reasons[-1]


def test_facilities_reorder_with_cited_reason() -> None:
    ctx = _run(
        [
            _hit("basic", "Hotel stay", "Cosy hotel stay", {"facilities": ["wifi"]}),
            _hit(
                "full",
                "Hotel stay",
                "Cosy hotel stay",
                {"facilities": ["WiFi", "Pool"]},
            ),
        ],
        query="hotel stay",
        filters={"facilities": ["wifi", "pool"]},
    )
    assert _by_signal(ctx) == ["full", "basic"]
    assert get_signal(ctx.results[1], "commerce") == 1.0
    assert get_signal(ctx.results[0], "commerce") == 0.5
    assert "matches facilities" in ctx.results[1].reasons[-1]
    assert "missing pool" in ctx.results[0].reasons[-1]


def test_combined_constraints_satisfied_fraction() -> None:
    ctx = _run(
        [
            _hit(
                "both",
                "Hotel deal",
                "Great hotel deal",
                {"price": 80.0, "location": "Lisbon"},
            ),
            _hit(
                "one",
                "Hotel deal",
                "Great hotel deal",
                {"price": 80.0, "location": "Madrid"},
            ),
            _hit(
                "neither",
                "Hotel deal",
                "Great hotel deal",
                {"price": 500.0, "location": "Madrid"},
            ),
        ],
        query="hotel deal",
        filters={"max_price": 100, "location": "lisbon"},
    )
    assert _by_signal(ctx) == ["both", "one", "neither"]
    assert get_signal(ctx.results[0], "commerce") == 1.0
    assert get_signal(ctx.results[1], "commerce") == 0.5
    assert get_signal(ctx.results[2], "commerce") == 0.0
    assert "within your budget" in ctx.results[0].reasons[-1]
    assert "matches location" in ctx.results[0].reasons[-1]


def test_no_constraints_is_neutral() -> None:
    ctx = _run(
        [_hit("a", "Hotel deal", "Great hotel deal", {"price": 10.0})],
        query="hotel deal",
        filters={},
    )
    assert get_signal(ctx.results[0], "commerce") == 0.5


def test_currency_mismatch_is_neutral_with_reason() -> None:
    ctx = _run(
        [
            _hit(
                "cheap-foreign",
                "Hotel deal",
                "Great hotel deal",
                {"price": 1.0, "currency": "EUR"},
            ),
        ],
        query="hotel deal",
        filters={"max_price": {"value": 100, "currency": "USD"}},
    )
    assert get_signal(ctx.results[0], "commerce") == 0.5
    assert "currency mismatch" in ctx.results[0].reasons[-1]


def test_matching_currency_compares() -> None:
    ctx = _run(
        [
            _hit(
                "ok",
                "Hotel deal",
                "Great hotel deal",
                {"price": 80.0, "currency": "usd"},
            ),
        ],
        query="hotel deal",
        filters={"max_price": {"value": 100, "currency": "USD"}},
    )
    assert get_signal(ctx.results[0], "commerce") == 1.0
    assert "within your budget" in ctx.results[0].reasons[-1]


def test_missing_attributes_keyword_fallback_with_reason() -> None:
    ctx = _run(
        [
            _hit("kw", "Hotel deal downtown", "Great hotel deal", {}),
            _hit("no-kw", "Quiet museum tour", "Calm galleries", {}),
        ],
        query="hotel deal",
        filters={"max_price": 100},
    )
    assert get_signal(ctx.results[0], "commerce") == 1.0
    assert get_signal(ctx.results[1], "commerce") == 0.0
    assert "keyword fallback" in ctx.results[0].reasons[-1]
    assert "keyword fallback" in ctx.results[1].reasons[-1]


def test_malformed_filters_never_raise_and_stay_neutral() -> None:
    ctx = _run(
        [_hit("a", "Hotel deal", "Great hotel deal", {"price": 10.0})],
        query="hotel deal",
        filters={"max_price": "free", "location": "", "facilities": []},
    )
    assert get_signal(ctx.results[0], "commerce") == 0.5

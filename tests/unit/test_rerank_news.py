"""News grouping + perspective rerank (plan step 3).

Falsifiable checks over hand-built hits: near-duplicate titles merge per
the pinned bigram/Jaccard/date rule, the perspective mix is
credibility-weighted (a distrusted outlier ranks below, never
equal-weight), ``story_groups`` has the documented shape on
``SearchContext.metadata`` (internal only, absent from diagnostics),
hits beyond K=50 carry no ``story_id``, and the ``trusted``-bool
fallback works with scoring signals absent.
"""

from __future__ import annotations

from typing import Any

import pytest

from openlens.core.context import SearchContext
from openlens.core.interfaces import ConfigError, SearchRequest, SearchResult
from openlens.plugins.news_reranker import NewsReranker

DAY_A = "2026-01-10"
DAY_B = "2026-02-01"


def _hit(
    hit_id: str,
    title: str,
    *,
    published: str | None = None,
    perspective: str | None = None,
    trust: float | None = None,
    trusted: bool | None = None,
) -> SearchResult:
    metadata: dict[str, Any] = {}
    if published is not None:
        metadata["published"] = published
    if perspective is not None:
        metadata["perspective"] = perspective
    if trust is not None:
        metadata["signals"] = {"trust": trust}
    if trusted is not None:
        metadata["trusted"] = trusted
    return SearchResult(
        id=hit_id,
        title=title,
        snippet="A news snippet.",
        original_score=1.0,
        final_score=1.0,
        url=None,
        provider="test",
        metadata=metadata,
    )


def _ctx(results: list[SearchResult]) -> SearchContext:
    ctx = SearchContext(request=SearchRequest(query="transit plan", mode="news"))
    ctx.results = results
    return ctx


def _run(results: list[SearchResult], **config: Any) -> SearchContext:
    ctx = _ctx(results)
    NewsReranker(dict(config)).run(ctx)
    return ctx


def _grouping_hits() -> list[SearchResult]:
    return [
        _hit(
            "n1",
            "City council approves new transit plan",
            published=DAY_A,
            perspective="supporting",
        ),
        _hit(
            "n2",
            "City council approves new transit plan expansion",
            published=DAY_A,
            perspective="critical",
        ),
        _hit(
            "n3",
            "City council approves new transit plan",
            published=DAY_B,
            perspective="supporting",
        ),
        _hit(
            "n4",
            "Local bakery wins national award",
            published=DAY_A,
            perspective="supporting",
        ),
    ]


def test_near_duplicate_titles_group_per_pinned_rule() -> None:
    ctx = _run(_grouping_hits())
    n1, n2, n3, n4 = ctx.results
    assert n1.metadata["story_id"] == n2.metadata["story_id"]
    # Same title but a different published day: no merge.
    assert "story_id" not in n3.metadata
    # Unrelated title on the same day: no merge.
    assert "story_id" not in n4.metadata


def test_credibility_weighted_mix_distrusted_outlier_below() -> None:
    ctx = _run(
        [
            _hit(
                "b1",
                "Mayor unveils downtown housing initiative",
                published=DAY_A,
                perspective="supporting",
                trust=0.9,
            ),
            _hit(
                "b2",
                "Mayor unveils downtown housing initiative today",
                published=DAY_A,
                perspective="critical",
                trust=0.2,
            ),
            _hit(
                "b3",
                "Mayor unveils downtown housing plan",
                published=DAY_A,
                perspective="critical",
            ),
        ]
    )
    b1, b2, b3 = ctx.results
    assert b1.metadata["story_id"] == b2.metadata["story_id"] == b3.metadata["story_id"]
    s1 = b1.metadata["signals"]["perspective"]
    s2 = b2.metadata["signals"]["perspective"]
    s3 = b3.metadata["signals"]["perspective"]
    # Most credible member of each perspective keeps its credibility ...
    assert s1 == 0.9
    assert s3 == 0.5
    # ... other same-perspective members get half; never equal-weight.
    assert s2 == 0.25
    assert len({s1, s2, s3}) == 3
    # The distrusted outlier ranks below the trusted member.
    assert s2 < s1


def test_story_groups_shape_internal_only() -> None:
    ctx = _run(_grouping_hits())
    assert ctx.metadata["story_groups"] == {
        ctx.results[0].metadata["story_id"]: {
            "members": ["n1", "n2"],
            "perspectives": ["critical", "supporting"],
        }
    }
    assert "story_groups" not in ctx.diagnostics()


def _pair_hits(total_pairs: int) -> list[SearchResult]:
    hits: list[SearchResult] = []
    for i in range(total_pairs):
        hits.append(
            _hit(f"p{i}a", f"Harbor bridge reopening phase {i} alpha", published=DAY_A)
        )
        hits.append(
            _hit(f"p{i}b", f"Harbor bridge reopening phase {i} beta", published=DAY_A)
        )
    return hits


def test_k_cap_hits_beyond_window_ungrouped() -> None:
    ctx = _run(_pair_hits(26))
    assert len(ctx.metadata["story_groups"]) == 25
    assert "story_id" in ctx.results[0].metadata
    assert "story_id" not in ctx.results[50].metadata
    assert "story_id" not in ctx.results[51].metadata


def test_top_k_is_configurable() -> None:
    ctx = _run(_pair_hits(2), top_k=2)
    assert ctx.results[0].metadata["story_id"] == (ctx.results[1].metadata["story_id"])
    assert "story_id" not in ctx.results[2].metadata
    assert "story_id" not in ctx.results[3].metadata


def test_trusted_bool_fallback_without_signals() -> None:
    ctx = _run(
        [
            _hit(
                "t1",
                "River flood barriers completed",
                published=DAY_A,
                perspective="supporting",
                trusted=True,
            ),
            _hit(
                "t2",
                "River flood barriers completed today",
                published=DAY_A,
                perspective="supporting",
                trusted=False,
            ),
            _hit(
                "t3",
                "River flood barriers completed early",
                published=DAY_A,
                perspective="critical",
            ),
        ]
    )
    t1, t2, t3 = ctx.results
    for result in (t1, t2, t3):
        assert "signals" not in result.metadata or (
            "trust" not in result.metadata["signals"]
        )
    assert t1.metadata["story_id"] == t2.metadata["story_id"]
    assert t1.metadata["signals"]["perspective"] == 1.0
    assert t2.metadata["signals"]["perspective"] == 0.5
    # No trust evidence at all degrades to neutral, below the trusted hit.
    assert t3.metadata["signals"]["perspective"] == 0.5
    assert t3.metadata["signals"]["perspective"] < t1.metadata["signals"]["perspective"]


def test_oversize_block_left_unmerged() -> None:
    hits = [
        _hit(f"m{i}", "Harbor bridge reopening delayed again", published=DAY_A)
        for i in range(30)
    ]
    ctx = _run(hits)
    assert ctx.metadata["story_groups"] == {}
    assert all("story_id" not in result.metadata for result in ctx.results)


def test_invalid_top_k_rejected() -> None:
    with pytest.raises(ConfigError):
        NewsReranker({"top_k": 0})
    with pytest.raises(ConfigError):
        NewsReranker({"top_k": "many"})

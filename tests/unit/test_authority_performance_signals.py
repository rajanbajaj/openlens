"""Authority + performance signals (backlink and page-speed ranking factors).

Accept: the authority curve saturates (0 -> 0.0, half -> 0.5, large ->
~1.0) with neutral on missing/garbage counts; the performance curve is
1.0 below the fast bound, 0.0 above the slow bound, log-decaying between,
neutral on missing/garbage latencies; both scorers write their signal
plus one reason per hit, never touch final_score, and reject bad config
loudly.
"""

from __future__ import annotations

import pytest

from openlens.core.context import SearchContext
from openlens.core.interfaces import ConfigError, SearchRequest, SearchResult
from openlens.plugins.authority_scorer import AuthorityScorer, authority_score
from openlens.plugins.performance_scorer import (
    PerformanceScorer,
    performance_score,
)


def _hit(metadata: dict | None = None) -> SearchResult:
    return SearchResult(
        id="a",
        title="Alpha",
        snippet="",
        original_score=1.0,
        final_score=1.0,
        metadata=dict(metadata or {}),
    )


def _ctx(hit: SearchResult) -> SearchContext:
    ctx = SearchContext(request=SearchRequest(query="q", mode="default"))
    ctx.results = [hit]
    return ctx


# --- authority curve ---------------------------------------------------------


def test_authority_curve_saturates() -> None:
    assert authority_score(0) == pytest.approx(0.0)
    assert authority_score(10) == pytest.approx(0.5)
    assert authority_score(1_000_000) == pytest.approx(1.0, abs=1e-3)
    assert authority_score(10, half=4.0) == pytest.approx(10 / 14)


def test_authority_curve_neutral_on_garbage() -> None:
    for bad in (None, "many", True, -3, float("nan"), [2]):
        assert authority_score(bad) == pytest.approx(0.5)


def test_authority_scorer_writes_signal_and_reason() -> None:
    scorer = AuthorityScorer()
    scorer.configure({})
    hit = _hit({"inlink_count": 10})
    scorer.run(_ctx(hit))
    assert hit.metadata["signals"]["authority"] == pytest.approx(0.5)
    assert len(hit.reasons) == 1
    assert hit.final_score == pytest.approx(1.0)


def test_authority_scorer_neutral_without_count() -> None:
    scorer = AuthorityScorer()
    scorer.configure({})
    hit = _hit()
    scorer.run(_ctx(hit))
    assert hit.metadata["signals"]["authority"] == pytest.approx(0.5)


def test_authority_scorer_rejects_bad_half() -> None:
    scorer = AuthorityScorer()
    with pytest.raises(ConfigError):
        scorer.configure({"half_saturation": 0})
    with pytest.raises(ConfigError):
        scorer.configure({"half_saturation": "lots"})


# --- performance curve -------------------------------------------------------


def test_performance_curve_bounds() -> None:
    assert performance_score(0) == pytest.approx(1.0)
    assert performance_score(300) == pytest.approx(1.0)
    assert performance_score(8000) == pytest.approx(0.0)
    assert performance_score(80_000) == pytest.approx(0.0)


def test_performance_curve_decays_between_bounds() -> None:
    mid = performance_score(1500)
    assert 0.0 < mid < 1.0
    assert performance_score(1000) > mid > performance_score(3000)


def test_performance_curve_neutral_on_garbage() -> None:
    for bad in (None, "fast", True, -5, float("nan")):
        assert performance_score(bad) == pytest.approx(0.5)


def test_performance_scorer_writes_signal_and_reason() -> None:
    scorer = PerformanceScorer()
    scorer.configure({})
    hit = _hit({"fetch_ms": 100.0})
    scorer.run(_ctx(hit))
    assert hit.metadata["signals"]["performance"] == pytest.approx(1.0)
    assert len(hit.reasons) == 1
    assert hit.final_score == pytest.approx(1.0)


def test_performance_scorer_neutral_without_latency() -> None:
    scorer = PerformanceScorer()
    scorer.configure({})
    hit = _hit()
    scorer.run(_ctx(hit))
    assert hit.metadata["signals"]["performance"] == pytest.approx(0.5)


def test_performance_scorer_rejects_bad_bounds() -> None:
    scorer = PerformanceScorer()
    with pytest.raises(ConfigError):
        scorer.configure({"fast_ms": 8000, "slow_ms": 300})
    with pytest.raises(ConfigError):
        scorer.configure({"fast_ms": "quick"})

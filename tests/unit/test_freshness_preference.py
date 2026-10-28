"""Freshness + preference scorer tests (scoring-pack plan Steps 3-4)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from openlens.core.context import SearchContext
from openlens.core.interfaces import (
    ConfigError,
    PipelineStage,
    PluginType,
    SearchRequest,
    SearchResult,
)
from openlens.plugins.freshness_scorer import FreshnessScorer
from openlens.plugins.preference_scorer import PreferenceScorer

NOW = datetime(2026, 10, 28, tzinfo=timezone.utc)
NOW_ISO = "2026-10-28T00:00:00Z"
HALF_LIFE = 30.0


def _hit(
    hit_id: str = "h1",
    title: str = "title",
    snippet: str = "snippet",
    url: str = "https://example.com/a",
    metadata: dict | None = None,
) -> SearchResult:
    return SearchResult(
        id=hit_id,
        title=title,
        snippet=snippet,
        original_score=1.0,
        final_score=1.0,
        url=url,
        provider="test",
        metadata=dict(metadata or {}),
    )


def _context(results: list[SearchResult], filters: dict | None = None) -> SearchContext:
    ctx = SearchContext(request=SearchRequest(query="q", filters=dict(filters or {})))
    ctx.results = list(results)
    return ctx


def _freshener(**config: object) -> FreshnessScorer:
    return FreshnessScorer({"now": NOW_ISO, **config})


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Freshness: decay curve
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("age_days", "expected"),
    [(0, 1.0), (30, 0.5), (60, 0.25), (90, 0.125)],
)
def test_decay_exact_at_half_life_multiples(age_days: int, expected: float) -> None:
    hit = _hit(metadata={"published_at": _iso(NOW - timedelta(days=age_days))})
    ctx = _context([hit])
    _freshener(half_life_days=HALF_LIFE).run(ctx)
    assert ctx.results[0].metadata["signals"]["freshness"] == pytest.approx(expected)


def test_fresh_result_scores_one() -> None:
    hit = _hit(metadata={"published_at": NOW_ISO})
    ctx = _context([hit])
    _freshener().run(ctx)
    assert ctx.results[0].metadata["signals"]["freshness"] == pytest.approx(1.0)


def test_future_date_clamps_to_one() -> None:
    hit = _hit(metadata={"published_at": _iso(NOW + timedelta(days=5))})
    ctx = _context([hit])
    _freshener().run(ctx)
    assert ctx.results[0].metadata["signals"]["freshness"] == pytest.approx(1.0)


def test_custom_half_life() -> None:
    hit = _hit(metadata={"published_at": _iso(NOW - timedelta(days=10))})
    ctx = _context([hit])
    _freshener(half_life_days=10).run(ctx)
    assert ctx.results[0].metadata["signals"]["freshness"] == pytest.approx(0.5)


@pytest.mark.parametrize("bad", [0, -3, "soon", float("nan")])
def test_bad_half_life_raises_config_error(bad: object) -> None:
    with pytest.raises(ConfigError):
        FreshnessScorer({"half_life_days": bad})


# ---------------------------------------------------------------------------
# Freshness: date keys, epoch forms, neutral handling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["published_at", "published", "date"])
def test_each_date_key_read(key: str) -> None:
    hit = _hit(metadata={key: _iso(NOW - timedelta(days=HALF_LIFE))})
    ctx = _context([hit])
    _freshener().run(ctx)
    assert ctx.results[0].metadata["signals"]["freshness"] == pytest.approx(0.5)


@pytest.mark.parametrize("as_float", [False, True])
def test_epoch_forms(as_float: bool) -> None:
    epoch = (NOW - timedelta(days=HALF_LIFE)).timestamp()
    hit = _hit(metadata={"published": float(epoch) if as_float else int(epoch)})
    ctx = _context([hit])
    # int() truncation shifts by < 1s; both forms must land near 0.5.
    _freshener().run(ctx)
    assert ctx.results[0].metadata["signals"]["freshness"] == pytest.approx(
        0.5, abs=1e-6
    )


def test_first_present_key_wins() -> None:
    hit = _hit(
        metadata={
            "published_at": _iso(NOW - timedelta(days=60)),
            "published": _iso(NOW),
        }
    )
    ctx = _context([hit])
    _freshener().run(ctx)
    assert ctx.results[0].metadata["signals"]["freshness"] == pytest.approx(0.25)


def test_dateless_is_neutral_with_reason() -> None:
    hit = _hit()
    ctx = _context([hit])
    _freshener().run(ctx)
    assert ctx.results[0].metadata["signals"]["freshness"] == pytest.approx(0.5)
    assert any("neutral" in r for r in ctx.results[0].reasons)


@pytest.mark.parametrize("raw", ["not-a-date", "", "2026-13-99", True, {"d": 1}, [1]])
def test_unparseable_is_neutral_with_reason(raw: object) -> None:
    hit = _hit(metadata={"published_at": raw})
    ctx = _context([hit])
    _freshener().run(ctx)
    assert ctx.results[0].metadata["signals"]["freshness"] == pytest.approx(0.5)
    assert any("neutral" in r for r in ctx.results[0].reasons)


def test_recency_score_backfill_equals_signal() -> None:
    hits = [
        _hit("fresh", metadata={"published_at": NOW_ISO}),
        _hit("old", metadata={"date": _iso(NOW - timedelta(days=60))}),
        _hit("dateless"),
    ]
    ctx = _context(hits)
    _freshener().run(ctx)
    for result in ctx.results:
        assert result.metadata["recency_score"] == pytest.approx(
            result.metadata["signals"]["freshness"]
        )
    assert ctx.results[0].metadata["recency_score"] == pytest.approx(1.0)
    assert ctx.results[1].metadata["recency_score"] == pytest.approx(0.25)
    assert ctx.results[2].metadata["recency_score"] == pytest.approx(0.5)


def test_freshness_plugin_metadata() -> None:
    scorer = _freshener()
    assert scorer.name == "freshness-scorer"
    assert scorer.plugin_type is PluginType.SCORER
    assert scorer.stage is PipelineStage.SCORING
    assert scorer.mandatory is False


# ---------------------------------------------------------------------------
# Preference: matching
# ---------------------------------------------------------------------------


def test_no_preferences_is_neutral() -> None:
    hit = _hit()
    ctx = _context([hit])
    PreferenceScorer({}).run(ctx)
    assert ctx.results[0].metadata["signals"]["preference"] == pytest.approx(0.5)
    assert "preference_warnings" not in ctx.metadata


def test_domain_exact_and_subdomain_match() -> None:
    hits = [
        _hit("exact", url="https://Example.COM/article"),
        _hit("sub", url="https://news.example.com/article"),
        _hit("other", url="https://other.org/article"),
    ]
    ctx = _context(hits, {"prefer_domains": ["example.com"]})
    PreferenceScorer({}).run(ctx)
    assert ctx.results[0].metadata["signals"]["preference"] == pytest.approx(0.5)
    assert ctx.results[1].metadata["signals"]["preference"] == pytest.approx(0.5)
    assert ctx.results[2].metadata["signals"]["preference"] == pytest.approx(0.0)


def test_domain_suffix_is_not_a_match() -> None:
    hit = _hit(url="https://notexample.com/a")
    ctx = _context([hit], {"prefer_domains": ["example.com"]})
    PreferenceScorer({}).run(ctx)
    assert ctx.results[0].metadata["signals"]["preference"] == pytest.approx(0.0)


def test_topic_case_insensitive_token_match() -> None:
    hits = [
        _hit("t", title="RUNNING shoes review", snippet="plain text"),
        _hit("s", title="plain", snippet="Great for Running trails"),
        _hit("x", title="plain", snippet="nothing relevant"),
    ]
    ctx = _context(hits, {"prefer_topics": ["running"]})
    PreferenceScorer({}).run(ctx)
    assert ctx.results[0].metadata["signals"]["preference"] == pytest.approx(0.5)
    assert ctx.results[1].metadata["signals"]["preference"] == pytest.approx(0.5)
    assert ctx.results[2].metadata["signals"]["preference"] == pytest.approx(0.0)


def test_topic_token_not_substring() -> None:
    hit = _hit(title="running late", snippet="marathon")
    ctx = _context([hit], {"prefer_topics": ["run"]})
    PreferenceScorer({}).run(ctx)
    assert ctx.results[0].metadata["signals"]["preference"] == pytest.approx(0.0)


def test_domain_and_topic_boosts_stack_bounded() -> None:
    hit = _hit(
        title="Python guide",
        snippet="tutorial",
        url="https://docs.example.com/x",
    )
    ctx = _context(
        [hit], {"prefer_domains": ["example.com"], "prefer_topics": ["python"]}
    )
    PreferenceScorer({}).run(ctx)
    assert ctx.results[0].metadata["signals"]["preference"] == pytest.approx(1.0)


def test_boosts_clamped_and_validated() -> None:
    with pytest.raises(ConfigError):
        PreferenceScorer({"domain_boost": 1.5})
    with pytest.raises(ConfigError):
        PreferenceScorer({"topic_boost": "lots"})  # type: ignore[dict-item]


# ---------------------------------------------------------------------------
# Preference: request scoping (no cross-query state)
# ---------------------------------------------------------------------------


def test_boosts_scoped_per_request_no_leak() -> None:
    scorer = PreferenceScorer({})
    first = _context(
        [_hit(url="https://example.com/a")],
        {"prefer_domains": ["example.com"]},
    )
    scorer.run(first)
    assert first.results[0].metadata["signals"]["preference"] == pytest.approx(0.5)

    second = _context([_hit(url="https://example.com/a")])
    scorer.run(second)
    assert second.results[0].metadata["signals"]["preference"] == pytest.approx(0.5)
    assert "preference_warnings" not in second.metadata
    assert any("no preferences" in r for r in second.results[0].reasons)


def test_malformed_filters_ignored_and_noted() -> None:
    hit = _hit(url="https://example.com/a", title="hello", snippet="world")
    ctx = _context(
        [hit],
        {"prefer_domains": "example.com", "prefer_topics": ["ok", 42, None]},
    )
    PreferenceScorer({}).run(ctx)
    # Non-list prefer_domains ignored; "ok" matches nothing -> 0.0.
    assert ctx.results[0].metadata["signals"]["preference"] == pytest.approx(0.0)
    warnings = ctx.metadata["preference_warnings"]
    assert len(warnings) == 2
    assert any("prefer_domains" in w for w in warnings)
    assert any("prefer_topics" in w for w in warnings)


def test_preference_plugin_metadata() -> None:
    scorer = PreferenceScorer({})
    assert scorer.name == "preference-scorer"
    assert scorer.plugin_type is PluginType.SCORER
    assert scorer.stage is PipelineStage.SCORING
    assert scorer.mandatory is False

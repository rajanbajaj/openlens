"""Scoring-pack ordering proof (plan step 6) + shipped-corpus neutral smoke.

Falsifiable check: hand-built hits carry spread ``published_at`` values
across half-lives plus one trusted-tier and one distrusted-tier domain.
The same query under the ``default`` vs ``news`` profile must return
different top-3 orderings, and the moved hits' reasons must cite the
``trust``/``freshness`` signals. This test fails if either signal is
neutral-uniform, if the per-mode weights do not differ, or if cited
reasons are absent.

The shipped ``tests/fixtures/corpus.py`` is used only as a neutral-signal
smoke check (uniform signals, ordering unchanged) — it has no dates or
reputation tiers to differentiate on, so it cannot drive the ordering
proof above.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from openlens.core.context import SearchContext
from openlens.core.interfaces import SearchRequest, SearchResult
from openlens.plugins.freshness_scorer import FreshnessScorer
from openlens.plugins.preference_scorer import PreferenceScorer
from openlens.plugins.trust_scorer import TrustScorer
from openlens.ranking.profiles import get_profile
from openlens.ranking.ranker import Ranker
from tests.fixtures.corpus import fixture_documents

NOW = datetime(2026, 1, 15, tzinfo=timezone.utc)
NOW_ISO = NOW.isoformat().replace("+00:00", "Z")
HALF_LIFE_DAYS = 30.0

TRUSTED_DOMAIN = "trusted.example.com"
DISTRUSTED_DOMAIN = "evil.example.net"
REPUTATION: dict[str, Any] = {
    "reputation": {"trusted": [TRUSTED_DOMAIN], "distrusted": [DISTRUSTED_DOMAIN]}
}


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _pack_hits() -> list[SearchResult]:
    """Four hits with identical base scores; only signals differentiate."""

    def hit(
        hit_id: str, url: str, age_days: int, original: float = 1.0
    ) -> SearchResult:
        return SearchResult(
            id=hit_id,
            title=f"Running shoes guide {hit_id}",
            snippet="A review of running shoes for daily training.",
            original_score=original,
            final_score=original,
            url=url,
            provider="test",
            metadata={"published_at": _iso(NOW - timedelta(days=age_days))},
        )

    return [
        hit("hit-fresh-trusted", f"https://{TRUSTED_DOMAIN}/fresh", 0),
        hit("hit-stale-trusted", f"https://{TRUSTED_DOMAIN}/stale", 90),
        hit("hit-mid-unknown", "https://plain.example.org/mid", 30),
        hit("hit-stale-distrusted", f"https://{DISTRUSTED_DOMAIN}/stale", 90),
    ]


def _score(results: list[SearchResult]) -> SearchContext:
    """Run the trust + freshness scorers over one request context."""
    ctx = SearchContext(request=SearchRequest(query="running shoes"))
    ctx.results = results
    TrustScorer(dict(REPUTATION)).run(ctx)
    FreshnessScorer({"now": NOW_ISO, "half_life_days": HALF_LIFE_DAYS}).run(ctx)
    return ctx


def _rank(ctx: SearchContext, mode: str) -> list[SearchResult]:
    ctx.request.mode = mode
    ranker = Ranker()
    ranker.configure({})
    ranker.run(ctx)
    return list(ctx.results)


def test_per_mode_signal_weights_differ() -> None:
    default = get_profile("default")
    news = get_profile("news")
    assert default.freshness_weight != news.freshness_weight
    assert default.preference_weight != news.preference_weight
    assert (default.trust_weight, default.freshness_weight) == (0.5, 0.25)
    assert (news.trust_weight, news.freshness_weight) == (0.5, 2.0)


def test_scoring_signals_are_not_neutral_uniform() -> None:
    ctx = _score(_pack_hits())
    freshness = {r.metadata["signals"]["freshness"] for r in ctx.results}
    trust = {r.metadata["signals"]["trust"] for r in ctx.results}
    assert len(freshness) > 1
    assert len(trust) > 1
    assert any(v != 0.5 for v in freshness)
    assert any(v != 0.5 for v in trust)


def test_default_vs_news_top3_differs_with_cited_reasons() -> None:
    default_order = [r.id for r in _rank(_score(_pack_hits()), "default")[:3]]
    news_order = [r.id for r in _rank(_score(_pack_hits()), "news")[:3]]
    assert default_order != news_order

    default_ranked = _rank(_score(_pack_hits()), "default")
    news_ranked = _rank(_score(_pack_hits()), "news")
    default_pos = {r.id: i for i, r in enumerate(default_ranked)}
    news_pos = {r.id: i for i, r in enumerate(news_ranked)}
    moved = [r for r in default_ranked if default_pos[r.id] != news_pos[r.id]]
    assert moved, "expected at least one hit to move between modes"
    for result in moved:
        reasons = " ".join(result.reasons).lower()
        assert "trust" in reasons, f"{result.id} reasons cite no trust signal"
        assert "freshness" in reasons, f"{result.id} reasons cite no freshness signal"


def test_fixture_corpus_is_neutral_smoke() -> None:
    """Shipped corpus: uniform signals, ranker ordering unchanged."""
    scored = [
        SearchResult(
            id=doc.id,
            title=doc.title,
            snippet=doc.content,
            original_score=10.0 - i,
            final_score=10.0 - i,
            url=doc.url,
            provider="test",
            metadata=dict(doc.metadata),
        )
        for i, doc in enumerate(fixture_documents())
    ]
    plain = [
        SearchResult(
            id=r.id,
            title=r.title,
            snippet=r.snippet,
            original_score=r.original_score,
            final_score=r.final_score,
            url=r.url,
            provider=r.provider,
            metadata=dict(r.metadata),
        )
        for r in scored
    ]
    ctx = SearchContext(request=SearchRequest(query="running shoes"))
    ctx.results = scored
    TrustScorer(dict(REPUTATION)).run(ctx)
    FreshnessScorer({"now": NOW_ISO, "half_life_days": HALF_LIFE_DAYS}).run(ctx)
    PreferenceScorer({}).run(ctx)

    freshness = {r.metadata["signals"]["freshness"] for r in scored}
    preference = {r.metadata["signals"]["preference"] for r in scored}
    trust = {r.metadata["signals"]["trust"] for r in scored}
    # No dates anywhere in the fixture corpus: every freshness signal is
    # exactly neutral. No preference filters: every preference signal is
    # exactly neutral. Fixture domains sit in no reputation tier, so every
    # trust signal is identical (uniform https bonus, no differentiation).
    assert freshness == {0.5}
    assert preference == {0.5}
    assert len(trust) == 1

    before = [
        r.id
        for r in _rank(
            SearchContext(
                request=SearchRequest(query="running shoes", mode="default"),
                results=plain,
            ),
            "default",
        )
    ]
    after = [r.id for r in _rank(ctx, "default")]
    assert after == before


def test_default_yaml_ships_scoring_pack_disabled() -> None:
    path = Path(__file__).resolve().parents[2] / "configs" / "default.yaml"
    config = yaml.safe_load(path.read_text())
    entries = {e["name"]: e for e in config["plugins"]}
    for name in ("trust-scorer", "freshness-scorer", "preference-scorer"):
        assert name in entries, f"{name} missing from configs/default.yaml"
        assert entries[name]["enabled"] is False, f"{name} must ship disabled"
    assert entries["baseline-scorer"]["enabled"] is True

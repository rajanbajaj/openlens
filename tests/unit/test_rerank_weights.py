"""Rerank profile weights + composite (rerank-pack plan step 5).

Accept: every mode carries the pinned rerank weights, the ranker composite
adds the diversity/perspective/commerce signal terms with neutral-0.5
defaults (absent signals keep existing orderings unchanged), the YAML and
builtins stay in parity, each reranker cites at most one concise reason per
result, and repeated runs are byte-identical in ordering.
"""

from __future__ import annotations

import pytest

from openlens.core.context import SearchContext
from openlens.core.interfaces import SearchRequest, SearchResult
from openlens.plugins.commerce_reranker import CommerceReranker
from openlens.plugins.diversity_reranker import DiversityReranker
from openlens.plugins.news_reranker import NewsReranker
from openlens.ranking.profiles import (
    BUILTIN_PROFILES,
    get_profile,
    load_profiles,
    profile_from_dict,
)
from openlens.ranking.ranker import Ranker
from openlens.core.interfaces import ConfigError


def _hit(
    id: str,
    title: str,
    snippet: str = "",
    original: float = 1.0,
    url: str | None = None,
) -> SearchResult:
    return SearchResult(
        id=id,
        title=title,
        snippet=snippet,
        original_score=original,
        final_score=original,
        url=url,
    )


def _ctx(
    hits: list[SearchResult],
    mode: str = "default",
    query: str = "q",
    filters: dict | None = None,
) -> SearchContext:
    ctx = SearchContext(
        request=SearchRequest(query=query, mode=mode, filters=filters or {})
    )
    ctx.results = hits
    return ctx


# --- Pinned per-mode weights -------------------------------------------------


def test_diversity_weight_present_in_every_mode() -> None:
    for mode in ("default", "child-safe", "news", "shopping", "travel"):
        assert get_profile(mode).diversity_weight == pytest.approx(0.25)


def test_perspective_weight_only_in_news() -> None:
    assert get_profile("news").perspective_weight == pytest.approx(1.0)
    for mode in ("default", "child-safe", "shopping", "travel"):
        assert get_profile(mode).perspective_weight == pytest.approx(0.0)


def test_commerce_weight_only_in_shopping_and_travel() -> None:
    for mode in ("shopping", "travel"):
        assert get_profile(mode).commerce_weight == pytest.approx(1.5)
    for mode in ("default", "child-safe", "news"):
        assert get_profile(mode).commerce_weight == pytest.approx(0.0)


# --- profile_from_dict: new keys ---------------------------------------------


def test_new_weight_keys_accepted_with_defaults() -> None:
    profile = profile_from_dict("x", {})
    assert (
        profile.diversity_weight,
        profile.perspective_weight,
        profile.commerce_weight,
    ) == (0.0, 0.0, 0.0)
    profile = profile_from_dict(
        "x",
        {
            "diversity_weight": 0.25,
            "perspective_weight": "1",
            "commerce_weight": 1.5,
        },
    )
    assert (
        profile.diversity_weight,
        profile.perspective_weight,
        profile.commerce_weight,
    ) == (0.25, 1.0, 1.5)


def test_new_weight_keys_validated() -> None:
    for key in ("diversity_weight", "perspective_weight", "commerce_weight"):
        with pytest.raises(ConfigError):
            profile_from_dict("x", {key: "heavy"})
        with pytest.raises(ConfigError):
            profile_from_dict("x", {key: float("nan")})
        with pytest.raises(ConfigError):
            profile_from_dict("x", {key: float("inf")})


def test_unknown_key_still_raises() -> None:
    with pytest.raises(ConfigError):
        profile_from_dict("x", {"diversity_boost": 1.0})


def test_yaml_and_builtins_carry_identical_rerank_weights() -> None:
    loaded = load_profiles("configs/ranking.yaml")  # repo-relative
    assert set(loaded) == set(BUILTIN_PROFILES)
    assert loaded == BUILTIN_PROFILES


# --- Composite: neutral-absent stability -------------------------------------


def test_absent_rerank_signals_keep_existing_orderings() -> None:
    for mode in ("default", "child-safe", "news", "shopping", "travel"):
        ranker = Ranker()
        profile = get_profile(mode)
        first = _hit("first", "Alpha", original=2.0)
        second = _hit("second", "Beta", original=1.0)
        # No signals at all: the rerank terms are a constant offset that
        # cancels out of every pairwise comparison.
        assert ranker.composite(profile, first) - ranker.composite(
            profile, second
        ) == pytest.approx(2.0 - 1.0)
        ctx = _ctx([second, first], mode=mode)
        ranker.run(ctx)
        assert [r.id for r in ctx.results] == ["first", "second"]


def test_missing_signal_keys_default_to_neutral_without_keyerror() -> None:
    ranker = Ranker()
    profile = get_profile("shopping")
    hit = _hit("a", "Alpha", original=1.0)
    hit.metadata["signals"] = {"trust": 1.0}
    expected = (
        profile.relevance_weight * 1.0
        + 0.5
        * (
            profile.trust_weight
            + profile.freshness_weight
            + profile.preference_weight
            + profile.diversity_weight
            + profile.perspective_weight
            + profile.commerce_weight
        )
        + 0.5 * profile.trust_weight  # trust=1.0 adds half a weight more
    )
    assert ranker.composite(profile, hit) == pytest.approx(expected)


# --- Composite: signals move ordering per weights -----------------------------


def test_commerce_signal_reorders_shopping_but_not_default() -> None:
    pricey = _hit("pricey", "Gadget", original=2.0)
    pricey.metadata["signals"] = {"commerce": 0.0}
    budget = _hit("budget", "Gadget", original=1.0)
    budget.metadata["signals"] = {"commerce": 1.0}
    ranker = Ranker()
    assert ranker.composite(get_profile("shopping"), budget) > ranker.composite(
        get_profile("shopping"), pricey
    )
    # Default mode carries no commerce weight: relevance still decides.
    assert ranker.composite(get_profile("default"), budget) < ranker.composite(
        get_profile("default"), pricey
    )


def test_perspective_signal_reorders_news_but_not_default() -> None:
    plain = _hit("plain", "Alpha", original=2.0)
    plain.metadata["signals"] = {"perspective": 0.0}
    grouped = _hit("grouped", "Alpha", original=1.5)
    grouped.metadata["signals"] = {"perspective": 1.0}
    ranker = Ranker()
    assert ranker.composite(get_profile("news"), grouped) > ranker.composite(
        get_profile("news"), plain
    )
    assert ranker.composite(get_profile("default"), grouped) < ranker.composite(
        get_profile("default"), plain
    )


def test_diversity_signal_demotes_repeat_domain() -> None:
    ranker = Ranker()
    first = _hit("first", "Alpha", original=1.0)
    first.metadata["signals"] = {"diversity": 1.0}
    repeat = _hit("repeat", "Alpha", original=1.0)
    repeat.metadata["signals"] = {"diversity": 0.5}
    for mode in ("default", "child-safe", "news", "shopping", "travel"):
        profile = get_profile(mode)
        assert ranker.composite(profile, first) > ranker.composite(profile, repeat)


# --- Reranker reasons: at most one per reranker per result --------------------


def test_each_reranker_cites_at_most_one_reason_per_result() -> None:
    hits = [
        _hit("a", "City council approves new park budget", url="https://a.test/x"),
        _hit("b", "City council approves new park budget plan", url="https://a.test/y"),
        _hit("c", "Budget camera deal review", url="https://shop.test/z"),
    ]
    for plugin in (DiversityReranker(), NewsReranker(), CommerceReranker()):
        ctx = _ctx(
            [_hit(h.id, h.title, h.snippet, h.original_score, h.url) for h in hits],
            mode="news",
            filters={"max_price": 50},
        )
        before = [len(r.reasons) for r in ctx.results]
        plugin.run(ctx)
        added = [len(r.reasons) - n for r, n in zip(ctx.results, before)]
        assert all(n <= 1 for n in added), plugin.name


def test_commerce_budget_reorder_carries_cited_reason() -> None:
    over = _hit("over", "Camera deal", original=2.0)
    over.metadata["price"] = 500.0
    over.metadata["currency"] = "USD"
    within = _hit("within", "Camera deal", original=1.0)
    within.metadata["price"] = 20.0
    within.metadata["currency"] = "USD"
    ctx = _ctx(
        [over, within],
        mode="shopping",
        query="camera",
        filters={"max_price": {"value": 50, "currency": "USD"}},
    )
    CommerceReranker().run(ctx)
    Ranker().run(ctx)
    assert [r.id for r in ctx.results] == ["within", "over"]
    assert any("within your budget" in reason for reason in ctx.results[0].reasons)


# --- Determinism ----------------------------------------------------------------


def test_repeated_runs_produce_identical_order() -> None:
    def _build() -> SearchContext:
        return _ctx(
            [
                _hit(
                    "a", "City council approves new park budget", url="https://a.test/1"
                ),
                _hit(
                    "b",
                    "City council approves new park budget plan",
                    url="https://a.test/2",
                ),
                _hit(
                    "c", "Camera deal review", original=0.5, url="https://shop.test/3"
                ),
            ],
            mode="shopping",
            query="camera",
            filters={"max_price": 50},
        )

    orders = []
    for _ in range(3):
        ctx = _build()
        DiversityReranker().run(ctx)
        NewsReranker().run(ctx)
        CommerceReranker().run(ctx)
        Ranker().run(ctx)
        orders.append([r.id for r in ctx.results])
    assert orders[0] == orders[1] == orders[2]

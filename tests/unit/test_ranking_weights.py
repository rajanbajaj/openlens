"""Ranking weights: migration + composite + profiles/config + wiring (plan step 5).

Accept: the baseline scorer emits a `relevance` signal with identical bonus
math, the ranker composite adds the trust/freshness/preference signal terms
with per-mode weights, bad profile values fail loudly at startup, the YAML
and builtins stay in parity, a tmp-path ranking.yaml changes live ordering,
and pre-existing ranking behavior is unchanged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from openlens.core.context import SearchContext
from openlens.core.engine import SearchEngine
from openlens.core.interfaces import (
    ConfigError,
    SearchProvider,
    SearchRequest,
    SearchResult,
)
from openlens.core.registry import PluginRegistry
from openlens.main import load_ranking_profiles
from openlens.plugins.relevance_scorer import BaselineRelevanceScorer
from openlens.ranking.profiles import (
    BUILTIN_PROFILES,
    get_profile,
    load_profiles,
    profile_from_dict,
)
from openlens.ranking.ranker import Ranker


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


# --- Baseline migration: identical math + relevance signal -----------------


def test_relevance_signal_matches_final_score_math() -> None:
    scorer = BaselineRelevanceScorer()
    ctx = SearchContext(request=SearchRequest(query="kittens"))
    ctx.results = [
        _hit("a", "Kittens care guide", "kittens kittens play", original=1.0),
        _hit("b", "Unrelated news roundup", original=2.0),
    ]
    scorer.run(ctx)
    by_id = {r.id: r for r in ctx.results}
    # Hand-computed: title has 1x "kittens", snippet 2x; weights 2.0/1.0.
    assert by_id["a"].final_score == pytest.approx(1.0 + 2.0 * 1 + 1.0 * 2)
    assert by_id["a"].metadata["signals"]["relevance"] == pytest.approx(1.0)
    assert by_id["b"].final_score == pytest.approx(2.0)
    assert by_id["b"].metadata["signals"]["relevance"] == pytest.approx(0.0)
    assert by_id["a"].reasons  # one concise reason per result


def test_relevance_math_preserves_duplicate_query_terms() -> None:
    scorer = BaselineRelevanceScorer()
    bonus, matched = scorer.score_hit(["kittens", "kittens"], "Kittens care", "")
    # Each query-term occurrence counts (multiplicities preserved).
    assert bonus == pytest.approx(2.0 * 2)
    assert matched == 2


def test_relevance_signal_clamped_but_bonus_exact() -> None:
    scorer = BaselineRelevanceScorer()
    ctx = SearchContext(request=SearchRequest(query="kittens kittens kittens"))
    ctx.results = [_hit("a", "kittens kittens kittens", "kittens", original=0.5)]
    scorer.run(ctx)
    hit = ctx.results[0]
    assert hit.final_score == pytest.approx(0.5 + 2.0 * 9 + 1.0 * 3)
    assert hit.metadata["signals"]["relevance"] == pytest.approx(1.0)


# --- Composite: new signal terms --------------------------------------------


def test_missing_signals_default_to_neutral_without_keyerror() -> None:
    ranker = Ranker()
    profile = get_profile("default")
    score = ranker.composite(profile, _hit("a", "Alpha", original=1.0))
    expected_offset = 0.5 * (
        profile.trust_weight
        + profile.freshness_weight
        + profile.preference_weight
        + profile.diversity_weight
        + profile.perspective_weight
        + profile.commerce_weight
    )
    assert score == pytest.approx(profile.relevance_weight * 1.0 + expected_offset)


def test_signal_terms_move_ordering_per_weights() -> None:
    ranker = Ranker()
    profile = get_profile("news")
    low = _hit("low", "Alpha", original=2.0)
    low.metadata["signals"] = {"trust": 0.0, "freshness": 0.0, "preference": 0.0}
    high = _hit("high", "Beta", original=1.0)
    high.metadata["signals"] = {"trust": 1.0, "freshness": 1.0, "preference": 1.0}
    assert ranker.composite(profile, high) > ranker.composite(profile, low)


def test_legacy_boost_terms_still_apply() -> None:
    ranker = Ranker()
    profile = get_profile("child-safe")
    warned = _hit("warned", "Kittens guide", original=5.0)
    warned.warnings.append("untrusted domain")
    clean = _hit("clean", "Kittens guide", original=1.0)
    assert ranker.composite(profile, clean) > ranker.composite(profile, warned)


def test_recency_backfill_key_still_read() -> None:
    ranker = Ranker()
    profile = get_profile("news")
    fresh = _hit("fresh", "Alpha", original=1.0)
    fresh.metadata["recency_score"] = 1.0
    stale = _hit("stale", "Alpha", original=1.0)
    stale.metadata["recency_score"] = 0.0
    assert ranker.composite(profile, fresh) > ranker.composite(profile, stale)


def test_default_and_news_weights_differ() -> None:
    default = get_profile("default")
    news = get_profile("news")
    assert (
        default.trust_weight,
        default.freshness_weight,
        default.preference_weight,
    ) == (
        0.5,
        0.25,
        1.0,
    )
    assert (news.trust_weight, news.freshness_weight, news.preference_weight) == (
        0.5,
        2.0,
        0.0,
    )


def test_pinned_weights_for_all_modes() -> None:
    assert (
        get_profile("child-safe").trust_weight,
        get_profile("child-safe").freshness_weight,
        get_profile("child-safe").preference_weight,
    ) == (1.0, 0.0, 0.0)
    for mode in ("shopping", "travel"):
        profile = get_profile(mode)
        assert (
            profile.trust_weight,
            profile.freshness_weight,
            profile.preference_weight,
        ) == (0.25, 0.0, 1.0)


# --- Profiles: validation ----------------------------------------------------


def test_unknown_profile_key_still_raises() -> None:
    with pytest.raises(ConfigError):
        profile_from_dict("oops", {"bogus_key": 1.0})


def test_new_weight_keys_accepted_and_validated() -> None:
    profile = profile_from_dict(
        "x",
        {"trust_weight": 0.5, "freshness_weight": "2", "preference_weight": 1},
    )
    assert (profile.trust_weight, profile.freshness_weight) == (0.5, 2.0)
    with pytest.raises(ConfigError):
        profile_from_dict("x", {"trust_weight": "heavy"})
    with pytest.raises(ConfigError):
        profile_from_dict("x", {"freshness_weight": float("nan")})
    with pytest.raises(ConfigError):
        profile_from_dict("x", {"preference_weight": float("inf")})


def test_keywords_string_form_raises() -> None:
    with pytest.raises(ConfigError):
        profile_from_dict("x", {"keywords": "buy, price"})


def test_keywords_non_string_entries_raise() -> None:
    with pytest.raises(ConfigError):
        profile_from_dict("x", {"keywords": ["buy", 42]})
    with pytest.raises(ConfigError):
        profile_from_dict("x", {"keywords": None})


def test_keywords_over_cap_raise() -> None:
    with pytest.raises(ConfigError):
        profile_from_dict("x", {"keywords": [f"kw{i}" for i in range(51)]})
    profile = profile_from_dict("x", {"keywords": [f"kw{i}" for i in range(50)]})
    assert len(profile.keywords) == 50


def test_keywords_stored_pre_lowered() -> None:
    profile = profile_from_dict("x", {"keywords": ["Buy", "PRICE"]})
    assert profile.keywords == ("buy", "price")


# --- Parity: YAML vs builtins -------------------------------------------------


def test_yaml_and_builtins_carry_identical_weights() -> None:
    loaded = load_profiles("configs/ranking.yaml")  # repo-relative
    assert set(loaded) == set(BUILTIN_PROFILES)
    assert loaded == BUILTIN_PROFILES


# --- Wiring: ranker injection + engine profiles + registry --------------------


def test_ranker_accepts_injected_profiles() -> None:
    custom = profile_from_dict("custom", {"freshness_weight": 9.0})
    ranker = Ranker({"profile": "custom"}, profiles={"custom": custom})
    hit = _hit("a", "Alpha", original=1.0)
    hit.metadata["signals"] = {"freshness": 1.0}
    # Missing trust/preference keys degrade to neutral 0.5 with zero weights.
    assert ranker.composite(custom, hit) == pytest.approx(1.0 + 9.0 * 1.0)
    with pytest.raises(ConfigError):
        Ranker({"profile": "nope"}, profiles={"custom": custom})


def test_ranker_unknown_mode_still_fails_loudly() -> None:
    ctx = SearchContext(request=SearchRequest(query="q", mode="nope"))
    ctx.results = [_hit("a", "Alpha")]
    with pytest.raises(ConfigError):
        Ranker().run(ctx)


def test_engine_configure_accepts_raw_and_built_profiles() -> None:
    engine = SearchEngine(providers=[], plugins=[])
    engine.configure(
        {
            "plugins": [],
            "ranking": {
                "default_profile": "custom",
                "profiles": {"custom": {"freshness_weight": 3.0}},
            },
        }
    )
    engine2 = SearchEngine(providers=[], plugins=[])
    engine2.configure(
        {
            "plugins": [],
            "ranking": {
                "default_profile": "custom",
                "profiles": {"custom": profile_from_dict("custom", {})},
            },
        }
    )


def test_engine_configure_rejects_bad_profiles() -> None:
    engine = SearchEngine(providers=[], plugins=[])
    with pytest.raises(ConfigError):
        engine.configure({"plugins": [], "ranking": {"profiles": ["not-a-mapping"]}})
    with pytest.raises(ConfigError):
        engine.configure(
            {
                "plugins": [],
                "ranking": {"profiles": {"custom": "nope"}},
            }
        )
    with pytest.raises(ConfigError):
        engine.configure(
            {
                "plugins": [],
                "ranking": {
                    "default_profile": "nope",
                    "profiles": {"custom": {}},
                },
            }
        )


def test_registry_resolves_new_scorers() -> None:
    registry = PluginRegistry()
    assert registry.resolve("trust-scorer").__name__ == "TrustScorer"
    assert registry.resolve("freshness-scorer").__name__ == "FreshnessScorer"
    assert registry.resolve("preference-scorer").__name__ == "PreferenceScorer"
    built = registry.build(
        [
            {"name": "trust-scorer", "config": {}},
            {"name": "freshness-scorer", "config": {}},
            {"name": "preference-scorer", "config": {}},
        ]
    )
    assert [p.name for p in built] == [
        "trust-scorer",
        "freshness-scorer",
        "preference-scorer",
    ]


def test_load_ranking_profiles_missing_file_returns_none(
    tmp_path: Path,
) -> None:
    assert load_ranking_profiles([tmp_path / "absent.yaml"]) is None


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


def _dated_hit(id: str, original: float, published: str) -> SearchResult:
    hit = _hit(id, "Kittens guide", "kittens", original=original)
    hit.metadata["published_at"] = published
    return hit


def test_tmp_yaml_changed_weight_changes_live_ordering(tmp_path: Path) -> None:
    now = "2026-10-28T00:00:00+00:00"
    hits = [
        _dated_hit("old", 2.0, "2024-01-01T00:00:00+00:00"),
        _dated_hit("new", 1.0, "2026-10-27T00:00:00+00:00"),
    ]
    base = {
        "default_profile": "default",
        "profiles": {
            "default": {
                "description": "tmp",
                "relevance_weight": 1.0,
                "warning_penalty": 0.0,
                "trust_weight": 0.0,
                "freshness_weight": 0.0,
                "preference_weight": 0.0,
            }
        },
    }
    tmp = tmp_path / "ranking.yaml"
    tmp.write_text(yaml.safe_dump(base), encoding="utf-8")

    def _order_for(weight: float) -> list[str]:
        data = yaml.safe_load(tmp.read_text(encoding="utf-8"))
        data["profiles"]["default"]["freshness_weight"] = weight
        tmp.write_text(yaml.safe_dump(data), encoding="utf-8")
        profiles = load_profiles(tmp)
        engine = SearchEngine(providers=[_FakeProvider(hits)], plugins=[])
        engine.configure(
            {
                "plugins": [
                    {
                        "name": "freshness-scorer",
                        "config": {"half_life_days": 30, "now": now},
                    }
                ],
                "ranking": {"default_profile": "default", "profiles": profiles},
            }
        )
        resp = engine.search(SearchRequest(query="kittens", mode="default"))
        return [r.id for r in resp.results]

    assert _order_for(0.0) == ["old", "new"]
    assert _order_for(5.0) == ["new", "old"]


def test_default_vs_news_reorders_with_freshness() -> None:
    now = "2026-10-28T00:00:00+00:00"
    hits = [
        _dated_hit("old", 2.0, "2024-01-01T00:00:00+00:00"),
        _dated_hit("new", 1.0, "2026-10-27T00:00:00+00:00"),
    ]
    engine = SearchEngine(providers=[_FakeProvider(hits)], plugins=[])
    engine.configure(
        {
            "plugins": [
                {
                    "name": "freshness-scorer",
                    "config": {"half_life_days": 30, "now": now},
                }
            ],
            "ranking": {"default_profile": "default"},
        }
    )
    default_ids = [
        r.id
        for r in engine.search(SearchRequest(query="kittens", mode="default")).results
    ]
    news_ids = [
        r.id for r in engine.search(SearchRequest(query="kittens", mode="news")).results
    ]
    assert default_ids == ["old", "new"]
    assert news_ids == ["new", "old"]

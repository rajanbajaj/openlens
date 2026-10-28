"""Unit tests for the trust scoring pack step.

Covers the TrustScorer tiers/bonuses/neutral handling and the tier-aware
TrustEnricher extension, including legacy config compatibility.
"""

from __future__ import annotations

from typing import Any

import pytest

from openlens.core.context import SearchContext
from openlens.core.interfaces import (
    ConfigError,
    PipelineStage,
    PluginType,
    SearchRequest,
    SearchResult,
)
from openlens.plugins._signals import get_signal, set_signal
from openlens.plugins.trust_enricher import TrustEnricher
from openlens.plugins.trust_scorer import SIGNAL_NAME, TrustScorer

REPUTATION: dict[str, Any] = {
    "reputation": {"trusted": ["Safe.Example"], "distrusted": ["shady.example"]}
}


def _hit(
    hit_id: str = "h",
    url: str | None = "http://unknown.example/x",
    metadata: dict[str, Any] | None = None,
) -> SearchResult:
    return SearchResult(
        id=hit_id,
        title="a title",
        snippet="a snippet",
        original_score=1.0,
        final_score=1.0,
        url=url,
        provider="test",
        metadata=dict(metadata or {}),
    )


def _ctx(*hits: SearchResult) -> SearchContext:
    ctx = SearchContext(request=SearchRequest(query="q"))
    ctx.results = list(hits)
    return ctx


def _signal(result: SearchResult) -> float:
    return get_signal(result, SIGNAL_NAME)


def _run_scorer(*hits: SearchResult, config: dict[str, Any] | None = None) -> list:
    out = TrustScorer(dict(REPUTATION) if config is None else config).run(_ctx(*hits))
    return out.results


# --- Scorer contract -------------------------------------------------------


def test_scorer_plugin_contract():
    plugin = TrustScorer(REPUTATION)
    assert plugin.name == "trust-scorer"
    assert plugin.stage == PipelineStage.SCORING
    assert plugin.plugin_type == PluginType.SCORER
    assert plugin.mandatory is False


# --- Tiers -----------------------------------------------------------------


def test_trust_tiers_ordered():
    results = _run_scorer(
        _hit("t", url="http://safe.example/a"),
        _hit("u", url="http://unknown.example/a"),
        _hit("d", url="http://shady.example/a"),
    )
    by_id = {r.id: r for r in results}
    assert _signal(by_id["t"]) == pytest.approx(0.8)
    assert _signal(by_id["u"]) == pytest.approx(0.5)
    assert _signal(by_id["d"]) == pytest.approx(0.2)
    assert _signal(by_id["t"]) > _signal(by_id["u"]) > _signal(by_id["d"])


def test_tier_matching_is_case_insensitive():
    results = _run_scorer(_hit("t", url="http://SAFE.EXAMPLE/a"))
    assert _signal(results[0]) == pytest.approx(0.8)


# --- Bonuses ---------------------------------------------------------------


def test_https_bonus():
    plain = _run_scorer(_hit("u", url="http://unknown.example/a"))[0]
    secure = _run_scorer(_hit("u", url="https://unknown.example/a"))[0]
    assert _signal(plain) == pytest.approx(0.5)
    assert _signal(secure) == pytest.approx(0.55)


def test_trusted_https_combined():
    results = _run_scorer(_hit("t", url="https://safe.example/a"))
    assert _signal(results[0]) == pytest.approx(0.85)


def test_domain_age_present_vs_absent():
    without = _run_scorer(_hit("u", url="http://unknown.example/a"))[0]
    with_age = _run_scorer(
        _hit("u", url="http://unknown.example/a", metadata={"domain_age_days": 400})
    )[0]
    assert _signal(without) == pytest.approx(0.5)
    assert _signal(with_age) == pytest.approx(0.55)


@pytest.mark.parametrize("bad_age", ["400", -1, 2.5, True, None])
def test_domain_age_invalid_types_ignored(bad_age: Any):
    results = _run_scorer(
        _hit(
            "u",
            url="http://unknown.example/a",
            metadata={"domain_age_days": bad_age},
        )
    )
    assert _signal(results[0]) == pytest.approx(0.5)


# --- Neutral handling ------------------------------------------------------


@pytest.mark.parametrize("url", [None, ""])
def test_missing_url_neutral_with_reason(url: str | None):
    results = _run_scorer(_hit("m", url=url))
    assert _signal(results[0]) == pytest.approx(0.5)
    assert any("neutral" in reason for reason in results[0].reasons)


def test_signal_helpers_clamp_and_default():
    hit = _hit("h")
    assert set_signal(hit, SIGNAL_NAME, 1.5) == pytest.approx(1.0)
    assert set_signal(hit, SIGNAL_NAME, -0.5) == pytest.approx(0.0)
    assert get_signal(_hit("fresh"), SIGNAL_NAME) == pytest.approx(0.5)


# --- Startup validation ----------------------------------------------------


def test_domain_in_both_tiers_rejected():
    both = {"reputation": {"trusted": ["x.example"], "distrusted": ["x.example"]}}
    with pytest.raises(ConfigError):
        TrustScorer(both)
    with pytest.raises(ConfigError):
        TrustEnricher(both)


def test_enricher_legacy_overlap_with_distrusted_rejected():
    with pytest.raises(ConfigError):
        TrustEnricher(
            {
                "trusted_domains": ["x.example"],
                "reputation": {"distrusted": ["x.example"]},
            }
        )


@pytest.mark.parametrize(
    "config",
    [
        {"reputation": ["safe.example"]},
        {"reputation": {"trusted": "safe.example"}},
        {"reputation": {"distrusted": [123]}},
        {"trusted_domains": "safe.example"},
    ],
)
def test_malformed_reputation_rejected(config: dict[str, Any]):
    with pytest.raises(ConfigError):
        TrustScorer(config)
    with pytest.raises(ConfigError):
        TrustEnricher(config)


def test_lookup_structures_are_o1():
    scorer = TrustScorer(REPUTATION)
    enricher = TrustEnricher(REPUTATION)
    assert isinstance(scorer._tiers, dict)
    assert isinstance(enricher._trusted_domains, frozenset)
    assert isinstance(enricher._distrusted_domains, frozenset)
    assert isinstance(enricher._tiers, dict)


# --- No duplicate warnings -------------------------------------------------


def test_scorer_never_warns_and_appends_one_reason():
    results = _run_scorer(
        _hit("t", url="https://safe.example/a"),
        _hit("d", url="http://shady.example/a"),
        _hit("m", url=None),
    )
    for result in results:
        assert result.warnings == []
        trust_reasons = [r for r in result.reasons if "trust" in r]
        assert len(trust_reasons) == 1


def test_no_duplicate_warnings_with_enricher():
    hits = [
        _hit("t", url="https://safe.example/a"),
        _hit("u", url="https://unknown.example/a"),
        _hit("d", url="http://shady.example/a"),
    ]
    ctx = _ctx(*hits)
    TrustScorer(REPUTATION).run(ctx)
    TrustEnricher(REPUTATION).run(ctx)
    by_id = {r.id: r for r in ctx.results}
    assert by_id["t"].warnings == []
    assert len(by_id["u"].warnings) == 1
    assert len(by_id["d"].warnings) == 1


# --- Enricher tiers --------------------------------------------------------


def test_enricher_tier_warnings_and_metadata():
    ctx = _ctx(
        _hit("t", url="https://safe.example/a"),
        _hit("d", url="https://shady.example/a"),
        _hit("u", url="https://unknown.example/a"),
        _hit("n", url=None),
    )
    TrustEnricher(REPUTATION).run(ctx)
    by_id = {r.id: r for r in ctx.results}
    assert by_id["t"].warnings == []
    assert by_id["t"].metadata["trusted"] is True
    assert by_id["t"].metadata["trust_tier"] == "trusted"
    assert any("Distrusted" in w for w in by_id["d"].warnings)
    assert by_id["d"].metadata["trusted"] is False
    assert by_id["u"].warnings  # legacy unverified-source warning kept
    assert by_id["n"].warnings == []  # legacy: no URL means no warning


def test_enricher_no_https_warnings():
    ctx = _ctx(
        _hit("u", url="http://unknown.example/a"),
        _hit("s", url="https://unknown.example/a"),
        _hit("t", url="http://safe.example/a"),
    )
    TrustEnricher(REPUTATION).run(ctx)
    by_id = {r.id: r for r in ctx.results}
    assert any("HTTPS" in w for w in by_id["u"].warnings)
    assert not any("HTTPS" in w for w in by_id["s"].warnings)
    assert any("HTTPS" in w for w in by_id["t"].warnings)


# --- Legacy config ---------------------------------------------------------


def test_scorer_honors_legacy_trusted_domains():
    results = _run_scorer(
        _hit("t", url="http://safe.example/a"),
        config={"trusted_domains": ["safe.example"]},
    )
    assert _signal(results[0]) == pytest.approx(0.8)


def test_legacy_trusted_domains_config_keeps_working():
    enricher = TrustEnricher({"trusted_domains": ["safe.example"]})
    assert isinstance(enricher._trusted_domains, frozenset)
    ctx = _ctx(
        _hit("t", url="https://safe.example/a"),
        _hit("u", url="https://other.example/a"),
    )
    enricher.run(ctx)
    by_id = {r.id: r for r in ctx.results}
    assert by_id["t"].metadata["trusted"] is True
    assert by_id["t"].warnings == []
    assert by_id["u"].metadata["trusted"] is False
    assert by_id["u"].warnings

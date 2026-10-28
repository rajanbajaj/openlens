"""Diversity reranker tests (rerank-pack plan Step 2)."""

from __future__ import annotations

import pytest

from openlens.core.context import SearchContext
from openlens.core.interfaces import (
    PipelineStage,
    PluginType,
    SearchRequest,
    SearchResult,
)
from openlens.plugins._signals import get_signal
from openlens.plugins.diversity_reranker import (
    DIVERSITY_SIGNAL,
    DiversityReranker,
    compute_diversity_signals,
)


def _hit(hit_id: str, url: str | None = None) -> SearchResult:
    return SearchResult(
        id=hit_id,
        title=f"title {hit_id}",
        snippet="snippet",
        original_score=1.0,
        final_score=1.0,
        url=url,
        provider="test",
        metadata={},
    )


def _context(results: list[SearchResult]) -> SearchContext:
    ctx = SearchContext(request=SearchRequest(query="q"))
    ctx.results = list(results)
    return ctx


def _six_hit_urls() -> list[str | None]:
    return [
        "https://a.com/1",
        "https://a.com/2",
        "https://b.com/1",
        "https://a.com/3",
        "https://b.com/2",
        "https://c.com/1",
    ]


def _run(urls: list[str | None]) -> list[SearchResult]:
    results = [_hit(f"h{i + 1}", url) for i, url in enumerate(urls)]
    ctx = _context(results)
    DiversityReranker({}).run(ctx)
    return ctx.results


def test_exact_signal_values_on_six_hit_list() -> None:
    results = _run(_six_hit_urls())
    expected = [1.0, 0.5, 1.0, 1.0 / 3.0, 0.5, 1.0]
    assert len(results) == len(expected)
    for result, want in zip(results, expected):
        assert get_signal(result, DIVERSITY_SIGNAL) == pytest.approx(want)
        assert result.metadata["signals"][DIVERSITY_SIGNAL] == pytest.approx(want)


def test_pure_function_matches_plugin_math() -> None:
    assert compute_diversity_signals(["a.com", "a.com", "b.com", "a.com"]) == (
        pytest.approx([1.0, 0.5, 1.0, 1.0 / 3.0])
    )


def test_rank_1_never_demoted_by_diversity_alone() -> None:
    results = _run(["https://a.com/1", "https://a.com/2", "https://a.com/3"])
    assert get_signal(results[0], DIVERSITY_SIGNAL) == 1.0
    assert get_signal(results[1], DIVERSITY_SIGNAL) == pytest.approx(0.5)
    assert get_signal(results[2], DIVERSITY_SIGNAL) == pytest.approx(1.0 / 3.0)


def test_no_hit_removed_and_order_untouched() -> None:
    urls = _six_hit_urls()
    results = _run(urls)
    assert [r.id for r in results] == [f"h{i + 1}" for i in range(len(urls))]
    assert [r.url for r in results] == urls


def test_domains_compared_case_and_scheme_insensitively() -> None:
    results = _run(["HTTP://Example.COM/a", "https://example.com:8443/b"])
    assert get_signal(results[0], DIVERSITY_SIGNAL) == 1.0
    assert get_signal(results[1], DIVERSITY_SIGNAL) == pytest.approx(0.5)


def test_missing_urls_share_neutral_key_without_removal() -> None:
    results = _run([None, None, "https://a.com/1"])
    assert get_signal(results[0], DIVERSITY_SIGNAL) == 1.0
    assert get_signal(results[1], DIVERSITY_SIGNAL) == pytest.approx(0.5)
    assert get_signal(results[2], DIVERSITY_SIGNAL) == 1.0
    assert len(results) == 3


def test_plugin_contract() -> None:
    plugin = DiversityReranker({})
    assert plugin.name == "diversity-reranker"
    assert plugin.plugin_type == PluginType.SCORER
    assert plugin.stage == PipelineStage.EXPLANATION
    assert plugin.mandatory is False

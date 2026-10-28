"""Rerank-pack integration (plan step 5): Goal through the full engine.

Drives hand-built hits (carrying price/published/perspective metadata the
shipped fixture corpus lacks) through attribute enricher -> rerankers ->
engine ``Ranker``: a news query returns grouped stories with a
credibility-weighted perspective mix, a shopping query with a budget
filter ranks in-budget hits first with cited reasons, and repeated runs
produce identical ``context.results`` id order. The shipped fixture
corpus is used only for a determinism/diversity smoke check.

Never puts ``"ranker"`` in the pipeline: the engine owns the single rank.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from openlens.core.context import SearchContext
from openlens.core.engine import SearchEngine
from openlens.core.interfaces import (
    SearchProvider,
    SearchRequest,
    SearchResult,
)
from openlens.core.pipeline import Pipeline
from openlens.core.registry import BUILTIN_PLUGINS, PluginRegistry
from openlens.plugins.attribute_enricher import AttributeEnricher
from openlens.plugins.commerce_reranker import CommerceReranker
from openlens.plugins.diversity_reranker import DiversityReranker
from openlens.plugins.news_reranker import NewsReranker
from openlens.ranking.ranker import Ranker
from tests.fixtures.corpus import fixture_documents

RERANK_PACK = (
    "attribute-enricher",
    "diversity-reranker",
    "news-reranker",
    "commerce-reranker",
)


def _hit(
    id: str,
    title: str,
    snippet: str = "",
    original: float = 1.0,
    url: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> SearchResult:
    return SearchResult(
        id=id,
        title=title,
        snippet=snippet,
        original_score=original,
        final_score=original,
        url=url,
        metadata=dict(metadata or {}),
    )


class FakeProvider(SearchProvider):
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


def _news_hits() -> list[SearchResult]:
    """Three near-duplicate same-day stories + outlier + other-day near-dup."""
    return [
        _hit(
            "n1",
            "City council approves downtown stadium plan",
            "Council backs the stadium plan.",
            url="https://left.example.com/stadium-plan",
            metadata={
                "published": "2026-10-20",
                "perspective": "Left",
                "trusted": True,
            },
        ),
        _hit(
            "n2",
            "City council approves downtown stadium funding",
            "Council backs stadium funding.",
            url="https://center.example.com/stadium-funding",
            metadata={
                "published": "2026-10-20",
                "perspective": "center",
                "trusted": True,
            },
        ),
        _hit(
            "n3",
            "City council approves downtown stadium project",
            "Council backs the stadium project.",
            url="https://right.example.com/stadium-project",
            metadata={
                "published": "2026-10-20",
                "perspective": "right",
                "trusted": False,
            },
        ),
        _hit(
            "n4",
            "Mayor unveils riverfront park renovation",
            "A park project breaks ground.",
            url="https://rumor.example.net/park-claims",
            metadata={
                "published": "2026-10-20",
                "perspective": "left",
                "trusted": False,
            },
        ),
        _hit(
            "n5",
            "City council approves downtown stadium plan",
            "Next-day follow-up on the stadium plan.",
            url="https://center.example.com/stadium-next-day",
            metadata={
                "published": "2026-10-21",
                "perspective": "center",
                "trusted": True,
            },
        ),
    ]


def _shopping_hits() -> list[SearchResult]:
    """Over-budget provider favorite vs in-budget hit vs metadata-less hit."""
    return [
        _hit(
            "c1",
            "Blue cotton dress buy now",
            "Comfortable fit in many sizes.",
            original=1.0,
            url="https://shop.example.com/blue-dress",
            metadata={"price": "30", "currency": "usd"},
        ),
        _hit(
            "c2",
            "Red silk dress buy now",
            "Comfortable fit in many sizes.",
            original=2.0,
            url="https://shop.example.com/red-dress",
            metadata={"price": 80, "currency": "USD"},
        ),
        _hit(
            "c3",
            "Green linen dress buy now",
            "Comfortable fit in many sizes.",
            original=1.5,
            url="https://other.example.com/green-dress",
        ),
    ]


def _news_engine() -> SearchEngine:
    return SearchEngine(
        providers=[FakeProvider(_news_hits())],
        plugins=[AttributeEnricher(), NewsReranker()],
    )


def _shopping_engine() -> SearchEngine:
    return SearchEngine(
        providers=[FakeProvider(_shopping_hits())],
        plugins=[AttributeEnricher(), CommerceReranker()],
    )


def _run_pipeline(
    hits: list[SearchResult],
    plugins: list[Any],
    mode: str,
    query: str,
    filters: dict[str, Any] | None = None,
) -> SearchContext:
    """Mirror the engine seam directly: pipeline then engine-owned ranker."""
    ctx = SearchContext(
        request=SearchRequest(query=query, mode=mode, filters=filters or {})
    )
    ctx.results = hits
    ctx = Pipeline(plugins).run(ctx)
    return Ranker().run(ctx)


# --- Registration + shipped config -------------------------------------------


def test_rerank_pack_registered() -> None:
    for name in RERANK_PACK:
        assert name in BUILTIN_PLUGINS, f"{name} missing from BUILTIN_PLUGINS"
    registry = PluginRegistry()
    assert registry.resolve("attribute-enricher") is AttributeEnricher
    assert registry.resolve("diversity-reranker") is DiversityReranker
    assert registry.resolve("news-reranker") is NewsReranker
    assert registry.resolve("commerce-reranker") is CommerceReranker


def test_default_yaml_ships_rerank_pack_disabled() -> None:
    path = Path(__file__).resolve().parents[2] / "configs" / "default.yaml"
    config = yaml.safe_load(path.read_text())
    entries = {e["name"]: e for e in config["plugins"]}
    for name in RERANK_PACK:
        assert name in entries, f"{name} missing from configs/default.yaml"
        assert entries[name]["enabled"] is False, f"{name} must ship disabled"


# --- News: grouped stories, credibility-weighted mix --------------------------


def test_news_query_groups_stories_with_credible_mix() -> None:
    resp = _news_engine().search(SearchRequest(query="stadium vote", mode="news"))
    ids = [r.id for r in resp.results]
    # Trusted same-story members outrank the distrusted outlier and the
    # other-day near-dup; ties keep provider order (stable sort).
    assert ids == ["n1", "n2", "n5", "n3", "n4"]
    assert ids.index("n4") > ids.index("n1")

    ctx = _run_pipeline(
        _news_hits(), [AttributeEnricher(), NewsReranker()], "news", "stadium vote"
    )
    groups = ctx.metadata["story_groups"]
    assert set(groups) == {"story-0"}
    assert groups["story-0"]["members"] == ["n1", "n2", "n3"]
    assert groups["story-0"]["perspectives"] == ["center", "left", "right"]
    by_id = {r.id: r.metadata for r in ctx.results}
    assert by_id["n1"]["story_id"] == "story-0"
    assert by_id["n2"]["story_id"] == "story-0"
    assert by_id["n3"]["story_id"] == "story-0"
    assert "story_id" not in by_id["n4"]
    # The other-day near-dup shares bigrams but not the calendar day.
    assert "story_id" not in by_id["n5"]
    signals = {r.id: r.metadata["signals"]["perspective"] for r in ctx.results}
    # Credibility-weighted, never equal: trusted members keep full
    # credibility while the distrusted same-story member is halved.
    assert signals["n1"] == signals["n2"] == 1.0
    assert signals["n3"] == 0.5
    assert signals["n3"] < signals["n1"]


# --- Shopping: budget first with cited reasons --------------------------------


def test_shopping_query_ranks_in_budget_first_with_reasons() -> None:
    resp = _shopping_engine().search(
        SearchRequest(
            query="buy dress under 50",
            mode="shopping",
            filters={"max_price": 50},
        )
    )
    ids = [r.id for r in resp.results]
    # In-budget c1 beats the higher-original over-budget c2 and the
    # metadata-less c3 (keyword fallback scores below a satisfied budget).
    assert ids == ["c1", "c3", "c2"]
    by_id = {r.id: r for r in resp.results}
    assert any("within your budget" in reason for reason in by_id["c1"].reasons), (
        f"c1 reasons cite no budget: {by_id['c1'].reasons}"
    )
    assert by_id["c1"].metadata["signals"]["commerce"] == 1.0
    assert by_id["c2"].metadata["signals"]["commerce"] == 0.0


# --- Determinism --------------------------------------------------------------


def test_repeated_runs_produce_identical_order() -> None:
    for _ in range(3):
        news_ids = [
            r.id
            for r in _news_engine()
            .search(SearchRequest(query="stadium vote", mode="news"))
            .results
        ]
        assert news_ids == ["n1", "n2", "n5", "n3", "n4"]
    for _ in range(3):
        shop_ids = [
            r.id
            for r in _shopping_engine()
            .search(
                SearchRequest(
                    query="buy dress under 50",
                    mode="shopping",
                    filters={"max_price": 50},
                )
            )
            .results
        ]
        assert shop_ids == ["c1", "c3", "c2"]


# --- Fixture-corpus smoke (determinism/diversity only) -------------------------


def test_fixture_corpus_diversity_determinism_smoke() -> None:
    """Shipped corpus has no price/perspective metadata: diversity signals
    and stable order only, nothing removed, repeated runs identical."""

    def _corpus_hits() -> list[SearchResult]:
        return [
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

    first: list[str] | None = None
    for _ in range(3):
        ctx = _run_pipeline(
            _corpus_hits(),
            [AttributeEnricher(), DiversityReranker()],
            "default",
            "running shoes",
        )
        ids = [r.id for r in ctx.results]
        # No hit is ever removed by the diversity pass.
        assert len(ids) == len(fixture_documents())
        if first is None:
            first = ids
        assert ids == first
    assert first is not None
    # Rank-1 keeps the full diversity signal; the repeat-domain shopping
    # hit is demoted but retained.
    ctx = _run_pipeline(
        _corpus_hits(),
        [AttributeEnricher(), DiversityReranker()],
        "default",
        "running shoes",
    )
    signals = {r.id: r.metadata["signals"]["diversity"] for r in ctx.results}
    assert signals[ctx.results[0].id] == 1.0
    assert signals["doc-shopping-2"] == 0.5

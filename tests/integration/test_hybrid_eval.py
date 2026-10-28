"""Hybrid eval: keyword-only vs hybrid recall + determinism (offline).

Promoted from the backend-run probe: seeds CustomIndexProvider with the
fixture corpus, runs a fixed query set, and asserts hybrid recall >=
keyword-only recall per query, 3x determinism, and disabled-no-op
byte-identical order.
"""

from __future__ import annotations

from openlens.core.interfaces import SearchRequest
from openlens.models.manager import ModelManager
from openlens.providers.custom_index import CustomIndexProvider
from tests.fixtures.corpus import fixture_documents

QUERIES = [
    "running shoes",
    "trail shoes",
    "park funding",
    "sqlite fts",
    "python dataclasses",
    "coast trip",
    "wireless headphones",
    "coffee maker",
]

# Relevance: doc ids sharing a content token with the query.
RELEVANT = {
    "running shoes": {
        "doc-shopping-1",
        "doc-shopping-2",
        "seed-doc-custom-index-guide",
    },
    "trail shoes": {"doc-shopping-2"},
    "park funding": {"doc-news-1"},
    "sqlite fts": {"doc-tech-1"},
    "python dataclasses": {"doc-tech-2"},
    "coast trip": {"doc-travel-1"},
    "wireless headphones": set(),
    "coffee maker": set(),
}


def _make_provider(**kwargs: object) -> CustomIndexProvider:
    provider = CustomIndexProvider(db_path=":memory:", **kwargs)  # type: ignore[arg-type]
    provider.configure({"db_path": ":memory:", "seed": False})
    provider.index_documents(fixture_documents())
    return provider


def _ids(provider: CustomIndexProvider, query: str) -> list[str]:
    return [hit.id for hit in provider.search(SearchRequest(query=query, limit=10))]


def _fake_models() -> ModelManager:
    return ModelManager.from_config(
        {"models": {"fake": {"enabled": True, "type": "fake"}}}
    )


def test_hybrid_recall_matches_or_beats_keyword_per_query() -> None:
    keyword = _make_provider()
    hybrid = _make_provider(models=_fake_models(), semantic_enabled=True)
    total_kw = total_hy = 0
    for query in QUERIES:
        rec_kw = len(set(_ids(keyword, query)) & RELEVANT[query])
        rec_hy = len(set(_ids(hybrid, query)) & RELEVANT[query])
        total_kw += rec_kw
        total_hy += rec_hy
        assert rec_hy >= rec_kw, f"hybrid regressed recall for {query!r}"
    assert total_hy >= total_kw


def test_hybrid_order_deterministic_across_runs() -> None:
    hybrid = _make_provider(models=_fake_models(), semantic_enabled=True)
    for query in QUERIES:
        runs = [_ids(hybrid, query) for _ in range(3)]
        assert runs[0] == runs[1] == runs[2], f"nondeterministic order: {query}"


def test_semantic_disabled_order_byte_identical() -> None:
    keyword = _make_provider()
    flagged = _make_provider(semantic_enabled=True)

    def snap(
        provider: CustomIndexProvider, query: str
    ) -> list[tuple[str, str, float, float]]:
        return [
            (h.id, h.title, h.original_score, h.final_score)
            for h in provider.search(SearchRequest(query=query, limit=10))
        ]

    for query in QUERIES:
        assert snap(keyword, query) == snap(flagged, query), f"order drift: {query}"

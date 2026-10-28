"""Hybrid retrieval tests: semantic re-rank + custom-index fan-out/fusion.

Offline only: the semantic signal uses the deterministic ``fake``
model backend (no network, no keys). Covers the plan step 2 done-gate:
disabled-no-op byte-identical order, fused-order determinism across
runs, and semantic-failure fail-open.
"""

from __future__ import annotations

from typing import Any

from openlens.core.interfaces import Document, SearchRequest, SearchResult
from openlens.models.manager import ModelManager
from openlens.providers.custom_index import CustomIndexProvider
from openlens.retrieval.semantic import cosine_similarity, rank_ids_by_semantic


def make_models(**options: Any) -> ModelManager:
    return ModelManager.from_config(
        {"models": {"fake": {"enabled": True, "type": "fake", **options}}}
    )


def make_provider(**kwargs: Any) -> CustomIndexProvider:
    provider = CustomIndexProvider(db_path=":memory:", **kwargs)
    provider.configure({"db_path": ":memory:", "seed": True})
    return provider


def seed_extra(provider: CustomIndexProvider) -> None:
    provider.index_documents(
        [
            Document(
                id="doc-alpha",
                title="alpha release notes",
                content="alpha release notes about the new search engine",
                url="https://example.com/alpha",
            ),
            Document(
                id="doc-beta",
                title="beta testing guide",
                content="beta testing guide for the new search engine",
                url="https://example.com/beta",
            ),
            Document(
                id="doc-gamma",
                title="gamma ray astronomy",
                content="gamma ray astronomy and telescope observations",
                url="https://example.com/gamma",
            ),
        ]
    )


def snapshot(hits: list[SearchResult]) -> list[tuple[Any, ...]]:
    return [
        (
            hit.id,
            hit.title,
            hit.snippet,
            hit.original_score,
            hit.final_score,
            hit.url,
            hit.provider,
            tuple(hit.reasons),
        )
        for hit in hits
    ]


def test_disabled_by_default_matches_plain_order_byte_identical():
    plain = make_provider()
    hybrid_flag_no_models = make_provider(semantic_enabled=True)
    request = SearchRequest(query="search engine", limit=10)
    assert snapshot(plain.search(request)) == snapshot(
        hybrid_flag_no_models.search(request)
    )
    assert [hit.id for hit in plain.search(request)] == [
        hit.id for hit in hybrid_flag_no_models.search(request)
    ]


def test_disabled_explicit_matches_plain_order_byte_identical():
    plain = make_provider()
    disabled = make_provider(models=make_models(), semantic_enabled=False)
    request = SearchRequest(query="python", limit=10)
    assert snapshot(plain.search(request)) == snapshot(
        disabled.search(SearchRequest(query="python", limit=10))
    )


def test_semantic_none_models_returns_empty():
    hits = make_provider().search(SearchRequest(query="python", limit=5))
    assert rank_ids_by_semantic("python", hits, None) == []


def test_semantic_empty_inputs_return_empty():
    models = make_models()
    assert rank_ids_by_semantic("", [], models) == []
    assert rank_ids_by_semantic("   ", [], models) == []
    hits = make_provider().search(SearchRequest(query="python", limit=5))
    assert rank_ids_by_semantic("", hits, models) == []
    assert rank_ids_by_semantic("python", [], models) == []


def test_semantic_orders_all_candidates_by_similarity():
    models = make_models()
    hits = make_provider().search(SearchRequest(query="python", limit=10))
    ordered = rank_ids_by_semantic("python", hits, models)
    assert sorted(ordered) == sorted(hit.id for hit in hits)
    assert len(set(ordered)) == len(ordered)


def test_cosine_similarity_basics():
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == -1.0
    assert cosine_similarity([0.0, 0.0], [1.0, 2.0]) == 0.0
    assert cosine_similarity([], []) == 0.0
    assert cosine_similarity([1.0], [1.0, 2.0]) == 0.0


def test_fused_order_deterministic_across_runs():
    orders: list[list[str]] = []
    for _ in range(3):
        provider = make_provider(models=make_models(), semantic_enabled=True)
        seed_extra(provider)
        hits = provider.search(SearchRequest(query="search engine", limit=10))
        orders.append([hit.id for hit in hits])
    assert orders[0] == orders[1] == orders[2]
    assert orders[0], "expected hits for fused query"


def test_fused_results_carry_rrf_metadata():
    provider = make_provider(models=make_models(), semantic_enabled=True)
    seed_extra(provider)
    hits = provider.search(SearchRequest(query="search engine", limit=10))
    assert hits
    for hit in hits:
        assert "rrf_score" in hit.metadata


def test_semantic_failure_falls_back_to_keyword_order():
    class RaisingModels:
        def embed(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("backend down")

    raising = RaisingModels()
    hits = make_provider().search(SearchRequest(query="python", limit=10))
    assert rank_ids_by_semantic("python", hits, raising) == []

    plain = make_provider()
    failing = make_provider(models=raising, semantic_enabled=True)
    request = SearchRequest(query="python", limit=10)
    assert [hit.id for hit in failing.search(request)] == [
        hit.id for hit in plain.search(SearchRequest(query="python", limit=10))
    ]


def test_semantic_malformed_embedding_falls_back():
    class BadShapeModels:
        def embed(self, texts: list[str]) -> list[list[float]]:
            return [[1.0]]

    bad = BadShapeModels()
    provider = make_provider(models=bad, semantic_enabled=True)
    plain = make_provider()
    request = SearchRequest(query="python", limit=10)
    assert [hit.id for hit in provider.search(request)] == [
        hit.id for hit in plain.search(SearchRequest(query="python", limit=10))
    ]


def test_hybrid_pagination_slices_fused_list():
    provider = make_provider(models=make_models(), semantic_enabled=True)
    seed_extra(provider)
    full = provider.search(SearchRequest(query="search engine", limit=10))
    page = provider.search(SearchRequest(query="search engine", limit=2, offset=1))
    assert [hit.id for hit in page] == [hit.id for hit in full[1:3]]

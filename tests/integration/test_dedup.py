"""Cross-provider dedup: merged result sets must not contain repeats.

Uses two seeded CustomIndexProviders plus synthetic cross-backend overlap
to prove shared deduplication collapses repeats by URL (case and
trailing-slash insensitive) and by id, keeping the first (best) hit.
"""

from __future__ import annotations

import pytest

from openlens.core.interfaces import SearchRequest, SearchResult
from openlens.providers.custom_index import CustomIndexProvider
from openlens.retrieval.deduplicator import dedupe_key, deduplicate
from tests.fixtures.corpus import fixture_documents


@pytest.fixture()
def provider_a() -> CustomIndexProvider:
    provider = CustomIndexProvider()
    provider.configure({"db_path": ":memory:", "seed": False})
    provider.index_documents(fixture_documents())
    return provider


@pytest.fixture()
def provider_b() -> CustomIndexProvider:
    provider = CustomIndexProvider()
    provider.configure({"db_path": ":memory:", "seed": False})
    provider.index_documents(fixture_documents())
    return provider


def _retag(results: list[SearchResult], provider: str) -> list[SearchResult]:
    return [
        SearchResult(
            id=r.id,
            title=r.title,
            snippet=r.snippet,
            original_score=r.original_score,
            final_score=r.final_score,
            url=r.url,
            provider=provider,
            reasons=list(r.reasons),
        )
        for r in results
    ]


def test_cross_provider_dedup_removes_shared_urls(provider_a, provider_b):
    query = SearchRequest(query="shoes", limit=10)
    merged = _retag(provider_a.search(query), "provider-a") + _retag(
        provider_b.search(query), "provider-b"
    )
    assert len(merged) > 1, "[integration/dedup] fixture needs overlapping hits"
    unique = deduplicate(merged)
    keys = [dedupe_key(r) for r in unique]
    assert len(set(keys)) == len(keys), (
        "[retrieval/deduplicator.py] merged cross-provider hits must be unique"
    )
    assert len(unique) < len(merged), (
        "[retrieval/deduplicator.py] shared URLs must collapse to one hit"
    )


def test_cross_provider_dedup_keeps_first_hit():
    first = SearchResult(
        id="a",
        title="A",
        snippet="s",
        original_score=0.9,
        final_score=0.9,
        url="https://example.com/Page",
        provider="provider-a",
    )
    second = SearchResult(
        id="b",
        title="B",
        snippet="s",
        original_score=0.1,
        final_score=0.1,
        url="https://EXAMPLE.com/page/",
        provider="provider-b",
    )
    unique = deduplicate([first, second])
    assert len(unique) == 1, (
        "[retrieval/deduplicator.py] URL match must be case/trailing-slash "
        "insensitive across providers"
    )
    assert unique[0].provider == "provider-a", (
        "[retrieval/deduplicator.py] dedup must keep the first (best) hit"
    )


def test_cross_provider_dedup_falls_back_to_id_without_url():
    first = SearchResult(
        id="same",
        title="A",
        snippet="s",
        original_score=0.5,
        final_score=0.5,
        provider="provider-a",
    )
    second = SearchResult(
        id="same",
        title="B",
        snippet="s",
        original_score=0.4,
        final_score=0.4,
        provider="provider-b",
    )
    assert len(deduplicate([first, second])) == 1, (
        "[retrieval/deduplicator.py] url-less hits must dedupe by id"
    )


def test_cross_provider_dedup_distinct_urls_preserved(provider_a, provider_b):
    query = SearchRequest(query="shoes", limit=10)
    merged = provider_a.search(query) + provider_b.search(query)
    unique = deduplicate(merged)
    urls = {r.url for r in unique if r.url}
    assert len(urls) == len(unique), (
        "[retrieval/deduplicator.py] distinct URLs must all survive dedup"
    )

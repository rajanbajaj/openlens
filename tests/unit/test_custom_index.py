"""Smoke tests for the custom index search core.

Covers the skeleton plan step 3 done-gate: index docs, search returns
normalized results with original_score, pagination works. Offline only.
"""

import pytest

from openlens.core.interfaces import Document, ProviderError, SearchRequest
from openlens.indexing.seed import SEED_DOCUMENTS
from openlens.providers.custom_index import CustomIndexProvider
from openlens.retrieval.deduplicator import deduplicate
from openlens.retrieval.query_parser import parse_query
from openlens.storage.repository import DocumentRepository


def make_provider() -> CustomIndexProvider:
    provider = CustomIndexProvider(db_path=":memory:")
    provider.configure({"db_path": ":memory:", "seed": True})
    return provider


def test_index_docs_seeds_fixture_corpus():
    provider = make_provider()
    assert provider.document_count() == len(SEED_DOCUMENTS)
    assert len(SEED_DOCUMENTS) >= 5


def test_repository_indexes_and_counts():
    repo = DocumentRepository(":memory:")
    repo.add_documents(
        [Document(id="a", title="hello world", content="hello world content")]
    )
    assert repo.count() == 1
    assert repo.get("a") is not None
    assert repo.get("missing") is None


def test_search_returns_normalized_results_with_original_score():
    provider = make_provider()
    results = provider.search(SearchRequest(query="python", limit=10))
    assert results, "expected hits for 'python'"
    for hit in results:
        assert hit.id
        assert hit.title
        assert hit.snippet
        assert isinstance(hit.original_score, float)
        assert hit.final_score == hit.original_score
        assert hit.provider == "custom_index"
        assert hit.reasons, "each hit must carry a human-readable reason"


def test_search_scores_rank_best_match_first():
    provider = make_provider()
    results = provider.search(SearchRequest(query="python sqlite FTS5", limit=10))
    assert results[0].id == "doc-python-search"


def test_pagination_pages_without_overlap():
    provider = make_provider()
    page1 = provider.search(SearchRequest(query="search", limit=2, offset=0))
    page2 = provider.search(SearchRequest(query="search", limit=2, offset=2))
    assert len(page1) <= 2 and len(page2) <= 2
    assert {h.id for h in page1}.isdisjoint({h.id for h in page2})
    all_hits = provider.search(SearchRequest(query="search", limit=10, offset=0))
    assert {h.id for h in page1} | {h.id for h in page2} <= {h.id for h in all_hits}


def test_search_with_total_reports_full_match_count():
    """The repository total travels with the window (pagination needs it).

    ``total`` is the full match count even when the returned window is
    smaller; ``search()`` keeps returning the plain hit list.
    """
    provider = make_provider()
    hits, total = provider.search_with_total(
        SearchRequest(query="search", limit=2, offset=0)
    )
    assert len(hits) <= 2
    # The seed corpus matches "search" more than twice: the true total
    # exceeds the 2-hit window (this is the pagination fix).
    assert total > len(hits) >= 1
    full = provider.search(SearchRequest(query="search", limit=1000, offset=0))
    assert total >= len(full) >= 1


def test_empty_query_raises_provider_error():
    provider = make_provider()
    with pytest.raises(ProviderError):
        provider.search(SearchRequest(query="   "))


def test_query_with_only_punctuation_returns_no_hits():
    provider = make_provider()
    assert provider.search(SearchRequest(query="!!!")) == []


def test_query_parser_strips_fts_operators():
    parsed = parse_query('python "exact" AND (fts5)')
    assert parsed.terms == ["python", "exact", "and", "fts5"]
    assert '"' not in parsed.fts_query and "(" not in parsed.fts_query


def test_deduplicator_keeps_first_hit_per_url():
    provider = make_provider()
    results = provider.search(SearchRequest(query="search", limit=10))
    assert len(deduplicate(list(results) + list(results))) == len(results)


def test_health_check_true_for_open_store():
    assert make_provider().health_check() is True


def test_configure_rejects_bad_config():
    provider = CustomIndexProvider(db_path=":memory:")
    with pytest.raises(Exception):
        provider.configure({"db_path": ""})  # type: ignore[dict-item]

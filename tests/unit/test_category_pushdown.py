"""Category pushdown + pagination fill tests.

Regression: category modes once returned the same result set as default
and ``total`` counted pre-filter matches, so tail pages rendered empty
with "No results found". ``site_category`` is now applied inside
retrieval (exact totals); a doubling fill loop covers the residual
post-retrieval filters, and an exhausted fetch makes the filtered
window the exact total.
"""

from __future__ import annotations

from openlens.core.engine import SearchEngine
from openlens.core.interfaces import Document, SearchRequest
from openlens.providers.custom_index import CustomIndexProvider
from openlens.storage.repository import DocumentRepository


def _doc(doc_id: str, category: str, text: str = "python guide") -> Document:
    return Document(
        id=doc_id,
        title=f"{text} {doc_id}",
        content=f"{text} content for {doc_id} with plenty of words " * 10,
        url=f"https://example.com/{doc_id}",
        metadata={"site_category": category},
    )


def _provider(news: int = 6, other: int = 30) -> CustomIndexProvider:
    provider = CustomIndexProvider(db_path=":memory:")
    provider.configure({"db_path": ":memory:", "seed": False})
    docs = [_doc(f"n{i}", "news") for i in range(news)]
    docs += [_doc(f"o{i}", "other") for i in range(other)]
    provider._repository.add_documents(docs)
    return provider


def test_store_search_narrows_rows_and_total() -> None:
    repo = DocumentRepository(":memory:")
    try:
        repo.add_documents([_doc("n1", "news"), _doc("o1", "other")])
        hits, total = repo.search("python", site_category="news")
        assert total == 1
        assert [d.id for d, _ in hits] == ["n1"]
        _, unfiltered = repo.search("python")
        assert unfiltered == 2
        _, bogus = repo.search("python", site_category="nope")
        assert bogus == 2
    finally:
        repo.close()


def test_provider_mode_prefilter_is_exact() -> None:
    provider = _provider()
    request = SearchRequest(query="python", mode="news", limit=10, offset=0)
    hits, total = provider.search_with_total(request)
    assert total == 6
    assert len(hits) == 6
    assert all(h.metadata.get("site_category") == "news" for h in hits)


def test_provider_explicit_category_beats_mode() -> None:
    provider = _provider()
    request = SearchRequest(
        query="python", mode="news", limit=10, offset=0, site_category="other"
    )
    _, total = provider.search_with_total(request)
    assert total == 30


def test_engine_news_mode_total_matches_results() -> None:
    engine = SearchEngine(providers=[_provider()], plugins=[])
    response = engine.search(SearchRequest(query="python", mode="news", limit=10))
    assert response.total == 6
    assert len(response.results) == 6
    # Page 2 (offset 10) is unreachable: total 6 at size 10 is one page,
    # so the UI can never render an empty "page 2".
    assert response.total <= 10


def test_engine_deep_page_never_outruns_total() -> None:
    engine = SearchEngine(providers=[_provider()], plugins=[])
    response = engine.search(
        SearchRequest(query="python", mode="news", limit=10, offset=10)
    )
    assert response.total == 6
    assert response.results == []


# --- fill loop over residual post-retrieval filters --------------------------


class _KeepOnlyFilter:
    """Test FILTER plugin: keep hits with 'keep' in the title."""

    name = "keep-only-filter"

    @property
    def plugin_type(self):  # noqa: ANN201 — test double
        from openlens.core.interfaces import PluginType

        return PluginType.FILTER

    @property
    def stage(self):  # noqa: ANN201 — test double
        from openlens.core.interfaces import PipelineStage

        return PipelineStage.FILTERING

    @property
    def mandatory(self) -> bool:
        return False

    @property
    def dependencies(self) -> tuple[str, ...]:
        return ()

    def configure(self, config: object) -> None:
        return None

    def run(self, context):  # noqa: ANN001, ANN201 — test double
        context.results = [r for r in context.results if "keep" in r.title]
        context.applied_filters.append(self.name)
        return context


def test_engine_fill_loop_covers_page_past_thinning_filter() -> None:
    provider = CustomIndexProvider(db_path=":memory:")
    provider.configure({"db_path": ":memory:", "seed": False})
    docs = [_doc(f"drop{i}", "other", text="python notes") for i in range(27)]
    docs.insert(3, _doc("keep-a", "other", text="python keep notes"))
    docs.insert(17, _doc("keep-b", "other", text="python keep notes"))
    docs.insert(28, _doc("keep-c", "other", text="python keep notes"))
    provider._repository.add_documents(docs)
    engine = SearchEngine(providers=[provider], plugins=[_KeepOnlyFilter()])
    response = engine.search(SearchRequest(query="python", mode="default", limit=10))
    assert response.total == 3
    assert [r.id for r in response.results] == ["keep-a", "keep-b", "keep-c"]
    assert "keep-only-filter" in response.applied_filters

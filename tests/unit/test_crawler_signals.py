"""Crawler capture for the new ranking signals (offline, no loopback).

Accept: ``_to_document`` carries the measured ``fetch_ms`` into metadata
and the capped, self-link-free outlink set; non-HTML pages carry neither;
the provider stamps ``inlink_count`` from the store hook and degrades
silently when the store lacks it.
"""

from __future__ import annotations

from openlens.core.interfaces import Document, SearchResult
from openlens.indexing.crawler import PoliteCrawler, _FetchedPage
from openlens.providers.custom_index import CustomIndexProvider
from openlens.storage.repository import DocumentRepository

PROSE = "Informative crawlable prose about the topic. " * 20


def _page(url: str, links: list[str], fetch_ms: float = 42.0) -> _FetchedPage:
    anchors = " ".join(f'<a href="{link}">x</a>' for link in links)
    return _FetchedPage(
        status=200,
        url=url,
        content_type="text/html",
        body=(
            f"<html><head><title>T</title></head><body><p>{PROSE}</p>"
            f"{anchors}</body></html>"
        ).encode(),
        headers={},
        fetch_ms=fetch_ms,
    )


def test_to_document_carries_fetch_ms_and_outlinks() -> None:
    crawler = PoliteCrawler()
    crawler.configure({})
    doc = crawler._to_document(
        _page("https://a.com/1", ["https://b.com/2", "https://a.com/1"])
    )
    assert doc.metadata["fetch_ms"] == 42.0
    # Self-link dropped, external kept.
    assert doc.metadata["outlinks"] == ["https://b.com/2"]


def test_to_document_caps_outlinks() -> None:
    crawler = PoliteCrawler()
    crawler.configure({})
    links = [f"https://x.com/{i}" for i in range(500)]
    doc = crawler._to_document(_page("https://a.com/1", links))
    assert len(doc.metadata["outlinks"]) == 200


def test_non_html_carries_no_outlinks() -> None:
    crawler = PoliteCrawler()
    crawler.configure({})
    page = _FetchedPage(
        status=200,
        url="https://a.com/f.bin",
        content_type="application/octet-stream",
        body=b"\x00\x01\x02",
        headers={},
        fetch_ms=7.0,
    )
    doc = crawler._to_document(page)
    assert doc.metadata["fetch_ms"] == 7.0
    assert "outlinks" not in doc.metadata


def test_provider_stamps_inlink_count() -> None:
    repo = DocumentRepository(":memory:")
    try:
        repo.add_documents(
            [
                Document(
                    id="https://b.com/2",
                    title="B",
                    content=PROSE,
                    url="https://b.com/2",
                    metadata={},
                ),
                Document(
                    id="https://a.com/1",
                    title="A",
                    content=PROSE,
                    url="https://a.com/1",
                    metadata={"outlinks": ["https://b.com/2"]},
                ),
            ]
        )
        provider = CustomIndexProvider(repository=repo)
        scored, _ = repo.search("b", limit=10)
        assert scored, "fixture must match"
        from openlens.retrieval.normalizer import normalize_many

        hits = normalize_many(scored, provider="custom_index", query="b")
        provider._enrich_inlink_counts(hits)
        assert hits[0].metadata["inlink_count"] == 1
    finally:
        repo.close()


def test_provider_enrichment_degrades_without_hook() -> None:
    provider = CustomIndexProvider.__new__(CustomIndexProvider)
    provider._repository = object()
    hit = SearchResult(
        id="x", title="X", snippet="", original_score=1.0, final_score=1.0
    )
    provider._enrich_inlink_counts([hit])
    assert "inlink_count" not in hit.metadata

"""Publisher date signals: capture, precedence, and future rejection.

Covers ``indexing/crawler.py::extract_page_attributes`` date capture
(JSON-LD ``datePublished``/``dateModified``, meta
``article:published_time``/``article:modified_time``/``pubdate`` plus
``og:article:`` variants, ``<time datetime>``), future-date rejection at
capture (degrade to unset), undated passthrough, ``<time>`` text kept in
visible content, and ``plugins/_signals.py::result_date`` picking up the
new ``date_modified`` key.
"""

from __future__ import annotations

from datetime import datetime, timezone

from openlens.core.interfaces import Document, SearchResult
from openlens.indexing import crawler as crawler_mod
from openlens.indexing.crawler import (
    PoliteCrawler,
    _FetchedPage,
    extract_page_attributes,
)
from openlens.indexing.indexer import RANKING_METADATA_KEYS
from openlens.plugins._signals import result_date

PROSE = "Informative crawlable prose about the topic. " * 20


def _doc(
    body: str,
    url: str = "https://x.test/p",
    headers: dict[str, str] | None = None,
) -> Document:
    page = _FetchedPage(
        status=200,
        url=url,
        content_type="text/html",
        body=body.encode(),
        headers=headers or {},
        fetch_ms=1.0,
    )
    return PoliteCrawler()._to_document(page)


def test_jsonld_published_wins_over_meta_and_time() -> None:
    html = """<html><head><title>T</title>
    <script type="application/ld+json">
    {"@type": "Article", "datePublished": "2024-03-01",
     "dateModified": "2024-04-02"}
    </script>
    <meta property="article:published_time" content="2024-05-06T10:00:00Z">
    <meta property="article:modified_time" content="2024-05-07T10:00:00Z">
    </head><body><p>prose here</p>
    <time datetime="2024-06-01">June</time></body></html>"""
    attrs = extract_page_attributes(html)
    assert attrs["published"] == "2024-03-01"
    assert attrs["date_modified"] == "2024-04-02"


def test_meta_pubdate_and_modified_time_captured() -> None:
    html = """<html><head><title>T</title>
    <meta name="pubdate" content="2023-11-02">
    <meta property="article:modified_time" content="2023-12-03T08:00:00Z">
    </head><body><p>prose here</p></body></html>"""
    attrs = extract_page_attributes(html)
    assert attrs["published"] == "2023-11-02"
    assert attrs["date_modified"] == "2023-12-03"


def test_og_article_variants_captured() -> None:
    html = """<html><head><title>T</title>
    <meta property="og:article:published_time" content="2022-01-15T00:00:00Z">
    <meta property="og:article:modified_time" content="2022-02-16">
    </head><body><p>prose here</p></body></html>"""
    attrs = extract_page_attributes(html)
    assert attrs["published"] == "2022-01-15"
    assert attrs["date_modified"] == "2022-02-16"


def test_meta_beats_time_when_no_jsonld() -> None:
    html = """<html><head><title>T</title>
    <meta property="article:published_time" content="2024-01-15">
    </head><body><p>prose here</p>
    <time datetime="2023-11-02">Nov 2</time></body></html>"""
    attrs = extract_page_attributes(html)
    assert attrs["published"] == "2024-01-15"


def test_future_dates_rejected_at_capture() -> None:
    html = """<html><head><title>T</title>
    <meta property="article:published_time" content="2999-01-01T00:00:00Z">
    <meta property="article:modified_time" content="2999-02-01">
    <meta name="pubdate" content="2999-03-01">
    </head><body><p>prose here</p>
    <time datetime="2999-04-01">future</time></body></html>"""
    attrs = extract_page_attributes(html)
    assert "published" not in attrs
    assert "date_modified" not in attrs

    html_ld = """<html><head><title>T</title>
    <script type="application/ld+json">
    {"@type": "Article", "datePublished": "2999-05-06",
     "dateModified": "2999-06-07"}
    </script>
    </head><body><p>prose here</p></body></html>"""
    attrs_ld = extract_page_attributes(html_ld)
    assert "published" not in attrs_ld
    assert "date_modified" not in attrs_ld


def test_future_header_last_modified_degrades_to_unset() -> None:
    doc = _doc(
        body="<html><head><title>T</title></head>"
        "<body><p>" + PROSE + "</p></body></html>",
        headers={"last-modified": "Tue, 01 Jan 2080 10:00:00 GMT"},
    )
    assert "published" not in doc.metadata
    assert doc.metadata["last_modified"] == "Tue, 01 Jan 2080 10:00:00 GMT"


def test_undated_page_passes_through_without_date_keys() -> None:
    html = "<html><head><title>T</title></head><body><p>" + PROSE + "</p></body></html>"
    attrs = extract_page_attributes(html)
    assert "published" not in attrs
    assert "date_modified" not in attrs
    assert attrs["sponsored"] is False


def test_result_date_picks_up_date_modified() -> None:
    def hit(**metadata: object) -> SearchResult:
        return SearchResult(
            id="d1",
            title="t",
            snippet="s",
            original_score=1.0,
            final_score=1.0,
            metadata=dict(metadata),
        )

    assert result_date(hit(date_modified="2024-07-08T00:00:00Z")) == datetime(
        2024, 7, 8, tzinfo=timezone.utc
    )
    # published still outranks date_modified; published_at outranks both.
    assert result_date(
        hit(published="2024-01-01T00:00:00Z", date_modified="2024-07-08T00:00:00Z")
    ) == datetime(2024, 1, 1, tzinfo=timezone.utc)
    assert result_date(
        hit(
            published_at="2023-01-01T00:00:00Z",
            published="2024-01-01T00:00:00Z",
            date_modified="2024-07-08T00:00:00Z",
        )
    ) == datetime(2023, 1, 1, tzinfo=timezone.utc)


def test_time_text_kept_in_visible_content() -> None:
    doc = _doc(
        body="<html><head><title>T</title></head><body><p>Launched "
        '<time datetime="2024-02-01">February 2024</time> today. '
        + PROSE
        + "</p></body></html>"
    )
    assert "February 2024" in doc.content
    assert doc.metadata["published"] == "2024-02-01"


def test_ranking_keys_cover_extracted_date_keys() -> None:
    assert set(crawler_mod.EXTRACTED_METADATA_KEYS) <= set(RANKING_METADATA_KEYS)
    assert "date_modified" in RANKING_METADATA_KEYS

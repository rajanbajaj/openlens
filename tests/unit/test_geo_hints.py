"""Geo + scheduling-hint signals: ccTLD, hreflang, content-language, lastmod.

GEO-2: sitemap ``<lastmod>`` is a dequeue-ordering hint only (never a
content date); existing ``ETag``/``Last-Modified`` validators already
feed conditional refetch, pinned here by regression tests.

GEO-3: ccTLD-derived ``geo_hint`` + hreflang alternates + content-language
meta captured into metadata through the same extractor pipeline.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from openlens.indexing import crawler as crawler_mod
from openlens.indexing.crawler import (
    PoliteCrawler,
    _FetchedPage,
    cctld_geo_hint,
    extract_page_attributes,
)
from openlens.indexing.discovery import lastmod_hint_priority
from openlens.indexing.indexer import RANKING_METADATA_KEYS
from openlens.indexing.worker import CrawlerWorker
from openlens.storage.frontier import SqliteFrontierStore

PROSE = "Informative crawlable prose about the topic. " * 20


def _doc(
    body: str,
    url: str = "https://x.test/p",
    headers: dict[str, str] | None = None,
):
    page = _FetchedPage(
        status=200,
        url=url,
        content_type="text/html",
        body=body.encode(),
        headers=headers or {},
        fetch_ms=1.0,
    )
    return PoliteCrawler()._to_document(page)


def _worker(store: SqliteFrontierStore) -> CrawlerWorker:
    crawler = PoliteCrawler()
    crawler.configure({"default_max_depth": 3})
    return CrawlerWorker(
        crawler=crawler,
        repository=None,
        frontier_store=store,
    )


def _settings(store: SqliteFrontierStore, **overrides: Any) -> None:
    payload: dict[str, Any] = {
        "default_max_depth": 3,
        "discovery_feeds": [],
        "refresh_after_days": 0,
        "discovery_interval_s": 3600,
    }
    payload.update(overrides)
    store.save_settings(payload)


def _iso(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _sitemap(*pairs: tuple[str, str]) -> str:
    urls = "".join(
        f"<url><loc>{loc}</loc><lastmod>{lastmod}</lastmod></url>"
        for loc, lastmod in pairs
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{urls}</urlset>"
    )


# -- GEO-3: ccTLD geo hint ---------------------------------------------


def test_cctld_hint_common_cases() -> None:
    assert cctld_geo_hint("https://example.de/page") == "de"
    assert cctld_geo_hint("https://EXAMPLE.FR/") == "fr"
    assert cctld_geo_hint("https://example.co.uk/page") == "uk"
    assert cctld_geo_hint("https://sub.example.com.br/x") == "br"


def test_cctld_hint_degrades_to_unset() -> None:
    assert cctld_geo_hint("https://example.com/page") is None
    assert cctld_geo_hint("https://example.org/") is None
    assert cctld_geo_hint("http://192.168.0.1/page") is None
    assert cctld_geo_hint("http://localhost/page") is None
    assert cctld_geo_hint("https://example.eu/") is None
    assert cctld_geo_hint("not a url") is None
    assert cctld_geo_hint("") is None
    assert cctld_geo_hint(None) is None


def test_geo_hint_extracted_from_page_url() -> None:
    html = "<html><head><title>T</title></head><body><p>" + PROSE + "</p></body></html>"
    attrs = extract_page_attributes(html, url="https://example.de/nachrichten")
    assert attrs["geo_hint"] == "de"


def test_geo_hint_absent_for_gtld() -> None:
    html = "<html><head><title>T</title></head><body><p>" + PROSE + "</p></body></html>"
    attrs = extract_page_attributes(html, url="https://example.com/news")
    assert "geo_hint" not in attrs


def test_geo_hint_alone_never_makes_a_page() -> None:
    # Garbage markup on a ccTLD host still yields {}: the hint derives
    # from the URL, not from page content, so it is not a page signal.
    assert extract_page_attributes("\x00\x01\x02", url="https://example.de/x") == {}


# -- GEO-3: hreflang alternates ----------------------------------------


def test_hreflang_alternates_captured() -> None:
    html = (
        "<html><head><title>T</title>"
        '<link rel="alternate" hreflang="de" href="https://example.de/nachrichten">'
        '<link rel="alternate" hreflang="en" href="/en/news">'
        "</head><body><p>" + PROSE + "</p></body></html>"
    )
    attrs = extract_page_attributes(html, url="https://example.de/nachrichten")
    assert attrs["hreflang"] == {
        "de": "https://example.de/nachrichten",
        "en": "https://example.de/en/news",
    }


def test_hreflang_first_wins_and_bare_alternate_ignored() -> None:
    html = (
        "<html><head><title>T</title>"
        '<link rel="alternate" hreflang="en" href="https://example.com/en-a">'
        '<link rel="alternate" hreflang="EN" href="https://example.com/en-b">'
        '<link rel="alternate" href="https://example.com/feed.xml">'
        '<link rel="alternate" hreflang="" href="https://example.com/blank">'
        '<link rel="alternate" hreflang="fr">'
        "</head><body><p>" + PROSE + "</p></body></html>"
    )
    attrs = extract_page_attributes(html, url="https://example.com/page")
    assert attrs["hreflang"] == {"en": "https://example.com/en-a"}


def test_hreflang_absent_when_no_link_tags() -> None:
    html = "<html><head><title>T</title></head><body><p>" + PROSE + "</p></body></html>"
    assert "hreflang" not in extract_page_attributes(html)


# -- GEO-3: content-language meta --------------------------------------


def test_content_language_meta_captured() -> None:
    html = (
        "<html><head><title>T</title>"
        '<meta http-equiv="content-language" content="DE">'
        "</head><body><p>" + PROSE + "</p></body></html>"
    )
    assert extract_page_attributes(html)["language"] == "de"


def test_jsonld_language_wins_over_content_language() -> None:
    html = (
        "<html><head><title>T</title>"
        '<script type="application/ld+json">'
        '{"@type": "Article", "inLanguage": "fr"}'
        "</script>"
        '<meta name="content-language" content="de">'
        "</head><body><p>" + PROSE + "</p></body></html>"
    )
    assert extract_page_attributes(html)["language"] == "fr"


# -- GEO-3: pipeline preservation --------------------------------------


def test_ranking_keys_cover_geo_keys() -> None:
    assert set(crawler_mod.EXTRACTED_METADATA_KEYS) <= set(RANKING_METADATA_KEYS)
    assert "geo_hint" in RANKING_METADATA_KEYS
    assert "hreflang" in RANKING_METADATA_KEYS


def test_geo_keys_survive_to_document() -> None:
    doc = _doc(
        body="<html><head><title>T</title>"
        '<link rel="alternate" hreflang="de" href="https://example.de/a">'
        "</head><body><p>" + PROSE + "</p></body></html>",
        url="https://example.de/a",
    )
    assert doc.metadata["geo_hint"] == "de"
    assert doc.metadata["hreflang"] == {"de": "https://example.de/a"}


# -- GEO-2: lastmod as scheduling hint only ----------------------------


def test_lastmod_hint_priority_buckets() -> None:
    assert lastmod_hint_priority(_iso(0)) == 2
    assert lastmod_hint_priority(_iso(6)) == 2
    assert lastmod_hint_priority(_iso(10)) == 1
    assert lastmod_hint_priority(_iso(29)) == 1
    assert lastmod_hint_priority(_iso(60)) == 0


def test_lastmod_hint_priority_degrades_to_neutral() -> None:
    assert lastmod_hint_priority("") == 0
    assert lastmod_hint_priority(None) == 0
    assert lastmod_hint_priority("not-a-date") == 0
    assert lastmod_hint_priority(12345) == 0
    future = (datetime.now(timezone.utc) + timedelta(days=30)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    assert lastmod_hint_priority(future) == 0


def test_discovery_pass_prioritizes_fresh_lastmod() -> None:
    store = SqliteFrontierStore(":memory:")
    _settings(store, discovery_feeds=["https://news.example/sitemap.xml"])
    worker = _worker(store)
    worker.refresh_limits()
    xml = _sitemap(
        ("https://news.example/fresh", _iso(1)),
        ("https://news.example/old", _iso(90)),
    )
    result = worker.run_discovery_pass(
        http_fetch=lambda url, **kwargs: xml,
    )
    assert result["new_urls"] == 2
    by_url = {entry.url: entry.priority for entry in store.pending(limit=10)}
    assert by_url["https://news.example/fresh"] == 2
    assert by_url["https://news.example/old"] == 0


def test_discovery_pass_keeps_priority_of_known_urls() -> None:
    store = SqliteFrontierStore(":memory:")
    assert store.enqueue("https://news.example/known", depth=0, priority=5)
    _settings(store, discovery_feeds=["https://news.example/sitemap.xml"])
    worker = _worker(store)
    worker.refresh_limits()
    xml = _sitemap(("https://news.example/known", _iso(0)))
    result = worker.run_discovery_pass(
        http_fetch=lambda url, **kwargs: xml,
    )
    assert result["new_urls"] == 0
    by_url = {entry.url: entry.priority for entry in store.pending(limit=10)}
    assert by_url["https://news.example/known"] == 5


def test_lastmod_never_becomes_content_date() -> None:
    # The sitemap date travels only as far as queue priority: a page
    # fetched from a discovered URL carries no published/date_modified
    # unless the page itself (or its headers) says so.
    store = SqliteFrontierStore(":memory:")
    _settings(store, discovery_feeds=["https://news.example/sitemap.xml"])
    worker = _worker(store)
    worker.refresh_limits()
    xml = _sitemap(("https://news.example/dated", _iso(1)))
    worker.run_discovery_pass(http_fetch=lambda url, **kwargs: xml)
    assert store.validators() == {}
    doc = _doc(
        body="<html><head><title>T</title></head>"
        "<body><p>" + PROSE + "</p></body></html>",
        url="https://news.example/dated",
    )
    assert "published" not in doc.metadata
    assert "date_modified" not in doc.metadata


def test_conditional_validators_remembered_for_refetch() -> None:
    # Existing conditional-fetch plumbing: response validators seed the
    # next request's If-None-Match/If-Modified-Since headers, and
    # frontier rows persist them across resumes via mark_done.
    crawler = PoliteCrawler()
    crawler._remember_conditional(
        "https://news.example/a",
        {"etag": '"abc"', "last-modified": "Tue, 01 Jan 2030 10:00:00 GMT"},
    )
    headers = crawler._conditional.get(
        crawler_mod.normalize_url("https://news.example/a"), {}
    )
    assert headers["If-None-Match"] == '"abc"'
    assert headers["If-Modified-Since"] == "Tue, 01 Jan 2030 10:00:00 GMT"

    store = SqliteFrontierStore(":memory:")
    assert store.enqueue("https://news.example/a", depth=0)
    store.mark_done(
        "https://news.example/a",
        etag='"abc"',
        last_modified="Tue, 01 Jan 2030 10:00:00 GMT",
    )
    saved = store.validators()["https://news.example/a"]
    assert saved["etag"] == '"abc"'
    assert saved["last_modified"] == "Tue, 01 Jan 2030 10:00:00 GMT"

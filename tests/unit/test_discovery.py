"""Sitemap/RSS discovery parsing (offline fixtures, no network)."""

from __future__ import annotations

from openlens.indexing.discovery import (
    discover_from_feed,
    discover_from_sitemap,
)

SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://news.example/a-breaking-story</loc>
      <lastmod>2026-10-30T08:00:00Z</lastmod></url>
  <url><loc>https://news.example/an-older-piece</loc></url>
</urlset>
"""

SITEMAP_INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://news.example/sitemap-oct.xml</loc></sitemap>
  <sitemap><loc>https://news.example/sitemap-sep.xml</loc></sitemap>
</sitemapindex>
"""

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Example News</title>
<item><title>Fresh story</title>
    <link>https://news.example/fresh-story</link>
    <pubDate>Fri, 30 Oct 2026 08:00:00 GMT</pubDate></item>
<item><title>Undated</title><link>https://news.example/undated</link></item>
</channel></rss>
"""

ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Example Blog</title>
<entry><title>Hello</title>
    <link href="https://blog.example/hello"/>
    <updated>2026-10-29T10:00:00Z</updated></entry>
</feed>
"""


def test_sitemap_yields_urls_with_lastmod() -> None:
    found = discover_from_sitemap(SITEMAP)
    assert ("https://news.example/a-breaking-story", "2026-10-30T08:00:00Z") in found
    assert any(url == "https://news.example/an-older-piece" for url, _ in found)


def test_sitemap_index_yields_child_sitemaps() -> None:
    found = discover_from_sitemap(SITEMAP_INDEX)
    assert [url for url, _ in found] == [
        "https://news.example/sitemap-oct.xml",
        "https://news.example/sitemap-sep.xml",
    ]


def test_sitemap_malformed_returns_empty() -> None:
    assert discover_from_sitemap("not xml at all <") == []
    assert discover_from_sitemap("") == []


def test_rss_yields_items() -> None:
    found = discover_from_feed(RSS)
    assert any(url == "https://news.example/fresh-story" for url, _ in found)
    assert any(url == "https://news.example/undated" for url, _ in found)


def test_atom_yields_entries() -> None:
    found = discover_from_feed(ATOM)
    assert found == [("https://blog.example/hello", "2026-10-29T10:00:00Z")]


def test_feed_malformed_returns_empty() -> None:
    assert discover_from_feed("just text") == []
    assert discover_from_feed("") == []

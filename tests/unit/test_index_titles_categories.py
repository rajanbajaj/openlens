"""Track INDEX regression tests: garbled titles + site categories.

Covers the title cascade (head-only non-SVG capture in both parsers,
``_clean_title``, og/twitter:title, first-h1, URL fallback), the
always-bool ``sponsored`` boundary, rules-v1 site classification, the
``configs/category_domains.yaml`` loader, repository category columns
(+ legacy migration), and the indexer backfill path.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from openlens.core.interfaces import Document
from openlens.indexing import crawler as crawler_mod
from openlens.indexing.crawler import (
    _LinkTitleParser,
    _PageDataParser,
    _clean_title,
    classify_site_category,
    extract_page_attributes,
    load_category_domains,
    resolve_page_title,
)
from openlens.indexing.indexer import RANKING_METADATA_KEYS, DocumentIndexer
from openlens.storage.repository import DocumentRepository

DOMAINS: dict[str, frozenset[str]] = {
    "shopping_allow": frozenset({"shop.example"}),
    "news_allow": frozenset({"news.example"}),
    "kids_allow": frozenset({"kids.example"}),
    "adult_block": frozenset({"adult.example"}),
}


def _parse(cls: type, html: str):  # helper: feed and return parser
    parser = cls()
    parser.feed(html)
    return parser


# --- _clean_title / cascade -------------------------------------------------


def test_clean_title_unescapes_and_collapses() -> None:
    assert _clean_title("Fish &amp;  Chips\n\tRoom") == "Fish & Chips Room"


def test_clean_title_rejects_blanks_and_icon_label_runs() -> None:
    assert _clean_title("") is None
    assert _clean_title("   ") is None
    assert _clean_title(None) is None
    assert _clean_title("★★★") is None  # icon-label run, no word chars
    assert _clean_title("a") == "a"


def test_cascade_order_head_meta_h1_url() -> None:
    assert resolve_page_title("Head", "OG", "H1", "http://u/") == "Head"
    assert resolve_page_title("", "OG", "H1", "http://u/") == "OG"
    assert resolve_page_title("  ", " ", "H1", "http://u/") == "H1"
    assert resolve_page_title("", "", "", "http://u/") == "http://u/"


# --- head-only non-SVG capture in BOTH parsers ------------------------------


HEAD_TWO_SVG = (
    "<html><head><title>Real Title</title></head><body>"
    '<svg xmlns="http://www.w3.org/2000/svg"><title>Search</title></svg>'
    "<h1>Body Headline</h1>"
    "<svg><title>Menu</title></svg>"
    "</body></html>"
)


def test_head_plus_two_svg_icons_link_parser() -> None:
    parser = _parse(_LinkTitleParser, HEAD_TWO_SVG)
    assert parser.title == "Real Title"
    assert parser.h1 == "Body Headline"
    assert parser.resolved_title("http://u/") == "Real Title"


def test_head_plus_two_svg_icons_page_data_parser() -> None:
    parser = _parse(_PageDataParser, HEAD_TWO_SVG)
    assert parser.title == "Real Title"
    assert parser.h1 == "Body Headline"
    assert parser.resolved_title("http://u/") == "Real Title"


def test_body_svg_title_ignored_falls_to_h1() -> None:
    html = (
        "<html><head></head><body>"
        "<svg><title>Close</title></svg><h1>Story</h1>"
        "</body></html>"
    )
    assert _parse(_LinkTitleParser, html).resolved_title("http://u/") == "Story"
    assert _parse(_PageDataParser, html).resolved_title("http://u/") == "Story"


def test_svg_title_inside_head_ignored() -> None:
    html = (
        "<html><head><svg><title>Icon</title></svg>"
        "<title>Actual</title></head><body></body></html>"
    )
    assert _parse(_LinkTitleParser, html).title == "Actual"
    assert _parse(_PageDataParser, html).title == "Actual"


def test_og_title_preferred_over_twitter_title() -> None:
    html = (
        "<html><head>"
        '<meta name="twitter:title" content="Tw Title">'
        '<meta property="og:title" content="OG Title">'
        "</head><body></body></html>"
    )
    link_parser = _parse(_LinkTitleParser, html)
    assert link_parser.meta_title == "OG Title"
    assert link_parser.resolved_title("http://u/") == "OG Title"
    page_parser = _parse(_PageDataParser, html)
    assert page_parser.meta_title == "OG Title"


def test_implied_head_title_without_head_wrapper() -> None:
    html = "<html><title>Loose</title><body><p>x</p></body></html>"
    assert _parse(_LinkTitleParser, html).title == "Loose"


# --- crawler extraction boundary --------------------------------------------


def test_to_document_title_cascade_and_bool_sponsored() -> None:
    from openlens.indexing.crawler import PoliteCrawler, _FetchedPage

    crawler = PoliteCrawler()
    page = _FetchedPage(
        status=200,
        url="https://example.com/a",
        content_type="text/html",
        body=HEAD_TWO_SVG.encode(),
    )
    doc = crawler._to_document(page)
    assert doc.title == "Real Title"
    assert doc.metadata["sponsored"] is False
    assert doc.metadata["site_category"] == "other"
    assert doc.metadata["site_category_source"] == "rules-v1"


def test_extract_sponsored_always_bool() -> None:
    plain = "<html><head><title>t</title></head><body><p>x</p></body></html>"
    assert extract_page_attributes(plain)["sponsored"] is False
    flagged = plain.replace("<p>", '<a href="https://a.example" rel="sponsored">', 1)
    assert extract_page_attributes(flagged)["sponsored"] is True


def test_extract_garbage_still_empty() -> None:
    assert extract_page_attributes("not html < <") == {}
    assert (
        extract_page_attributes('<script type="application/ld+json">{oops</script>')
        == {}
    )


# --- rules-v1 classification -------------------------------------------------


def test_shopping_domain_hit_confidence_full() -> None:
    verdict = classify_site_category("https://shop.example/item", {}, domains=DOMAINS)
    assert verdict["site_category"] == "shopping"
    assert verdict["site_category_confidence"] == 1.0
    assert verdict["site_category_source"] == "rules-v1"


def test_shopping_subdomain_matches() -> None:
    verdict = classify_site_category("https://m.shop.example/x", {}, domains=DOMAINS)
    assert (verdict["site_category"], verdict["kids_domain_hit"]) == (
        "shopping",
        0,
    )


def test_shopping_price_plus_combo_is_point_nine() -> None:
    meta = {"price": 19.99, "currency": "USD"}
    verdict = classify_site_category("https://example.com/x", meta, domains=DOMAINS)
    assert (verdict["site_category"], verdict["site_category_confidence"]) == (
        "shopping",
        0.9,
    )


def test_shopping_price_alone_is_point_seven() -> None:
    verdict = classify_site_category(
        "https://example.com/x", {"price": 5.0}, domains=DOMAINS
    )
    assert (verdict["site_category"], verdict["site_category_confidence"]) == (
        "shopping",
        0.7,
    )


def test_shopping_product_brand_without_price_is_point_seven() -> None:
    html = (
        '<html><head><script type="application/ld+json">'
        '{"@type": "Product", "brand": "Acme"}'
        "</script></head><body>x</body></html>"
    )
    attrs = extract_page_attributes(html, url="https://example.com/x")
    assert attrs["site_category"] == "shopping"
    assert attrs["site_category_confidence"] == 0.7


def test_shopping_commerce_path_plus_price_is_point_six() -> None:
    verdict = classify_site_category(
        "https://example.com/product/widget", {"price": 9.99}, domains=DOMAINS
    )
    assert (verdict["site_category"], verdict["site_category_confidence"]) == (
        "shopping",
        0.6,
    )


def test_news_ladder() -> None:
    domain = classify_site_category("https://news.example/a", {}, domains=DOMAINS)
    assert (domain["site_category"], domain["site_category_confidence"]) == (
        "news",
        0.9,
    )
    assert domain["news_domain_hit"] == 1
    article_full = classify_site_category(
        "https://example.com/a",
        {"section": "World", "author": "A", "published": "2024-01-01"},
        domains=DOMAINS,
        has_article=True,
    )
    assert (
        article_full["site_category"],
        article_full["site_category_confidence"],
    ) == ("news", 0.85)
    article_bare = classify_site_category(
        "https://example.com/a", {}, domains=DOMAINS, has_article=True
    )
    assert (
        article_bare["site_category"],
        article_bare["site_category_confidence"],
    ) == ("news", 0.6)


def test_kids_fail_closed() -> None:
    allowed = classify_site_category("https://kids.example/game", {}, domains=DOMAINS)
    assert (allowed["site_category"], allowed["site_category_confidence"]) == (
        "kids",
        1.0,
    )
    assert allowed["kids_domain_hit"] == 1
    adult_meta = classify_site_category(
        "https://kids.example/game",
        {"content_safety": "adult"},
        domains=DOMAINS,
    )
    assert adult_meta["site_category"] != "kids"
    adult_domain = classify_site_category(
        "https://adult.example/x", {}, domains=DOMAINS
    )
    assert adult_domain["site_category"] != "kids"


def test_other_default_when_no_rules_fire() -> None:
    verdict = classify_site_category("https://example.com/x", {}, domains=DOMAINS)
    assert verdict["site_category"] == "other"
    assert verdict["site_category_confidence"] == 1.0
    assert verdict["site_category_source"] == "rules-v1"
    assert verdict["kids_domain_hit"] == 0
    assert verdict["news_domain_hit"] == 0


# --- category_domains.yaml loader --------------------------------------------


def test_category_domains_yaml_loads() -> None:
    repo_root = Path(crawler_mod.__file__).resolve().parents[3]
    config = repo_root / "configs" / "category_domains.yaml"
    assert config.is_file()
    domains = load_category_domains(str(config))
    assert set(domains) == {
        "shopping_allow",
        "news_allow",
        "kids_allow",
        "adult_block",
    }
    assert "amazon.com" in domains["shopping_allow"]
    assert "bbc.com" in domains["news_allow"]
    assert "pbskids.org" in domains["kids_allow"]


# --- ranking-key parity -------------------------------------------------------


def test_extracted_category_keys_preserved_by_indexer() -> None:
    for key in (
        "site_category",
        "site_category_confidence",
        "site_category_source",
        "kids_domain_hit",
        "news_domain_hit",
        "sponsored",
    ):
        assert key in crawler_mod.EXTRACTED_METADATA_KEYS
        assert key in RANKING_METADATA_KEYS


# --- repository columns + legacy migration ------------------------------------


def _doc(doc_id: str, meta: dict | None = None) -> Document:
    return Document(
        id=doc_id,
        title=f"title {doc_id}",
        content=f"body content for {doc_id}",
        url=f"https://example.com/{doc_id}",
        metadata=dict(meta or {}),
    )


def test_repository_round_trips_category_columns() -> None:
    repo = DocumentRepository(":memory:")
    meta = {
        "site_category": "shopping",
        "site_category_confidence": 0.9,
        "site_category_source": "rules-v1",
        "kids_domain_hit": False,
        "news_domain_hit": 0,
    }
    repo.add_document(_doc("c1", meta))
    stored = repo.get("c1")
    assert stored is not None
    assert stored.metadata["site_category"] == "shopping"
    assert stored.metadata["site_category_confidence"] == 0.9
    assert stored.metadata["site_category_source"] == "rules-v1"
    row = repo._conn.execute(
        "SELECT site_category, kids_domain_hit FROM documents WHERE id = 'c1'"
    ).fetchone()
    assert (row["site_category"], row["kids_domain_hit"]) == ("shopping", 0)


def test_repository_coerces_unknown_category_to_other() -> None:
    repo = DocumentRepository(":memory:")
    repo.add_document(_doc("c2", {"site_category": "toys"}))
    assert repo.get("c2").metadata["site_category"] == "other"  # type: ignore[union-attr]


def test_repository_check_constraint_rejects_bad_category_sql() -> None:
    repo = DocumentRepository(":memory:")
    with repo._lock:
        with pytest.raises(sqlite3.IntegrityError):
            repo._conn.execute(
                "INSERT INTO documents (id, title, content, site_category)"
                " VALUES ('bad1', 't', 'c', 'toys')"
            )


def test_repository_migrates_legacy_schema(tmp_path: Path) -> None:
    db_path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE documents (id TEXT PRIMARY KEY, title TEXT NOT NULL,"
        " content TEXT NOT NULL, url TEXT, metadata TEXT NOT NULL DEFAULT '{}',"
        " content_hash TEXT, url_norm TEXT, content_type TEXT, published TEXT)"
    )
    meta = {
        "site_category": "news",
        "site_category_confidence": 0.9,
        "site_category_source": "rules-v1",
        "kids_domain_hit": 0,
        "news_domain_hit": 1,
    }
    conn.execute(
        "INSERT INTO documents (id, title, content, url, metadata)"
        " VALUES (?, ?, ?, ?, ?)",
        ("n1", "t", "legacy body text", "https://news.example/a", json.dumps(meta)),
    )
    conn.commit()
    conn.close()
    repo = DocumentRepository(db_path)
    row = repo._conn.execute(
        "SELECT site_category, site_category_confidence, site_category_source,"
        " kids_domain_hit, news_domain_hit FROM documents WHERE id = 'n1'"
    ).fetchone()
    assert row["site_category"] == "news"
    assert row["site_category_confidence"] == 0.9
    assert row["site_category_source"] == "rules-v1"
    assert (row["kids_domain_hit"], row["news_domain_hit"]) == (0, 1)
    indexes = {
        r[0]
        for r in repo._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
    }
    assert "idx_documents_site_category" in indexes
    assert "idx_documents_kids_domain_hit" in indexes


# --- indexer backfill ----------------------------------------------------------


def test_backfill_site_categories_reclassifies() -> None:
    repo = DocumentRepository(":memory:")
    indexer = DocumentIndexer(repo)
    repo.add_document(_doc("b1", {"price": 10.0, "currency": "USD"}))
    repo.add_document(_doc("b2"))
    rewritten = indexer.backfill_site_categories()
    assert rewritten == 2
    assert repo.get("b1").metadata["site_category"] == "shopping"  # type: ignore[union-attr]
    assert repo.get("b2").metadata["site_category"] == "other"  # type: ignore[union-attr]
    assert repo.get("b1").metadata["site_category_source"] == "rules-v1"  # type: ignore[union-attr]
    assert indexer.backfill_site_categories() == 0

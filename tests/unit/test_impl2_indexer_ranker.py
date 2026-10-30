"""IMPL-2: indexer populates new columns; ranker gains integrity signals.

Accept: the offline crawler extractor emits vertical metadata (price,
author, published, recipe, sponsored, ...) from HTML alone; the indexer
preserves those keys across incremental updates; the integrity scorer
derives safety/privacy/fairness/news-trust/sponsored signals purely
from indexed data with reasons + warnings; the ranker composite honors
the new per-mode weights; YAML and builtins stay in parity.
"""

from __future__ import annotations

import pytest

from openlens.core.context import SearchContext
from openlens.core.interfaces import Document, SearchRequest, SearchResult
from openlens.indexing import crawler as crawler_mod
from openlens.indexing.crawler import (
    PoliteCrawler,
    _FetchedPage,
    extract_page_attributes,
)
from openlens.indexing.indexer import RANKING_METADATA_KEYS, DocumentIndexer
from openlens.plugins.integrity_scorer import IntegrityScorer
from openlens.ranking.profiles import BUILTIN_PROFILES, get_profile, load_profiles
from openlens.ranking.ranker import Ranker
from openlens.storage.repository import DocumentRepository


def _hit(
    id: str,
    title: str,
    snippet: str = "",
    metadata: dict | None = None,
    original: float = 1.0,
) -> SearchResult:
    return SearchResult(
        id=id,
        title=title,
        snippet=snippet,
        original_score=original,
        final_score=original,
        metadata=dict(metadata or {}),
    )


def _ctx(*hits: SearchResult) -> SearchContext:
    ctx = SearchContext(request=SearchRequest(query="q"))
    ctx.results = list(hits)
    return ctx


# --- Crawler: offline vertical extraction ------------------------------------


def test_extract_product_jsonld() -> None:
    html = """<html><head><title>Widget</title>
    <script type="application/ld+json">
    {"@context": "https://schema.org", "@type": "Product",
     "name": "Widget", "brand": {"@type": "Brand", "name": "Acme"},
     "offers": {"@type": "Offer", "price": "19.99",
       "priceCurrency": "usd",
       "availability": "https://schema.org/InStock"},
     "aggregateRating": {"@type": "AggregateRating",
       "ratingValue": "4.5", "reviewCount": "120"}}
    </script></head><body>hi</body></html>"""
    attrs = extract_page_attributes(html)
    assert attrs["price"] == pytest.approx(19.99)
    assert attrs["currency"] == "USD"
    assert attrs["availability"] == "instock"
    assert attrs["brand"] == "Acme"
    assert attrs["rating"] == pytest.approx(4.5)
    assert attrs["rating_count"] == 120


def test_extract_book_recipe_and_article() -> None:
    html = """<html lang="en"><head>
    <script type="application/ld+json">
    [{"@type": "Book", "author": {"@name": "X", "@type": "Person", "name": "Ada"},
      "isbn": "978-0-00-000000-0", "publisher": "Open Press",
      "license": "https://creativecommons.org/licenses/by/4.0/",
      "datePublished": "2024-03-01"}]
    </script>
    <script type="application/ld+json">
    {"@type": "Recipe", "recipeIngredient": ["flour", "water"],
     "recipeCuisine": "Italian",
     "suitableForDiet": "https://schema.org/VeganDiet",
     "totalTime": "PT1H30M"}
    </script>
    <meta property="article:published_time" content="2024-05-06T10:00:00Z">
    <meta property="article:section" content="Opinion">
    </head><body>x</body></html>"""
    attrs = extract_page_attributes(html)
    assert attrs["language"] == "en"
    assert attrs["author"] == "Ada"
    assert attrs["isbn"] == "978-0-00-000000-0"
    assert attrs["publisher"] == "Open Press"
    assert attrs["license"].endswith("/by/4.0/")
    assert attrs["ingredients"] == ["flour", "water"]
    assert attrs["cuisine"] == "Italian"
    assert attrs["dietary_tags"] == ["vegandiet"]
    assert attrs["total_minutes"] == 90
    # JSON-LD datePublished wins over the later article meta tag.
    assert attrs["published"] == "2024-03-01"
    assert attrs["section"] == "Opinion"


def test_extract_sponsored_and_safety_markers() -> None:
    html = """<html><head><title>t</title>
    <meta name="rating" content="adult">
    <meta property="og:price:amount" content="$29.99">
    <meta property="og:price:currency" content="eur">
    </head><body>
    <a href="https://ads.example/x" rel="sponsored nofollow">ad</a>
    <time datetime="2023-11-02">Nov 2</time>
    </body></html>"""
    attrs = extract_page_attributes(html)
    assert attrs["sponsored"] is True
    assert attrs["content_safety"] == "adult"
    assert attrs["price"] == pytest.approx(29.99)
    assert attrs["currency"] == "EUR"
    assert attrs["published"] == "2023-11-02"


def test_extract_malformed_input_never_raises() -> None:
    assert extract_page_attributes("not html < <") == {}
    assert (
        extract_page_attributes('<script type="application/ld+json">{oops</script>')
        == {}
    )
    page = _FetchedPage(
        status=200,
        url="https://x.test/",
        content_type="text/plain",
        body=b"plain",
        headers={},
    )
    doc = PoliteCrawler()._to_document(page)
    assert doc.metadata == {"content_type": "text/plain"}


def test_to_document_merges_page_attributes_over_header_fallback() -> None:
    body = (
        "<html><head><title>T</title>"
        '<meta property="article:published_time" content="2024-01-15">'
        "</head><body>t</body></html>"
    ).encode()
    page = _FetchedPage(
        status=200,
        url="https://x.test/p",
        content_type="text/html",
        body=body,
        headers={"last-modified": "Tue, 02 Jan 2024 10:00:00 GMT"},
    )
    doc = PoliteCrawler()._to_document(page)
    assert doc.title == "T"
    # Page content beats the transport-header fallback.
    assert doc.metadata["published"] == "2024-01-15"
    assert doc.metadata["last_modified"] == "Tue, 02 Jan 2024 10:00:00 GMT"


# --- Indexer: preserved ranker inputs ----------------------------------------


def test_ranking_metadata_keys_cover_extracted_keys() -> None:
    assert set(crawler_mod.EXTRACTED_METADATA_KEYS) <= set(RANKING_METADATA_KEYS)


def test_index_incremental_preserves_new_keys() -> None:
    repo = DocumentRepository(db_path=":memory:")
    indexer = DocumentIndexer(repo)
    stored = Document(
        id="d1",
        title="T",
        content="body",
        url="https://x.test/p",
        metadata={"price": 9.5, "sponsored": True, "published": "2024-01-01"},
    )
    indexer.index_incremental([stored])
    # Refresh without the new keys: stored values must survive.
    refreshed = Document(
        id="d1",
        title="T2",
        content="body2",
        url="https://x.test/p",
        metadata={},
    )
    stats = indexer.index_incremental([refreshed])
    assert stats.updated == 1
    got = repo.get("d1")
    assert got is not None
    assert got.metadata["price"] == pytest.approx(9.5)
    assert got.metadata["sponsored"] is True
    assert got.metadata["published"] == "2024-01-01"


# --- Integrity scorer: indexed-data-only signals ------------------------------


def test_integrity_scorer_clean_hit_all_ones() -> None:
    hit = _hit(
        "a",
        "Quiet guide to bread",
        "bake at home",
        {"trusted": True, "published": "2024-01-01"},
    )
    IntegrityScorer().run(_ctx(hit))
    signals = hit.metadata["signals"]
    assert signals == {
        "safety": 1.0,
        "privacy": 1.0,
        "fairness": 1.0,
        "news_trust": 1.0,
        "sponsored": 1.0,
    }
    assert any(r.startswith("integrity:") for r in hit.reasons)
    assert hit.warnings == []


def test_integrity_scorer_flags_with_warnings() -> None:
    hit = _hit(
        "b",
        "Deal: women can't resist these prices",
        "call +1-212-555-0147 now",
        {"content_safety": "adult", "sponsored": True, "trusted": False},
    )
    IntegrityScorer().run(_ctx(hit))
    signals = hit.metadata["signals"]
    assert signals["safety"] == 0.0
    assert signals["privacy"] == pytest.approx(0.2)
    assert signals["fairness"] == pytest.approx(0.4)
    assert signals["news_trust"] == pytest.approx(0.2)
    assert signals["sponsored"] == 0.0
    assert len(hit.warnings) == 5  # safety, privacy, fairness, sponsored, news
    assert any(r.startswith("integrity:") for r in hit.reasons)


def test_integrity_scorer_no_network_or_clock() -> None:
    hit = _hit("c", "Plain", "text", {})
    first = IntegrityScorer().run(_ctx(hit))
    again = IntegrityScorer().run(_ctx(_hit("c", "Plain", "text", {})))
    assert first.results[0].final_score == again.results[0].final_score
    assert (
        first.results[0].metadata["signals"] == (again.results[0].metadata["signals"])
    )


# --- Ranker: new composite weights --------------------------------------------


def test_new_signal_weights_default_neutral_without_keyerror() -> None:
    ranker = Ranker()
    profile = get_profile("default")
    score = ranker.composite(profile, _hit("a", "Alpha", original=1.0))
    expected = 0.5 * (
        profile.safety_weight
        + profile.privacy_weight
        + profile.fairness_weight
        + profile.news_trust_weight
        + profile.sponsored_weight
    )
    # relevance 1.0 + legacy neutral offset 1.0 + new neutral offset.
    assert score == pytest.approx(1.0 + 1.0 + expected)


def test_sponsored_label_demotes_in_news_profile() -> None:
    ranker = Ranker()
    profile = get_profile("news")
    clean = _hit("clean", "Report", original=1.0, metadata={"signals": {}})
    paid = _hit("paid", "Report", original=1.0, metadata={"signals": {}})
    clean.metadata["signals"] = {
        "safety": 1.0,
        "privacy": 1.0,
        "fairness": 1.0,
        "news_trust": 1.0,
        "sponsored": 1.0,
    }
    paid.metadata["signals"] = {
        "safety": 1.0,
        "privacy": 1.0,
        "fairness": 1.0,
        "news_trust": 1.0,
        "sponsored": 0.0,
    }
    assert ranker.composite(profile, clean) > ranker.composite(profile, paid)


def test_child_safe_weights_exceed_default() -> None:
    safe = get_profile("child-safe")
    default = get_profile("default")
    for field in (
        "safety_weight",
        "privacy_weight",
        "fairness_weight",
        "sponsored_weight",
    ):
        assert getattr(safe, field) >= getattr(default, field)


def test_yaml_builtins_parity_with_new_weights() -> None:
    assert load_profiles("configs/ranking.yaml") == BUILTIN_PROFILES

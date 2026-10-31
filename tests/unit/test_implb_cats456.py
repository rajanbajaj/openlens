"""IMPL-B (cats 4-6): news/trust/shopping PARTIAL/MISSING upgrades.

Covers crawler-truth extractors (booking/style/size/occasion,
redirect chain/final URL/domain-age placeholder), the indexer
preservation list, attribute normalization, sponsored/rating hard
exclusion, typosquat + new-domain trust signals, redirect warnings,
opinion labels, claim spans, and story-group perspective gaps.
"""

from __future__ import annotations

from typing import Any

from openlens.core.context import SearchContext
from openlens.core.interfaces import SearchRequest, SearchResult
from openlens.core.registry import BUILTIN_PLUGINS, PluginRegistry
from openlens.indexing import crawler as crawler_mod
from openlens.indexing.crawler import (
    PoliteCrawler,
    _FetchedPage,
    extract_page_attributes,
)
from openlens.indexing.indexer import RANKING_METADATA_KEYS
from openlens.plugins.attribute_enricher import AttributeEnricher
from openlens.plugins.news_reranker import NewsReranker
from openlens.plugins.opinion_detector import OpinionDetector, classify_opinion
from openlens.plugins.trust_enricher import TrustEnricher
from openlens.plugins.trust_scorer import (
    TrustScorer,
    is_typosquat,
    levenshtein_distance,
)
from openlens.plugins.user_restrictions import UserRestrictionsFilter
from openlens.retrieval.claim_extract import extract_claims


def _hit(
    hit_id: str = "h",
    title: str = "a title",
    snippet: str = "a snippet",
    url: str | None = "http://unknown.example/x",
    metadata: dict[str, Any] | None = None,
) -> SearchResult:
    return SearchResult(
        id=hit_id,
        title=title,
        snippet=snippet,
        original_score=1.0,
        final_score=1.0,
        url=url,
        provider="test",
        metadata=dict(metadata or {}),
    )


def _ctx(*hits: SearchResult, filters: Any = None) -> SearchContext:
    request = SearchRequest(query="q")
    if filters is not None:
        request.filters = filters
    ctx = SearchContext(request=request)
    ctx.results = list(hits)
    return ctx


# --- crawler truth -------------------------------------------------------


def test_crawler_extracts_booking_style_size_occasion() -> None:
    html = (
        "<html><head>"
        '<script type="application/ld+json">{"@type": "Product",'
        '"brand": "Acme", "size": "M", "color": "Blue",'
        '"audience": {"audienceType": "casual"}}'
        "</script></head><body>"
        '<a href="/booking/room-1" rel="booking">Reserve</a>'
        "</body></html>"
    )
    attrs = extract_page_attributes(html)
    assert attrs["booking_url"] == "/booking/room-1"
    assert attrs["brand"] == "Acme"
    assert attrs["size"] == "M"
    assert attrs["style"] == "Blue"
    assert attrs["occasion"] == "casual"


def test_crawler_truth_keys_include_new_fields() -> None:
    for key in (
        "final_url",
        "redirect_chain",
        "domain_age_days",
        "booking_url",
        "style",
        "size",
        "occasion",
    ):
        assert key in crawler_mod.EXTRACTED_METADATA_KEYS


def test_to_document_records_redirect_chain_and_final_url() -> None:
    crawler = PoliteCrawler()
    page = _FetchedPage(
        status=200,
        url="https://final.example/article",
        content_type="text/html",
        body=b"<html><head><title>T</title></head><body>Hi</body></html>",
        requested_url="http://start.example/article",
        redirect_chain=["http://start.example/article", "https://mid.example/article"],
    )
    doc = crawler._to_document(page)
    assert doc.metadata["redirect_chain"] == [
        "http://start.example/article",
        "https://mid.example/article",
    ]
    assert doc.metadata["final_url"] == "https://final.example/article"
    assert "domain_age_days" not in doc.metadata


def test_to_document_resolves_relative_booking_url() -> None:
    crawler = PoliteCrawler()
    page = _FetchedPage(
        status=200,
        url="https://hotel.example/rooms",
        content_type="text/html",
        body=(
            b"<html><head><title>H</title></head><body>"
            b'<a href="/book/101">Book now</a></body></html>'
        ),
    )
    doc = crawler._to_document(page)
    assert doc.metadata["booking_url"] == "https://hotel.example/book/101"


def test_indexer_preserves_new_ranking_keys() -> None:
    for key in (
        "final_url",
        "redirect_chain",
        "domain_age_days",
        "booking_url",
        "style",
        "size",
        "occasion",
    ):
        assert key in RANKING_METADATA_KEYS


# --- attribute enricher --------------------------------------------------


def test_attribute_enricher_normalizes_rating_and_shopping_details() -> None:
    out = AttributeEnricher().run(
        _ctx(
            _hit(
                metadata={
                    "rating": "4.5",
                    "rating_count": "12",
                    "brand": "  Acme ",
                    "booking_url": "https://hotel.example/book/1",
                    "style": "Blue",
                    "size": "M",
                    "occasion": "casual",
                }
            )
        )
    )
    meta = out.results[0].metadata
    assert meta["rating"] == 4.5
    assert meta["rating_count"] == 12
    assert meta["brand"] == "Acme"
    assert meta["booking_url"] == "https://hotel.example/book/1"
    assert meta["style"] == "Blue"
    assert meta["size"] == "M"
    assert meta["occasion"] == "casual"


# --- sponsored / rating hard exclusion -----------------------------------


def test_exclude_sponsored_removes_truth_flagged_hits() -> None:
    out = UserRestrictionsFilter().run(
        _ctx(
            _hit("s", metadata={"sponsored": True}),
            _hit("k"),
            filters={"exclude_sponsored": True},
        )
    )
    assert [hit.id for hit in out.results] == ["k"]
    assert out.metadata["user-restrictions-removed"] == 1


def test_min_rating_removes_low_rated_hits() -> None:
    out = UserRestrictionsFilter().run(
        _ctx(
            _hit("low", metadata={"rating": 2.0}),
            _hit("high", metadata={"rating": 4.5}),
            _hit("none"),
            filters={"min_rating": 4},
        )
    )
    assert [hit.id for hit in out.results] == ["high"]


def test_brand_style_size_occasion_keys_hard_exclusion() -> None:
    filters = {"brand": "Acme", "style": "casual", "size": "M", "occasion": "party"}
    matching = _hit(
        "h1",
        metadata={"brand": "Acme", "style": "casual", "size": "M", "occasion": "party"},
    )
    out = UserRestrictionsFilter().run(_ctx(matching, _hit("h2"), filters=filters))
    assert [hit.id for hit in out.results] == ["h1"]
    assert out.metadata["user-restrictions-removed"] == 1


# --- trust: typosquat + new domain ---------------------------------------


def test_levenshtein_and_typosquat_helpers() -> None:
    assert levenshtein_distance("safe", "safe") == 0
    assert levenshtein_distance("safe", "saffe") == 1
    assert levenshtein_distance("safe", "other") > 1
    assert is_typosquat("saffe.example", frozenset({"safe.example"})) == "safe.example"
    assert is_typosquat("safe.example", frozenset({"safe.example"})) is None
    assert is_typosquat("unrelated.example", frozenset({"safe.example"})) is None


def test_trust_scorer_penalizes_new_and_lookalike_domains() -> None:
    config: dict[str, Any] = {
        "reputation": {"trusted": ["safe.example"], "distrusted": []}
    }
    fresh = TrustScorer(config).run(
        _ctx(_hit(url="http://unknown.example/a", metadata={"domain_age_days": 5}))
    )
    assert any("newly registered" in reason for reason in fresh.results[0].reasons)
    aged = TrustScorer(config).run(
        _ctx(_hit(url="http://unknown.example/a", metadata={"domain_age_days": 400}))
    )
    assert any("domain age 400d" in reason for reason in aged.results[0].reasons)
    spoof = TrustScorer(config).run(_ctx(_hit(url="http://saffe.example/a")))
    assert any("typosquat" in reason for reason in spoof.results[0].reasons)


def test_trust_enricher_warns_on_redirect_chain_and_new_domain() -> None:
    enricher = TrustEnricher({"reputation": {"trusted": [], "distrusted": []}})
    out = enricher.run(
        _ctx(
            _hit(
                url="https://final.example/article",
                metadata={
                    "redirect_chain": [
                        "http://start.example/article",
                        "https://final.example/article",
                    ],
                    "final_url": "https://final.example/article",
                    "domain_age_days": 3,
                },
            )
        )
    )
    hit = out.results[0]
    assert any("Redirected across domains" in warning for warning in hit.warnings)
    assert any("Newly registered domain" in warning for warning in hit.warnings)
    assert hit.metadata["redirect_hops"] == 2


# --- news: opinion, claims, perspective gaps ------------------------------


def test_opinion_detector_labels_editorial_and_factual() -> None:
    assert classify_opinion("T", "S", "Opinion")[0] == "opinion"
    label, _ = classify_opinion(
        "Budget vote fails",
        "The council said the budget was rejected 5-2.",
        "news",
    )
    assert label == "fact"
    out = OpinionDetector().run(
        _ctx(
            _hit(
                title="Our view: the plan must be stopped", snippet="We argue it fails."
            )
        )
    )
    assert out.results[0].metadata["opinion"] == "opinion"


def test_claim_extraction_picks_assertion_sentences() -> None:
    claims = extract_claims(
        "Council approves budget",
        "The council approved the budget 5-2 on Tuesday. Amazing stuff indeed!",
    )
    assert claims and any("council approved" in claim.lower() for claim in claims)
    assert extract_claims("Hi", "Wow!") == []


def test_news_reranker_reports_missing_perspectives_and_claims() -> None:
    def hit(
        hit_id: str,
        title: str,
        perspective: str,
        published: str = "2026-01-10",
    ) -> SearchResult:
        meta: dict[str, Any] = {"published": published, "perspective": perspective}
        return _hit(
            hit_id,
            title=title,
            snippet="The council approved the budget 5-2.",
            metadata=meta,
        )

    out = NewsReranker({}).run(
        _ctx(
            hit("a1", "Harbor bridge reopening delayed again", "supporting"),
            hit("a2", "Harbor bridge reopening delayed today", "supporting"),
            hit(
                "b1",
                "Council approves downtown stadium funding",
                "critical",
                published="2026-02-01",
            ),
        )
    )
    groups = out.metadata["story_groups"]
    assert len(groups) == 1
    story = next(iter(groups.values()))
    assert story["missing_perspectives"] == ["critical"]
    assert any("missing perspectives in" in reason for reason in out.results[0].reasons)
    assert out.results[0].metadata.get("key_claims")


def test_opinion_detector_registered_as_builtin() -> None:
    assert BUILTIN_PLUGINS["opinion-detector"] == (
        "openlens.plugins.opinion_detector:OpinionDetector"
    )
    plugin = PluginRegistry().create("opinion-detector", {})
    assert isinstance(plugin, OpinionDetector)

"""Vertical hard filters: travel budget gate + book/food metadata filters.

Covers the IMPL-C additions to ``user-restrictions`` (``exclude_over_budget``,
``license``, ``author``, ``cuisine``, ``dietary_tags``, ``ingredients``,
``exclude_allergens``) and the matching ``attribute-enricher`` normalizers
(``license``/``author``/``cuisine``/``isbn``/``publisher`` text fields and
``ingredients``/``dietary_tags``/``allergens`` tag lists).

Reserved keys stay lenient: malformed values never fail closed.
"""

from __future__ import annotations

from openlens.core.context import SearchContext
from openlens.core.interfaces import SearchRequest, SearchResult
from openlens.plugins.attribute_enricher import AttributeEnricher
from openlens.plugins.user_restrictions import UserRestrictionsFilter


def _hit(hit_id: str, metadata: dict | None = None) -> SearchResult:
    return SearchResult(
        id=hit_id,
        title=f"hit {hit_id}",
        snippet="a snippet",
        original_score=1.0,
        final_score=1.0,
        url="https://example.com/a",
        provider="test",
        metadata=dict(metadata or {}),
    )


def _run(filters: dict, hits: list[SearchResult]) -> list[str]:
    plugin = UserRestrictionsFilter({})
    ctx = SearchContext(request=SearchRequest(query="q", filters=dict(filters)))
    ctx.results = list(hits)
    return [r.id for r in plugin.run(ctx).results]


def _cheap() -> SearchResult:
    return _hit("cheap", {"price": 100.0, "currency": "USD"})


def _pricey() -> SearchResult:
    return _hit("pricey", {"price": 500.0, "currency": "USD"})


def _priceless() -> SearchResult:
    return _hit("priceless", {})


# ---------------------------------------------------------------------------
# exclude_over_budget: max_price stays ordering-only unless opted in
# ---------------------------------------------------------------------------


def test_max_price_alone_never_excludes() -> None:
    assert _run({"max_price": 150}, [_cheap(), _pricey()]) == ["cheap", "pricey"]


def test_exclude_over_budget_drops_over_cap() -> None:
    filters = {"max_price": 150, "exclude_over_budget": True}
    assert _run(filters, [_cheap(), _pricey()]) == ["cheap"]


def test_exclude_over_budget_keeps_missing_price() -> None:
    filters = {"max_price": 150, "exclude_over_budget": True}
    assert _run(filters, [_pricey(), _priceless()]) == ["priceless"]


def test_exclude_over_budget_currency_mismatch_keeps() -> None:
    filters = {
        "max_price": {"value": 150, "currency": "EUR"},
        "exclude_over_budget": True,
    }
    assert _run(filters, [_pricey()]) == ["pricey"]


def test_exclude_over_budget_malformed_cap_ignores() -> None:
    filters = {"max_price": "not-a-number", "exclude_over_budget": True}
    assert _run(filters, [_cheap(), _pricey()]) == ["cheap", "pricey"]


# ---------------------------------------------------------------------------
# Book filters: license / author
# ---------------------------------------------------------------------------


def test_license_filter_matches_case_insensitively() -> None:
    filters = {"license": "cc-by"}
    hits = [_hit("open", {"license": "CC-BY"}), _hit("closed", {"license": "MIT"})]
    assert _run(filters, hits) == ["open"]


def test_license_filter_drops_missing_license() -> None:
    assert _run({"license": "CC-BY"}, [_hit("bare", {})]) == []


def test_author_filter_is_substring_match() -> None:
    filters = {"author": "austen"}
    hits = [
        _hit("match", {"author": "Jane Austen"}),
        _hit("other", {"author": "Mark Twain"}),
        _hit("bare", {}),
    ]
    assert _run(filters, hits) == ["match"]


# ---------------------------------------------------------------------------
# Food filters: cuisine / dietary_tags / ingredients / exclude_allergens
# ---------------------------------------------------------------------------


def _recipe(hit_id: str) -> SearchResult:
    return _hit(
        hit_id,
        {
            "cuisine": "Italian",
            "dietary_tags": ["Vegan", "Gluten-Free"],
            "ingredients": ["Tomato Sauce", "Fresh Basil"],
            "allergens": ["peanuts"],
        },
    )


def test_cuisine_filter_matches_case_insensitively() -> None:
    assert _run({"cuisine": "italian"}, [_recipe("r")]) == ["r"]
    assert _run({"cuisine": "mexican"}, [_recipe("r")]) == []


def test_dietary_filter_requires_every_requested_tag() -> None:
    assert _run({"dietary_tags": ["vegan"]}, [_recipe("r")]) == ["r"]
    assert _run({"dietary_tags": ["vegan", "kosher"]}, [_recipe("r")]) == []


def test_dietary_filter_accepts_comma_string() -> None:
    assert _run({"dietary_tags": "vegan, gluten-free"}, [_recipe("r")]) == ["r"]


def test_ingredients_filter_is_substring_match() -> None:
    assert _run({"ingredients": ["basil"]}, [_recipe("r")]) == ["r"]
    assert _run({"ingredients": ["basil", "chocolate"]}, [_recipe("r")]) == []
    assert _run({"ingredients": ["basil"]}, [_hit("bare", {})]) == []


def test_exclude_allergens_drops_matches_keeps_missing() -> None:
    assert _run({"exclude_allergens": ["peanuts"]}, [_recipe("r")]) == []
    assert _run({"exclude_allergens": ["shellfish"]}, [_recipe("r")]) == ["r"]
    assert _run({"exclude_allergens": ["peanuts"]}, [_hit("bare", {})]) == ["bare"]


def test_malformed_vertical_values_are_ignored() -> None:
    filters = {"license": 123, "dietary_tags": 42, "exclude_over_budget": "maybe"}
    assert _run(filters, [_recipe("r")]) == ["r"]


# ---------------------------------------------------------------------------
# Attribute enricher: book/food normalizers
# ---------------------------------------------------------------------------


def _enrich(metadata: dict) -> dict:
    ctx = SearchContext(request=SearchRequest(query="q"))
    ctx.results = [_hit("h", dict(metadata))]
    AttributeEnricher({}).run(ctx)
    return ctx.results[0].metadata


def test_enricher_normalizes_book_fields() -> None:
    meta = _enrich({"license": "  CC-BY  ", "author": "Jane Austen"})
    assert meta["license"] == "CC-BY"
    assert meta["author"] == "Jane Austen"
    assert meta["isbn"] is None
    assert meta["publisher"] is None


def test_enricher_normalizes_food_tag_lists() -> None:
    meta = _enrich(
        {
            "cuisine": "  Italian ",
            "ingredients": ["Tomato", " Basil "],
            "dietary_tags": "Vegan, Gluten-Free",
            "allergens": ["Peanuts"],
        }
    )
    assert meta["cuisine"] == "Italian"
    assert meta["ingredients"] == ["tomato", "basil"]
    assert meta["dietary_tags"] == ["vegan", "gluten-free"]
    assert meta["allergens"] == ["peanuts"]


def test_enricher_food_missing_defaults_to_empty() -> None:
    meta = _enrich({})
    assert meta["ingredients"] == []
    assert meta["dietary_tags"] == []
    assert meta["allergens"] == []
    assert meta["license"] is None
    assert meta["cuisine"] is None

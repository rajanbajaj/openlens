"""Regression tests for TRACK RANK gaps: sponsored truthiness, brand filter,
site_category filter, and shopping/travel sponsored weights.

Covers section-1 items #2-backend/#3/#4-backend (sponsored end-to-end,
brand/style/size/occasion + site_category hard exclusion) and the
configs/profiles sponsored_weight raise.
"""

from __future__ import annotations

import pytest

from openlens.core.context import SearchContext
from openlens.core.interfaces import PluginError, SearchRequest, SearchResult
from openlens.plugins._signals import (
    coerce_sponsored_marker,
    is_sponsored_hit,
    is_truthy_flag,
)
from openlens.plugins.integrity_scorer import sponsored_of
from openlens.plugins.user_restrictions import UserRestrictionsFilter
from openlens.ranking.profiles import BUILTIN_PROFILES


def _hit(hit_id: str, metadata: dict | None = None) -> SearchResult:
    return SearchResult(
        id=hit_id,
        title="clean title",
        snippet="a snippet",
        original_score=1.0,
        final_score=1.0,
        url="https://example.com/a",
        provider="test",
        metadata=dict(metadata or {}),
    )


def _context(filters: dict, hits: list[SearchResult]) -> SearchContext:
    ctx = SearchContext(request=SearchRequest(query="q", filters=dict(filters)))
    ctx.results = list(hits)
    return ctx


def _ids(ctx: SearchContext) -> list[str]:
    return [r.id for r in ctx.results]


# ---------------------------------------------------------------------------
# Shared truthiness helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value", [True, 1, 1.0, "true", "True", " TRUE ", "1", "yes", "Yes", "YES"]
)
def test_is_truthy_flag_accepts_truthy_spellings(value):
    assert is_truthy_flag(value) is True


@pytest.mark.parametrize(
    "value",
    [False, 0, 0.0, None, "false", "no", "0", "", "truthy", [], {}, 2, "on"],
)
def test_is_truthy_flag_rejects_everything_else(value):
    assert is_truthy_flag(value) is False


@pytest.mark.parametrize(
    "metadata",
    [
        {"sponsored": True},
        {"sponsored": 1},
        {"sponsored": "true"},
        {"sponsored": "Yes"},
        {"is_sponsored": True},
        {"is_sponsored": "yes"},
    ],
)
def test_is_sponsored_hit_flags_canonical_and_provider_markers(metadata):
    assert is_sponsored_hit(metadata) is True


def test_is_sponsored_hit_rejects_absent_or_falsy():
    assert is_sponsored_hit({}) is False
    assert is_sponsored_hit({"sponsored": False}) is False
    assert is_sponsored_hit({"sponsored": "no"}) is False
    assert is_sponsored_hit({"is_sponsored": 0}) is False


def test_coerce_sponsored_marker_canonicalizes_to_real_true():
    metadata: dict = {"is_sponsored": "yes"}
    assert coerce_sponsored_marker(metadata) is True
    assert metadata["sponsored"] is True


def test_coerce_sponsored_marker_leaves_organic_hits_alone():
    metadata: dict = {"sponsored": False}
    assert coerce_sponsored_marker(metadata) is False
    assert metadata["sponsored"] is False


# ---------------------------------------------------------------------------
# (a) Sponsored truthiness end to end: filter + scorer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("marker", [1, "true", "yes", "YES", True])
def test_exclude_sponsored_removes_truthy_markers(marker):
    ctx = _context(
        filters={"exclude_sponsored": True},
        hits=[
            _hit("ad", metadata={"sponsored": marker}),
            _hit("provider-ad", metadata={"is_sponsored": marker}),
            _hit("organic", metadata={}),
        ],
    )
    out = UserRestrictionsFilter().run(ctx)
    assert _ids(out) == ["organic"]


def test_exclude_sponsored_canonicalizes_marker_for_downstream():
    hit = _hit("ad", metadata={"sponsored": "yes"})
    UserRestrictionsFilter().run(_context(filters={}, hits=[hit]))
    assert hit.metadata["sponsored"] is True


@pytest.mark.parametrize("marker", [1, "true", "yes", True])
def test_integrity_scorer_demotes_truthy_sponsored(marker):
    score, warning = sponsored_of({"sponsored": marker})
    assert score == 0.0
    assert warning is not None


def test_integrity_scorer_demotes_provider_marker():
    score, warning = sponsored_of({"is_sponsored": "yes"})
    assert score == 0.0
    assert warning is not None


def test_integrity_scorer_keeps_organic_at_full():
    score, warning = sponsored_of({})
    assert score == 1.0
    assert warning is None


# ---------------------------------------------------------------------------
# (c) brand / style / size / occasion hard exclusion
# ---------------------------------------------------------------------------


def test_brand_filter_keeps_exact_match_case_insensitive():
    ctx = _context(
        filters={"brand": "Acme"},
        hits=[
            _hit("match", metadata={"brand": "acme"}),
            _hit("other", metadata={"brand": "Globex"}),
            _hit("unmarked", metadata={}),
        ],
    )
    assert _ids(UserRestrictionsFilter().run(ctx)) == ["match"]


def test_brand_filter_accepts_list_and_matches_all_terms():
    ctx = _context(
        filters={"brand": ["Acme", "Outdoor"]},
        hits=[
            _hit("both", metadata={"brand": "Acme, Outdoor"}),
            _hit("one", metadata={"brand": "Acme"}),
        ],
    )
    assert _ids(UserRestrictionsFilter().run(ctx)) == ["both"]


def test_style_filter_matches_comma_joined_metadata_terms():
    ctx = _context(
        filters={"style": "red"},
        hits=[
            _hit("match", metadata={"style": "red, cotton"}),
            _hit("other", metadata={"style": "blue, wool"}),
        ],
    )
    assert _ids(UserRestrictionsFilter().run(ctx)) == ["match"]


def test_size_and_occasion_filters_exclude_non_matching():
    ctx = _context(
        filters={"size": "M", "occasion": "wedding"},
        hits=[
            _hit("match", metadata={"size": "m", "occasion": "Wedding"}),
            _hit("wrong-size", metadata={"size": "L", "occasion": "wedding"}),
            _hit("unmarked", metadata={}),
        ],
    )
    assert _ids(UserRestrictionsFilter().run(ctx)) == ["match"]


def test_product_filters_malformed_values_are_lenient_passthrough():
    ctx = _context(
        filters={"brand": 42, "style": {"red": True}, "size": [], "occasion": [None]},
        hits=[_hit("h1"), _hit("h2")],
    )
    assert _ids(UserRestrictionsFilter().run(ctx)) == ["h1", "h2"]


# ---------------------------------------------------------------------------
# (d) site_category: hard exclusion, fail-closed unknowns, lenient malformed
# ---------------------------------------------------------------------------


def test_site_category_keeps_intersecting_hits_only():
    ctx = _context(
        filters={"site_category": ["kids"]},
        hits=[
            _hit("kid", metadata={"site_category": "kids"}),
            _hit("multi", metadata={"site_category": ["news", "kids"]}),
            _hit("shop", metadata={"site_category": "shopping"}),
            _hit("unmarked", metadata={}),
        ],
    )
    assert _ids(UserRestrictionsFilter().run(ctx)) == ["kid", "multi"]


def test_site_category_accepts_bare_string_and_falls_back_to_category():
    ctx = _context(
        filters={"site_category": "News"},
        hits=[
            _hit("hit", metadata={"category": "news"}),
            _hit("miss", metadata={"category": "other"}),
        ],
    )
    assert _ids(UserRestrictionsFilter().run(ctx)) == ["hit"]


def test_site_category_unknown_value_is_fail_closed():
    ctx = _context(
        filters={"site_category": "gaming"},
        hits=[_hit("h1", metadata={"site_category": "gaming"})],
    )
    with pytest.raises(PluginError):
        UserRestrictionsFilter().run(ctx)


def test_site_category_unknown_value_in_list_is_fail_closed():
    ctx = _context(
        filters={"site_category": ["news", "gaming"]},
        hits=[_hit("h1")],
    )
    with pytest.raises(PluginError):
        UserRestrictionsFilter().run(ctx)


@pytest.mark.parametrize(
    "value", [42, {"shopping": True}, [], ["", "  "], [None], True]
)
def test_site_category_malformed_values_are_lenient_passthrough(value):
    ctx = _context(filters={"site_category": value}, hits=[_hit("h1")])
    assert _ids(UserRestrictionsFilter().run(ctx)) == ["h1"]


# ---------------------------------------------------------------------------
# (b) shopping/travel sponsored_weight raise
# ---------------------------------------------------------------------------


def test_shopping_and_travel_sponsored_weight_matches_news():
    assert BUILTIN_PROFILES["shopping"].sponsored_weight == (
        BUILTIN_PROFILES["news"].sponsored_weight
    )
    assert BUILTIN_PROFILES["travel"].sponsored_weight == (
        BUILTIN_PROFILES["news"].sponsored_weight
    )
    assert BUILTIN_PROFILES["shopping"].sponsored_weight == 1.0

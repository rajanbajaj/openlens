"""Unit tests for the user-restrictions filter (filter-pack Step 3).

Covers every owned operator (must / must-not terms, include / exclude
domain rules), literal escaping of user terms, both pinned raise sites
(ConfigError at configure(), PluginError(mandatory=True) per request),
unknown-key rejection, and the D4 reserved-key pass-through.
"""

from __future__ import annotations

import pytest

from openlens.core.context import SearchContext
from openlens.core.interfaces import (
    ConfigError,
    PipelineStage,
    PluginError,
    PluginType,
    SearchRequest,
    SearchResult,
)
from openlens.core.pipeline import Pipeline
from openlens.plugins.user_restrictions import UserRestrictionsFilter


def _hit(
    hit_id: str,
    title: str = "clean title",
    snippet: str = "a snippet",
    url: str = "https://example.com/a",
) -> SearchResult:
    return SearchResult(
        id=hit_id,
        title=title,
        snippet=snippet,
        original_score=1.0,
        final_score=1.0,
        url=url,
        provider="test",
    )


def _context(
    filters: dict | None = None, hits: list[SearchResult] | None = None
) -> SearchContext:
    ctx = SearchContext(request=SearchRequest(query="q", filters=dict(filters or {})))
    ctx.results = (
        list(hits)
        if hits is not None
        else [_hit("h1"), _hit("h2", title="python guide", snippet="learn python")]
    )
    return ctx


def _ids(ctx: SearchContext) -> list[str]:
    return [r.id for r in ctx.results]


# ---------------------------------------------------------------------------
# Plugin declaration
# ---------------------------------------------------------------------------


def test_declaration_is_mandatory_filter_at_filtering_stage():
    plugin = UserRestrictionsFilter()
    assert plugin.name == "user-restrictions"
    assert plugin.plugin_type is PluginType.FILTER
    assert plugin.stage is PipelineStage.FILTERING
    assert plugin.mandatory is True


def test_no_filters_is_a_passthrough_that_still_records_evidence():
    ctx = _context()
    out = UserRestrictionsFilter().run(ctx)
    assert _ids(out) == ["h1", "h2"]
    assert "user-restrictions" in out.applied_filters
    assert out.metadata["user-restrictions-removed"] == 0


# ---------------------------------------------------------------------------
# Operators: must / must-not terms
# ---------------------------------------------------------------------------


def test_must_requires_every_term():
    ctx = _context(
        filters={"must": ["python", "guide"]},
        hits=[
            _hit("both", title="python guide"),
            _hit("one", title="python tips"),
            _hit("none", title="cooking guide"),
        ],
    )
    assert _ids(UserRestrictionsFilter().run(ctx)) == ["both"]


def test_must_matches_case_insensitively_across_title_snippet_url():
    ctx = _context(
        filters={"must": ["PYTHON"]},
        hits=[
            _hit("title", title="Python news"),
            _hit("snippet", snippet="all about python"),
            _hit("url", url="https://example.com/python"),
            _hit("miss", title="java news"),
        ],
    )
    assert _ids(UserRestrictionsFilter().run(ctx)) == ["title", "snippet", "url"]


def test_must_not_removes_any_matching_term():
    ctx = _context(
        filters={"must_not": ["sponsored", "casino"]},
        hits=[
            _hit("keep", title="honest review"),
            _hit("title-hit", title="sponsored post"),
            _hit("snippet-hit", snippet="best casino bonuses"),
        ],
    )
    out = UserRestrictionsFilter().run(ctx)
    assert _ids(out) == ["keep"]
    assert out.metadata["user-restrictions-removed"] == 2


def test_must_and_must_not_combine():
    ctx = _context(
        filters={"must": ["shoes"], "must_not": ["fake"]},
        hits=[
            _hit("keep", title="real shoes"),
            _hit("wrong-term", title="real hats"),
            _hit("banned-term", title="fake shoes"),
        ],
    )
    assert _ids(UserRestrictionsFilter().run(ctx)) == ["keep"]


def test_terms_are_literals_not_regexes():
    ctx = _context(
        filters={"must_not": [".*"]},
        hits=[
            _hit("plain", title="ordinary result"),
            _hit("literal", title="matches .* literally"),
        ],
    )
    assert _ids(UserRestrictionsFilter().run(ctx)) == ["plain"]


def test_dotted_term_does_not_act_as_wildcard():
    ctx = _context(
        filters={"must": ["a.b"]},
        hits=[_hit("dot", title="the a.b file"), _hit("any", title="the aXb file")],
    )
    assert _ids(UserRestrictionsFilter().run(ctx)) == ["dot"]


def test_synonym_keys_share_the_canonical_operators():
    ctx = _context(
        filters={"must_terms": ["python"], "blocked_domains": ["spam.test"]},
        hits=[
            _hit("keep", title="python", url="https://example.com/x"),
            _hit("spam", title="python", url="https://spam.test/x"),
            _hit("off-topic", title="java", url="https://example.com/y"),
        ],
    )
    assert _ids(UserRestrictionsFilter().run(ctx)) == ["keep"]


# ---------------------------------------------------------------------------
# Operators: domain rules (parsed host, exact-or-subdomain)
# ---------------------------------------------------------------------------


def test_include_domains_keeps_only_listed_hosts_and_subdomains():
    ctx = _context(
        filters={"include_domains": ["example.com"]},
        hits=[
            _hit("apex", url="https://example.com/a"),
            _hit("sub", url="https://docs.example.com/a"),
            _hit("other", url="https://other.test/a"),
        ],
    )
    assert _ids(UserRestrictionsFilter().run(ctx)) == ["apex", "sub"]


def test_include_domains_parses_port_and_userinfo_to_bare_host():
    ctx = _context(
        filters={"include_domains": ["example.com"]},
        hits=[
            _hit("port", url="https://example.com:8443/a"),
            _hit("userinfo", url="https://user:secret@example.com/a"),
        ],
    )
    assert _ids(UserRestrictionsFilter().run(ctx)) == ["port", "userinfo"]


def test_include_domains_removes_hit_without_url():
    ctx = _context(
        filters={"include_domains": ["example.com"]},
        hits=[_hit("no-url", url=None), _hit("ok", url="https://example.com/a")],
    )
    assert _ids(UserRestrictionsFilter().run(ctx)) == ["ok"]


def test_exclude_domains_removes_listed_hosts_and_subdomains():
    ctx = _context(
        filters={"exclude_domains": ["spam.test"]},
        hits=[
            _hit("keep", url="https://example.com/a"),
            _hit("apex", url="https://spam.test/a"),
            _hit("sub", url="https://ads.spam.test/a"),
        ],
    )
    out = UserRestrictionsFilter().run(ctx)
    assert _ids(out) == ["keep"]
    assert out.metadata["user-restrictions-removed"] == 2


@pytest.mark.parametrize(
    "url",
    ["https://evil-example.com/a", "https://example.com.evil.test/a"],
)
def test_exclude_domains_has_no_substring_false_positives(url: str):
    ctx = _context(
        filters={"exclude_domains": ["example.com"]}, hits=[_hit("h", url=url)]
    )
    assert _ids(UserRestrictionsFilter().run(ctx)) == ["h"]


# ---------------------------------------------------------------------------
# Config defaults + pinned raise site 1: ConfigError at configure()
# ---------------------------------------------------------------------------


def test_config_defaults_apply_to_every_request_and_merge():
    plugin = UserRestrictionsFilter({"must_not": ["sponsored"]})
    ctx = _context(
        filters={"must_not": ["casino"]},
        hits=[
            _hit("keep", title="honest review"),
            _hit("config-term", title="sponsored post"),
            _hit("request-term", title="casino night"),
        ],
    )
    assert _ids(plugin.run(ctx)) == ["keep"]


@pytest.mark.parametrize(
    "config",
    [
        {"must": "python"},
        {"must": [123]},
        {"must_not": ["ok", None]},
        {"include_domains": "example.com"},
        {"exclude_domains": [42]},
        {"must": [""]},
    ],
)
def test_malformed_config_lists_raise_config_error_at_configure(config: dict):
    with pytest.raises(ConfigError):
        UserRestrictionsFilter(config)
    with pytest.raises(ConfigError):
        UserRestrictionsFilter().configure(config)


# ---------------------------------------------------------------------------
# Pinned raise site 2: PluginError(mandatory=True) per request
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filters",
    [
        {"must": "python"},
        {"must": [123]},
        {"must": ["   "]},
        {"must_not": "casino"},
        {"include_domains": [None]},
        {"exclude_domains": "spam.test"},
        {"must": ["python"] * 51},
    ],
)
def test_malformed_request_filters_raise_mandatory_plugin_error(filters: dict):
    ctx = _context(filters=filters)
    with pytest.raises(PluginError) as excinfo:
        UserRestrictionsFilter().run(ctx)
    assert excinfo.value.mandatory is True
    assert excinfo.value.plugin_name == "user-restrictions"
    # Fail closed: validation happens before any hit is filtered.
    assert _ids(ctx) == ["h1", "h2"]


def test_unknown_filter_key_is_rejected_fail_closed():
    ctx = _context(filters={"sponsored": True})
    with pytest.raises(PluginError) as excinfo:
        UserRestrictionsFilter().run(ctx)
    assert excinfo.value.mandatory is True
    assert "sponsored" in str(excinfo.value)


def test_malformed_filters_propagate_through_pipeline():
    ctx = _context(filters={"definitely_unknown": []})
    with pytest.raises(PluginError) as excinfo:
        Pipeline([UserRestrictionsFilter()]).run(ctx)
    assert excinfo.value.mandatory is True


def test_simulate_outage_fails_closed():
    ctx = _context(filters={"must": ["python"]})
    with pytest.raises(PluginError) as excinfo:
        UserRestrictionsFilter({"simulate_outage": True}).run(ctx)
    assert excinfo.value.mandatory is True
    assert _ids(ctx) == ["h1", "h2"]


# ---------------------------------------------------------------------------
# D4 reserved-key pass-through (never consumed, never rejected)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filters",
    [
        {"prefer_domains": ["other.test"]},
        {"prefer_topics": ["unrelated"]},
        {"prefer_anything_at_all": 12345},
        {"max_price": 1},
        {"location": "somewhere-else"},
        {"facilities": ["helipad", "moat"]},
    ],
)
def test_reserved_keys_pass_through_without_filtering(filters: dict):
    ctx = _context(filters=filters)
    out = UserRestrictionsFilter().run(ctx)
    assert _ids(out) == ["h1", "h2"]
    assert out.metadata["user-restrictions-removed"] == 0
    # The consumer keys survive untouched for the scoring/rerank packs.
    assert out.request.filters == filters


def test_facilities_pass_through_explicitly():
    # facilities belongs to the rerank pack (D4 reservation): hits are
    # kept even though none of them advertises the requested facilities.
    ctx = _context(filters={"facilities": ["wifi", "pool"]})
    out = UserRestrictionsFilter().run(ctx)
    assert _ids(out) == ["h1", "h2"]
    assert out.request.filters["facilities"] == ["wifi", "pool"]
    assert out.metadata["user-restrictions-removed"] == 0


def test_owned_rules_apply_alongside_reserved_keys():
    filters = {
        "must_not": ["python"],
        "facilities": ["wifi"],
        "max_price": 10,
        "location": "paris",
        "prefer_topics": ["python"],
    }
    ctx = _context(filters=filters)
    out = UserRestrictionsFilter().run(ctx)
    assert _ids(out) == ["h1"]
    for key in ("facilities", "max_price", "location", "prefer_topics"):
        assert key in out.request.filters

"""Privacy filter tests: each PII class x {redact, exclude}.

Covers plan Step 1 — PrivacyFilter is a mandatory FILTER at FILTERING,
fail-closed, with per-class config patterns (street addresses only from
config) and one compiled alternation per class.
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
from openlens.plugins.privacy_filter import PrivacyFilter

# Explicit per-class patterns pinned in config for the tests. Street
# address uses an explicit config pattern — no invented grammar.
PATTERNS: dict[str, str] = {
    "email": r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}",
    "phone": r"\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b",
    "government_id": r"\b\d{3}-\d{2}-\d{4}\b",
    "street_address": r"\b\d+\s+Main\s+Street\b",
}

SAMPLE_SNIPPETS: dict[str, str] = {
    "email": "Contact john.doe@example.com for details",
    "phone": "Call 555-123-4567 for details",
    "government_id": "SSN 123-45-6789 on file",
    "street_address": "Meet at 123 Main Street today",
}

PII_STRINGS: dict[str, str] = {
    "email": "john.doe@example.com",
    "phone": "555-123-4567",
    "government_id": "123-45-6789",
    "street_address": "123 Main Street",
}

ALL_CLASSES = ["email", "phone", "government_id", "street_address"]


def _hit(
    hit_id: str = "h1",
    title: str = "clean title",
    snippet: str = "a clean snippet",
    url: str = "https://example.com/a",
    metadata: dict | None = None,
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


def _context(results: list[SearchResult]) -> SearchContext:
    ctx = SearchContext(request=SearchRequest(query="q"))
    ctx.results = list(results)
    return ctx


def _filter_for(pii_class: str, action: str) -> PrivacyFilter:
    """Filter with only ``pii_class`` active (others disabled)."""
    patterns = {
        cls: ([PATTERNS[cls]] if cls == pii_class else []) for cls in ALL_CLASSES
    }
    return PrivacyFilter({"patterns": patterns, "actions": {pii_class: action}})


# ---------------------------------------------------------------------------
# Plugin declaration
# ---------------------------------------------------------------------------


def test_privacy_filter_declaration():
    plugin = PrivacyFilter()
    assert plugin.name == "privacy-filter"
    assert plugin.plugin_type is PluginType.FILTER
    assert plugin.stage is PipelineStage.FILTERING
    assert plugin.mandatory is True


# ---------------------------------------------------------------------------
# Each PII class x {exclude, redact}
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pii_class", ALL_CLASSES)
def test_privacy_exclude_each_class(pii_class: str):
    plugin = _filter_for(pii_class, "exclude")
    hit = _hit(snippet=SAMPLE_SNIPPETS[pii_class])
    clean = _hit("clean", snippet="nothing sensitive here")
    out = plugin.run(_context([hit, clean]))
    assert [r.id for r in out.results] == ["clean"], (
        f"[privacy_filter] exclude must drop {pii_class} hit"
    )
    assert "privacy-filter" in out.applied_filters
    assert out.metadata["privacy-filter-removed"] == 1


@pytest.mark.parametrize("pii_class", ALL_CLASSES)
def test_privacy_redact_each_class(pii_class: str):
    plugin = _filter_for(pii_class, "redact")
    hit = _hit(snippet=SAMPLE_SNIPPETS[pii_class])
    out = plugin.run(_context([hit]))
    assert len(out.results) == 1, f"[privacy_filter] redact must keep {pii_class} hit"
    result = out.results[0]
    assert PII_STRINGS[pii_class] not in result.snippet
    assert "[REDACTED]" in result.snippet
    assert any("privacy-filter" in r and pii_class in r for r in result.reasons), (
        "[privacy_filter] redaction must append a reason naming the class"
    )
    assert out.metadata["privacy-filter-removed"] == 0
    assert "privacy-filter" in out.applied_filters


@pytest.mark.parametrize("pii_class", ALL_CLASSES)
def test_privacy_exclude_in_title(pii_class: str):
    plugin = _filter_for(pii_class, "exclude")
    hit = _hit(title=SAMPLE_SNIPPETS[pii_class], snippet="clean snippet")
    out = plugin.run(_context([hit]))
    assert out.results == []
    assert out.metadata["privacy-filter-removed"] == 1


@pytest.mark.parametrize("pii_class", ALL_CLASSES)
def test_privacy_redact_in_title(pii_class: str):
    plugin = _filter_for(pii_class, "redact")
    hit = _hit(title=SAMPLE_SNIPPETS[pii_class], snippet="clean snippet")
    out = plugin.run(_context([hit]))
    assert len(out.results) == 1
    assert PII_STRINGS[pii_class] not in out.results[0].title
    assert "[REDACTED]" in out.results[0].title


@pytest.mark.parametrize("pii_class", ALL_CLASSES)
def test_privacy_exclude_in_metadata(pii_class: str):
    plugin = _filter_for(pii_class, "exclude")
    hit = _hit(metadata={"note": SAMPLE_SNIPPETS[pii_class]})
    out = plugin.run(_context([hit]))
    assert out.results == []
    assert out.metadata["privacy-filter-removed"] == 1


@pytest.mark.parametrize("pii_class", ALL_CLASSES)
def test_privacy_redact_in_metadata(pii_class: str):
    plugin = _filter_for(pii_class, "redact")
    hit = _hit(metadata={"note": SAMPLE_SNIPPETS[pii_class]})
    out = plugin.run(_context([hit]))
    assert len(out.results) == 1
    assert PII_STRINGS[pii_class] not in str(out.results[0].metadata["note"])
    assert "[REDACTED]" in str(out.results[0].metadata["note"])


def test_privacy_mixed_actions_per_class():
    """Per-class config: email redacts while government_id excludes."""
    patterns = {cls: [PATTERNS[cls]] for cls in ALL_CLASSES}
    plugin = PrivacyFilter(
        {
            "patterns": patterns,
            "actions": {"email": "redact", "government_id": "exclude"},
        }
    )
    email_hit = _hit("e", snippet=SAMPLE_SNIPPETS["email"])
    ssn_hit = _hit("s", snippet=SAMPLE_SNIPPETS["government_id"])
    out = plugin.run(_context([email_hit, ssn_hit]))
    assert [r.id for r in out.results] == ["e"]
    assert "[REDACTED]" in out.results[0].snippet
    assert out.metadata["privacy-filter-removed"] == 1


# ---------------------------------------------------------------------------
# Redaction isolation: score fields untouched
# ---------------------------------------------------------------------------


def test_redaction_leaves_score_fields_untouched():
    plugin = _filter_for("email", "redact")
    hit = _hit(snippet="Reach john.doe@example.com today")
    hit.original_score = 3.25
    hit.final_score = 2.5
    original_before = hit.original_score
    final_before = hit.final_score
    out = plugin.run(_context([hit]))
    result = out.results[0]
    assert result.original_score == original_before
    assert result.final_score == final_before
    assert "[REDACTED]" in result.snippet
    assert "john.doe@example.com" not in result.snippet


@pytest.mark.parametrize("pii_class", ALL_CLASSES)
def test_redaction_never_writes_scores_any_class(pii_class: str):
    plugin = _filter_for(pii_class, "redact")
    hit = _hit(snippet=SAMPLE_SNIPPETS[pii_class])
    hit.original_score = 7.0
    hit.final_score = 6.0
    out = plugin.run(_context([hit]))
    assert out.results[0].original_score == 7.0
    assert out.results[0].final_score == 6.0


# ---------------------------------------------------------------------------
# No-PII passthrough
# ---------------------------------------------------------------------------


def test_no_pii_passthrough():
    patterns = {cls: [PATTERNS[cls]] for cls in ALL_CLASSES}
    actions = {cls: "exclude" for cls in ALL_CLASSES}
    plugin = PrivacyFilter({"patterns": patterns, "actions": actions})
    hits = [
        _hit("a", title="Quarterly report", snippet="Revenue grew this year"),
        _hit("b", title="Travel guide", snippet="Best places to visit"),
    ]
    out = plugin.run(_context(hits))
    assert [r.id for r in out.results] == ["a", "b"]
    assert out.metadata["privacy-filter-removed"] == 0
    assert "privacy-filter" in out.applied_filters
    for result in out.results:
        assert "[REDACTED]" not in result.title
        assert "[REDACTED]" not in result.snippet


def test_no_pii_passthrough_redact_mode():
    patterns = {cls: [PATTERNS[cls]] for cls in ALL_CLASSES}
    actions = {cls: "redact" for cls in ALL_CLASSES}
    plugin = PrivacyFilter({"patterns": patterns, "actions": actions})
    hit = _hit("a", snippet="a perfectly clean snippet")
    reasons_before = list(hit.reasons)
    out = plugin.run(_context([hit]))
    assert len(out.results) == 1
    assert out.results[0].reasons == reasons_before
    assert out.results[0].snippet == "a perfectly clean snippet"


# ---------------------------------------------------------------------------
# Street addresses: only config patterns, no invented grammar
# ---------------------------------------------------------------------------


def test_street_address_only_from_config_patterns():
    # No street pattern configured -> address-like text passes through.
    patterns = {cls: [] for cls in ALL_CLASSES}
    patterns["email"] = [PATTERNS["email"]]
    plugin = PrivacyFilter({"patterns": patterns, "actions": {"email": "exclude"}})
    hit = _hit(snippet="Meet at 123 Main Street today")
    out = plugin.run(_context([hit]))
    assert len(out.results) == 1

    # With an explicit config pattern it is caught.
    plugin2 = _filter_for("street_address", "exclude")
    out2 = plugin2.run(_context([_hit(snippet="Meet at 123 Main Street today")]))
    assert out2.results == []


def test_default_filter_has_no_street_address_pattern():
    # Default construction must not invent an address grammar.
    plugin = PrivacyFilter()
    hit = _hit(snippet="Meet at 999 Broad Avenue today")
    out = plugin.run(_context([hit]))
    assert len(out.results) == 1


# ---------------------------------------------------------------------------
# Fail-closed outage
# ---------------------------------------------------------------------------


def test_simulate_outage_fails_closed():
    patterns = {cls: [PATTERNS[cls]] for cls in ALL_CLASSES}
    plugin = PrivacyFilter({"patterns": patterns, "simulate_outage": True})
    ctx = _context([_hit(snippet=SAMPLE_SNIPPETS["email"]), _hit("c")])
    before_ids = [r.id for r in ctx.results]
    with pytest.raises(PluginError) as excinfo:
        plugin.run(ctx)
    assert excinfo.value.mandatory is True
    assert excinfo.value.plugin_name == "privacy-filter"
    # No partial filtering happened before the raise.
    assert [r.id for r in ctx.results] == before_ids


def test_simulate_outage_fails_closed_via_pipeline():
    plugin = PrivacyFilter({"simulate_outage": True})
    ctx = _context([_hit(), _hit("h2")])
    with pytest.raises(PluginError) as excinfo:
        Pipeline([plugin]).run(ctx)
    assert excinfo.value.mandatory is True


# ---------------------------------------------------------------------------
# Config validation: caps + invalid patterns (ReDoS guards)
# ---------------------------------------------------------------------------


def test_configure_rejects_too_many_patterns():
    too_many = [r"a"] * 33
    with pytest.raises(ConfigError):
        PrivacyFilter({"patterns": {"email": too_many}, "actions": {"email": "redact"}})


def test_configure_rejects_overlong_pattern():
    long_pattern = "a" * 513
    with pytest.raises(ConfigError):
        PrivacyFilter(
            {
                "patterns": {"email": [long_pattern]},
                "actions": {"email": "redact"},
            }
        )


def test_configure_rejects_invalid_regex():
    with pytest.raises(ConfigError):
        PrivacyFilter(
            {
                "patterns": {"email": ["[unclosed"]},
                "actions": {"email": "redact"},
            }
        )


def test_configure_rejects_invalid_action():
    with pytest.raises(ConfigError):
        PrivacyFilter(
            {
                "patterns": {"email": [PATTERNS["email"]]},
                "actions": {"email": "hide"},
            }
        )


def test_configure_rejects_unknown_class():
    with pytest.raises(ConfigError):
        PrivacyFilter({"patterns": {"fingerprint": [r"abc"]}})


def test_configure_rejects_non_list_patterns():
    with pytest.raises(ConfigError):
        PrivacyFilter({"patterns": {"email": 123}})  # type: ignore[dict-item]


def test_alternate_config_shape_classes_mapping():
    plugin = PrivacyFilter(
        {
            "classes": {
                "email": {
                    "patterns": [PATTERNS["email"]],
                    "action": "redact",
                },
                "phone": {
                    "patterns": [PATTERNS["phone"]],
                    "action": "exclude",
                },
                "government_id": {"patterns": [], "action": "exclude"},
                "street_address": {"patterns": [], "action": "exclude"},
            }
        }
    )
    out = plugin.run(_context([_hit(snippet="Mail john.doe@example.com")]))
    assert "[REDACTED]" in out.results[0].snippet
    out2 = plugin.run(_context([_hit(snippet="Call 555-123-4567")]))
    assert out2.results == []

"""Unit tests for the shared scoring-signal helpers."""

from __future__ import annotations

from datetime import datetime, timezone

from openlens.core.interfaces import SearchResult
from openlens.plugins import _signals
from openlens.plugins._signals import (
    NEUTRAL_SIGNAL,
    extract_domain,
    get_signal,
    parse_date,
    result_date,
    set_signal,
)


def _hit(**metadata: object) -> SearchResult:
    return SearchResult(
        id="d1",
        title="t",
        snippet="s",
        original_score=1.0,
        final_score=1.0,
        metadata=dict(metadata),
    )


def test_get_signal_defaults_neutral() -> None:
    assert get_signal(_hit(), "trust") == NEUTRAL_SIGNAL


def test_get_signal_neutral_when_signals_not_a_dict() -> None:
    hit = _hit(signals="trust me")  # type: ignore[arg-type]
    assert get_signal(hit, "trust") == NEUTRAL_SIGNAL


def test_set_then_get_roundtrip() -> None:
    hit = _hit()
    assert set_signal(hit, "trust", 0.8) == 0.8
    assert get_signal(hit, "trust") == 0.8
    assert get_signal(hit, "freshness") == NEUTRAL_SIGNAL


def test_set_signal_clamps_out_of_range() -> None:
    hit = _hit()
    assert set_signal(hit, "a", 1.5) == 1.0
    assert set_signal(hit, "b", -0.2) == 0.0
    assert hit.metadata["signals"] == {"a": 1.0, "b": 0.0}


def test_get_signal_clamps_preexisting_out_of_range() -> None:
    hit = _hit(signals={"trust": 99.0, "freshness": -3.0})
    assert get_signal(hit, "trust") == 1.0
    assert get_signal(hit, "freshness") == 0.0


def test_set_signal_non_numeric_becomes_neutral() -> None:
    hit = _hit()
    assert set_signal(hit, "trust", "garbage") == NEUTRAL_SIGNAL
    assert set_signal(hit, "trust", None) == NEUTRAL_SIGNAL
    assert set_signal(hit, "trust", True) == NEUTRAL_SIGNAL


def test_extract_domain_case_and_scheme_insensitive() -> None:
    assert extract_domain("HTTP://Example.COM/Path") == "example.com"
    assert extract_domain("https://example.com:8443/x") == "example.com"
    assert extract_domain("example.com/some/page") == "example.com"


def test_extract_domain_idn_normalized() -> None:
    assert extract_domain("http://MÜNCHEN.de/x") == "xn--mnchen-3ya.de"
    assert extract_domain("http://münchen.de/x") == extract_domain(
        "http://xn--mnchen-3ya.de/x"
    )


def test_extract_domain_malformed_returns_empty() -> None:
    assert extract_domain("") == ""
    assert extract_domain("   ") == ""
    assert extract_domain("http://") == ""


def test_parse_date_iso_with_z() -> None:
    assert parse_date("2024-01-15T10:30:00Z") == datetime(
        2024, 1, 15, 10, 30, tzinfo=timezone.utc
    )


def test_parse_date_iso_with_offset() -> None:
    parsed = parse_date("2024-01-15T10:30:00+02:00")
    assert parsed is not None
    assert parsed.utcoffset() is not None


def test_parse_date_epoch_forms() -> None:
    assert parse_date(0) == datetime(1970, 1, 1, tzinfo=timezone.utc)
    assert parse_date(1710000000.5) == datetime.fromtimestamp(
        1710000000.5, tz=timezone.utc
    )


def test_parse_date_malformed_returns_none() -> None:
    assert parse_date("yesterday-ish") is None
    assert parse_date("") is None
    assert parse_date("   ") is None
    assert parse_date(None) is None
    assert parse_date(True) is None
    assert parse_date({"year": 2024}) is None
    assert parse_date(float("nan")) is None


def test_result_date_pinned_key_priority() -> None:
    hit = _hit(
        date="2020-01-01T00:00:00Z",
        published="2021-06-01T00:00:00Z",
        published_at="2022-03-01T00:00:00Z",
    )
    assert result_date(hit) == datetime(2022, 3, 1, tzinfo=timezone.utc)
    assert result_date(_hit(published="2021-06-01T00:00:00Z")) == datetime(
        2021, 6, 1, tzinfo=timezone.utc
    )
    assert result_date(_hit(date=1710000000)) == datetime.fromtimestamp(
        1710000000, tz=timezone.utc
    )


def test_result_date_absent_or_malformed_is_none() -> None:
    assert result_date(_hit()) is None
    assert result_date(_hit(published_at="not-a-date")) is None
    assert result_date(_hit(published_at=None)) is None


def test_helpers_are_deterministic_and_side_effect_free() -> None:
    hit = _hit(published_at="2022-03-01T00:00:00Z")
    before = dict(hit.metadata)
    assert result_date(hit) == result_date(hit)
    assert extract_domain("https://Example.com/x") == "example.com"
    set_signal(hit, "trust", 0.9)
    assert before["published_at"] == hit.metadata["published_at"]
    assert _signals.SIGNALS_KEY in hit.metadata

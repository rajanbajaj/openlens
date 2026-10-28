"""Unit tests for the attribute enricher (rerank-pack step 1)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from openlens.core.context import SearchContext
from openlens.core.interfaces import (
    PipelineStage,
    PluginType,
    SearchRequest,
    SearchResult,
)
from openlens.plugins.attribute_enricher import AttributeEnricher


def _hit(hit_id: str = "h", metadata: dict[str, Any] | None = None) -> SearchResult:
    return SearchResult(
        id=hit_id,
        title="a title",
        snippet="a snippet",
        original_score=1.0,
        final_score=1.0,
        url="https://example.com/x",
        provider="test",
        metadata=dict(metadata or {}),
    )


def _ctx(*hits: SearchResult) -> SearchContext:
    ctx = SearchContext(request=SearchRequest(query="q"))
    ctx.results = list(hits)
    return ctx


def _run(*hits: SearchResult) -> list[SearchResult]:
    return AttributeEnricher({}).run(_ctx(*hits)).results


def test_plugin_contract():
    plugin = AttributeEnricher({})
    assert plugin.name == "attribute-enricher"
    assert plugin.stage == PipelineStage.ENRICHMENT
    assert plugin.plugin_type == PluginType.ENRICHER
    assert plugin.mandatory is False


def test_price_normalization():
    assert _run(_hit(metadata={"price": 42}))[0].metadata["price"] == 42.0
    assert _run(_hit(metadata={"price": "$1,299.99"}))[0].metadata["price"] == 1299.99
    assert _run(_hit(metadata={"price": "100 USD"}))[0].metadata["price"] == 100.0


def test_currency_normalization():
    assert _run(_hit(metadata={"currency": "usd"}))[0].metadata["currency"] == "USD"


def test_location_normalization():
    (result,) = _run(_hit(metadata={"location": "  Paris  "}))
    assert result.metadata["location"] == "Paris"


def test_facilities_normalization():
    (listed,) = _run(_hit(metadata={"facilities": [" WiFi ", "Pool"]}))
    assert listed.metadata["facilities"] == ["wifi", "pool"]
    (csv,) = _run(_hit(metadata={"facilities": "wifi, pool"}))
    assert csv.metadata["facilities"] == ["wifi", "pool"]


def test_published_normalization():
    (iso,) = _run(_hit(metadata={"published": "2024-03-05"}))
    assert iso.metadata["published"] == "2024-03-05"
    (slashed,) = _run(_hit(metadata={"published": "2024/03/05"}))
    assert slashed.metadata["published"] == "2024-03-05"
    (dt,) = _run(_hit(metadata={"published": datetime(2024, 3, 5, 12, 0)}))
    assert dt.metadata["published"] == "2024-03-05"
    (day,) = _run(_hit(metadata={"published": date(2024, 3, 5)}))
    assert day.metadata["published"] == "2024-03-05"


def test_perspective_normalization():
    (result,) = _run(_hit(metadata={"perspective": " Left "}))
    assert result.metadata["perspective"] == "left"


def test_missing_attributes_degrade_to_sentinels():
    (result,) = _run(_hit())
    assert result.metadata["price"] is None
    assert result.metadata["currency"] is None
    assert result.metadata["location"] is None
    assert result.metadata["facilities"] == []
    assert result.metadata["published"] is None
    assert result.metadata["perspective"] == "unknown"
    assert result.warnings == []


def test_unparseable_keeps_raw_plus_warning():
    (result,) = _run(
        _hit(
            metadata={
                "price": "free-ish",
                "currency": 123,
                "location": 123,
                "facilities": 123,
                "published": "sometime-ish",
                "perspective": 123,
            }
        )
    )
    assert result.metadata["price"] == "free-ish"
    assert result.metadata["currency"] == 123
    assert result.metadata["location"] == 123
    assert result.metadata["facilities"] == 123
    assert result.metadata["published"] == "sometime-ish"
    assert result.metadata["perspective"] == 123
    assert len(result.warnings) == 6
    assert all("attribute-enricher" in w for w in result.warnings)


def test_never_emits_trust_warnings():
    (result,) = _run(_hit(metadata={"price": "nope", "published": "never"}))
    assert result.warnings
    for warning in result.warnings:
        lowered = warning.lower()
        assert "trust" not in lowered
        assert "unverified" not in lowered
        assert "distrusted" not in lowered


def test_deterministic_output():
    def snapshot() -> list[dict[str, Any]]:
        hits = _run(
            _hit("a", {"price": "$10", "facilities": "wifi"}),
            _hit("b"),
        )
        return [dict(r.metadata) for r in hits]

    assert snapshot() == snapshot()

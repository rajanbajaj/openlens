"""AI query understanding: model labeling, rule fallback, failure isolation.

Offline only: fake backend and stubs, no network or keys.
"""

from __future__ import annotations

from typing import Any

from openlens.core.context import SearchContext
from openlens.core.interfaces import (
    PipelineStage,
    ProviderError,
    SearchRequest,
    SearchResult,
)
from openlens.core.pipeline import Pipeline
from openlens.models.fake import FakeModelProvider
from openlens.plugins.ai_understanding import AIUnderstandingPlugin


def _context(query: str = "cheap flights to Paris") -> SearchContext:
    return SearchContext(
        request=SearchRequest(query=query, mode="default", limit=10, offset=0),
        results=[
            SearchResult(
                id="1",
                title="Flights",
                snippet="Cheap flights.",
                original_score=1.0,
                final_score=1.0,
            ),
            SearchResult(
                id="2",
                title="Paris guide",
                snippet="Visit Paris.",
                original_score=0.5,
                final_score=0.5,
            ),
        ],
    )


class _RaisingModel(FakeModelProvider):
    @property
    def name(self) -> str:  # type: ignore[override]
        return "raising"

    def complete(self, prompt: str, **kwargs: Any) -> str:
        raise ProviderError(
            "backend exploded",
            provider_name="raising",
            retryable=False,
            code=ProviderError.UNAVAILABLE,
        )

    def classify(self, text: str, labels: list[str]) -> str:
        raise ProviderError(
            "backend exploded",
            provider_name="raising",
            retryable=False,
            code=ProviderError.UNAVAILABLE,
        )


def test_plugin_identity_is_optional_query_parsing() -> None:
    plugin = AIUnderstandingPlugin()
    assert plugin.name == "ai-understanding"
    assert plugin.stage in (PipelineStage.QUERY_PARSING, PipelineStage.QUERY_REWRITE)
    assert plugin.mandatory is False
    # Docstring states v1 does not influence retrieval / S1 dependency.
    doc = (
        AIUnderstandingPlugin.__module__
        and __import__("openlens.plugins.ai_understanding", fromlist=["x"]).__doc__
    ) or ""
    assert "does not influence retrieval" in doc
    assert "S1" in doc


def test_fake_backend_labels_intent_entities_with_backend_name() -> None:
    plugin = AIUnderstandingPlugin(model=FakeModelProvider())
    context = plugin.run(_context())
    assert context.results
    for result in context.results:
        assert result.metadata["ai_model"] == "fake"
        assert result.metadata["ai_intent"]
        assert isinstance(result.metadata["ai_entities"], list)
    assert context.results[0].metadata["ai_intent"] == "shopping"
    assert "Paris" in context.results[0].metadata["ai_entities"]
    assert context.metadata["ai_understanding"]["ai_model"] == "fake"
    assert context.metadata["ai_understanding"]["intent"] == "shopping"
    # Labeling only: order, membership, scores, and query untouched.
    assert [r.id for r in context.results] == ["1", "2"]
    assert context.request.query == "cheap flights to Paris"


def test_no_backend_returns_rule_based_output() -> None:
    for plugin in (AIUnderstandingPlugin(), AIUnderstandingPlugin(model=None)):
        context = plugin.run(_context("buy shoes online"))
        for result in context.results:
            assert result.metadata["ai_model"] == "rule-based"
            assert result.metadata["ai_intent"] == "shopping"
            assert "shoes" in result.metadata["ai_entities"]
        assert context.metadata["ai_understanding"]["source"] == "rule-based"


def test_raising_model_falls_back_without_request_failure() -> None:
    plugin = AIUnderstandingPlugin(model=_RaisingModel())
    # Through the pipeline: request succeeds, diagnostics record fallback.
    context = Pipeline([plugin]).run(_context())
    for result in context.results:
        assert result.metadata["ai_model"] == "rule-based"
        assert result.metadata["ai_intent"]
        assert isinstance(result.metadata["ai_entities"], list)
    diagnostics = context.diagnostics()
    assert diagnostics["fallback_used"] is True
    assert any(
        "ai-understanding" in error and "rule-based" in error
        for error in diagnostics["errors"]
    )
    assert context.metadata["ai_understanding"]["fallback"] is True
    executions = diagnostics["plugin_executions"]
    assert executions and executions[0]["plugin"] == "ai-understanding"
    assert executions[0]["status"] == "ok"


def test_set_model_injection_switches_to_backend_label() -> None:
    plugin = AIUnderstandingPlugin()
    assert plugin.run(_context()).results[0].metadata["ai_model"] == "rule-based"
    plugin.set_model(FakeModelProvider())
    assert plugin.model is not None
    context = plugin.run(_context())
    assert context.results[0].metadata["ai_model"] == "fake"

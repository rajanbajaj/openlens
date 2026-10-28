"""Step 3: AI classification is advisory only; rules still exclude alone.

Offline only: fake/stub backends, no network, no keys.
"""

from __future__ import annotations

from typing import Any

import pytest

from openlens.core.context import SearchContext
from openlens.core.interfaces import (
    ConfigError,
    PipelineStage,
    ProviderError,
    SearchRequest,
    SearchResult,
)
from openlens.core.pipeline import Pipeline
from openlens.models.fake import FakeModelProvider
from openlens.models.interfaces import AIModelProvider
from openlens.plugins.ai_classification import AIClassificationPlugin
from openlens.plugins.safety_filter import SafetyFilter


def _result(
    id: str = "1",
    title: str = "Cats",
    snippet: str = "Cats nap.",
    url: str | None = "https://example.com/cats",
) -> SearchResult:
    return SearchResult(
        id=id,
        title=title,
        snippet=snippet,
        original_score=1.0,
        final_score=1.0,
        url=url,
        provider="fake-search",
    )


def _context(results: list[SearchResult] | None = None) -> SearchContext:
    return SearchContext(
        request=SearchRequest(query="cats", mode="default", limit=10, offset=0),
        results=results
        if results is not None
        else [_result("1"), _result("2", title="Dogs", snippet="Dogs bark.")],
    )


class _SafeModel(AIModelProvider):
    """Stub backend that labels everything 'safe'."""

    @property
    def name(self) -> str:
        return "safe-stub"

    def configure(self, config: dict[str, Any]) -> None:
        pass

    def complete(self, prompt: str, **kwargs: Any) -> str:
        return "safe"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] for _ in texts]

    def classify(self, text: str, labels: list[str]) -> str:
        return "safe"

    def health_check(self) -> bool:
        return True


class _RaisingModel(AIModelProvider):
    """Stub backend whose classify always raises ProviderError."""

    @property
    def name(self) -> str:
        return "raising-stub"

    def configure(self, config: dict[str, Any]) -> None:
        pass

    def complete(self, prompt: str, **kwargs: Any) -> str:
        raise ProviderError(
            "backend down", provider_name=self.name, code=ProviderError.UNAVAILABLE
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise ProviderError(
            "backend down", provider_name=self.name, code=ProviderError.UNAVAILABLE
        )

    def classify(self, text: str, labels: list[str]) -> str:
        raise ProviderError(
            "backend down", provider_name=self.name, code=ProviderError.UNAVAILABLE
        )

    def health_check(self) -> bool:
        return False


def _blocked_and_clean() -> list[SearchResult]:
    return [
        _result("blocked", title="explicit content", snippet="explicit stuff"),
        _result("clean", title="Cats", snippet="Cats nap."),
    ]


# -- plugin contract ----------------------------------------------------------


def test_plugin_is_optional_filtering() -> None:
    plugin = AIClassificationPlugin(model=FakeModelProvider())
    assert plugin.mandatory is False
    assert plugin.stage is PipelineStage.FILTERING
    assert plugin.name == "ai-classification"


def test_plugin_does_not_reference_safety_filter() -> None:
    import openlens.plugins.ai_classification as module

    assert not hasattr(module, "SafetyFilter")


# -- advisory labels only -----------------------------------------------------


def test_labels_land_in_metadata_and_warnings_only() -> None:
    plugin = AIClassificationPlugin(model=FakeModelProvider())
    context = _context(
        [
            _result("1", title="Cats", snippet="Cats nap."),
            _result("2", title="spam deal", snippet="spam offer now"),
        ]
    )
    before_ids = [r.id for r in context.results]
    before_scores = [(r.original_score, r.final_score) for r in context.results]
    result_list = context.results
    out = plugin.run(context)
    # Membership and list identity untouched: never excludes, never mutates.
    assert out.results is result_list
    assert [r.id for r in out.results] == before_ids
    assert len(out.results) == 2
    assert [(r.original_score, r.final_score) for r in out.results] == before_scores
    assert out.results[0].metadata["ai_classification"] == "safe"
    assert out.results[0].metadata["ai_model"] == "fake"
    assert out.results[0].warnings == []
    assert out.results[1].metadata["ai_classification"] == "spam"
    assert out.results[1].metadata["ai_model"] == "fake"
    assert any("spam" in w for w in out.results[1].warnings)


def test_classification_plugin_alone_never_changes_length() -> None:
    for model in (FakeModelProvider(), _SafeModel(), _RaisingModel(), None):
        context = _context(_blocked_and_clean())
        plugin = AIClassificationPlugin(model=model)  # type: ignore[arg-type]
        out = plugin.run(context)
        assert len(out.results) == 2
        assert [r.id for r in out.results] == ["blocked", "clean"]


def test_no_backend_means_no_labels() -> None:
    context = _context()
    out = AIClassificationPlugin(model=None).run(context)
    assert len(out.results) == 2
    for result in out.results:
        assert "ai_classification" not in result.metadata
        assert "ai_model" not in result.metadata
        assert result.warnings == []
    # Default construction (no injected backend) is also label-free.
    out2 = AIClassificationPlugin().run(_context())
    for result in out2.results:
        assert "ai_classification" not in result.metadata


def test_raising_model_means_no_labels_and_no_failure() -> None:
    context = _context()
    out = AIClassificationPlugin(model=_RaisingModel()).run(context)
    assert [r.id for r in out.results] == ["1", "2"]
    for result in out.results:
        assert "ai_classification" not in result.metadata
        assert result.warnings == []


def test_classification_capped_at_top_k() -> None:
    results = [_result(str(i), title=f"Doc {i}", snippet="Cats nap.") for i in range(8)]
    context = _context(results)
    out = AIClassificationPlugin({"classify_top_k": 3}, model=FakeModelProvider()).run(
        context
    )
    assert len(out.results) == 8
    labeled = [r for r in out.results if "ai_classification" in r.metadata]
    assert [r.id for r in labeled] == ["0", "1", "2"]
    # enrich_top_k is honoured as the fallback cap key.
    out2 = AIClassificationPlugin({"enrich_top_k": 1}, model=FakeModelProvider()).run(
        _context([_result("a"), _result("b", title="Dogs", snippet="Dogs bark.")])
    )
    assert "ai_classification" in out2.results[0].metadata
    assert "ai_classification" not in out2.results[1].metadata
    # 0 disables labeling without failing.
    out3 = AIClassificationPlugin({"classify_top_k": 0}, model=FakeModelProvider()).run(
        _context()
    )
    assert all("ai_classification" not in r.metadata for r in out3.results)


def test_bad_config_rejected() -> None:
    with pytest.raises(ConfigError):
        AIClassificationPlugin({"classify_top_k": 99})
    with pytest.raises(ConfigError):
        AIClassificationPlugin({"labels": []})
    with pytest.raises(ConfigError):
        AIClassificationPlugin({"model": "fake"})  # type: ignore[dict-item]


def test_model_injection_via_set_model_and_config() -> None:
    plugin = AIClassificationPlugin()
    fake = FakeModelProvider()
    plugin.set_model(fake)
    assert plugin.model is fake
    configured = AIClassificationPlugin({"model": {"type": "fake"}})
    assert isinstance(configured.model, FakeModelProvider)


# -- SafetyFilter still excludes alone in all three modes ---------------------


def _assert_blocked_stays_excluded(model: Any) -> None:
    # Classification runs first (advisory), then the mandatory filter.
    context = _context(_blocked_and_clean())
    AIClassificationPlugin(model=model).run(context)
    assert len(context.results) == 2  # classification alone excluded nothing
    SafetyFilter().run(context)
    assert [r.id for r in context.results] == ["clean"]


def test_excluded_stays_excluded_model_disabled() -> None:
    _assert_blocked_stays_excluded(None)


def test_excluded_stays_excluded_model_returns_safe() -> None:
    _assert_blocked_stays_excluded(_SafeModel())
    # Also via the fake backend, which returns 'safe' for a blocked hit
    # whose text contains no label keyword.
    _assert_blocked_stays_excluded(FakeModelProvider())


def test_excluded_stays_excluded_model_raises() -> None:
    _assert_blocked_stays_excluded(_RaisingModel())


def test_excluded_stays_excluded_through_pipeline_all_modes() -> None:
    for model in (None, _SafeModel(), _RaisingModel()):
        context = _context(_blocked_and_clean())
        pipeline = Pipeline(
            [AIClassificationPlugin(model=model), SafetyFilter()]  # type: ignore[arg-type]
        )
        out = pipeline.run(context)
        assert [r.id for r in out.results] == ["clean"]

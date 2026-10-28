"""Step-4 enrichment tests: labeled summary+perspectives, cap, timeout.

Offline only: fake/stub backends, no network, no keys.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from openlens.core.context import SearchContext
from openlens.core.interfaces import (
    ConfigError,
    PluginError,
    ProviderError,
    SearchRequest,
    SearchResult,
)
from openlens.core.pipeline import Pipeline
from openlens.models.base import BaseModelProvider
from openlens.models.fake import FakeModelProvider
from openlens.plugins.ai_summary import AISummaryPlugin


def _result(id: str, title: str = "Cats", snippet: str = "Cats nap.") -> SearchResult:
    return SearchResult(
        id=id, title=title, snippet=snippet, original_score=1.0, final_score=1.0
    )


def _context(count: int = 3) -> SearchContext:
    return SearchContext(
        request=SearchRequest(query="cats", mode="default", limit=10, offset=0),
        results=[_result(str(i)) for i in range(1, count + 1)],
    )


class _StubModel(BaseModelProvider):
    """Counting stub with a non-fake name, to prove backend labeling."""

    def __init__(self) -> None:
        super().__init__()
        self.prompts: list[str] = []

    @property
    def name(self) -> str:
        return "stub-backend"

    def complete(self, prompt: str, **kwargs: Any) -> str:
        self.prompts.append(prompt)
        # Deliberately longer than any max_chars: plugin must not re-truncate.
        return f"reply[{len(prompt)}]: {prompt}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] for _ in texts]

    def classify(self, text: str, labels: list[str]) -> str:
        return labels[0]

    def health_check(self) -> bool:
        return True


class _HangingModel(BaseModelProvider):
    """Hangs in ``complete`` so the shared helper's timeout must fire."""

    @property
    def name(self) -> str:
        return "hanging"

    def complete(self, prompt: str, **kwargs: Any) -> str:
        time.sleep(30)
        return "never"  # pragma: no cover - helper times out first

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] for _ in texts]

    def classify(self, text: str, labels: list[str]) -> str:
        return labels[0]

    def health_check(self) -> bool:
        return True


# -- labeling -----------------------------------------------------------------


def test_labels_present_with_backend_name() -> None:
    plugin = AISummaryPlugin(model=FakeModelProvider())
    context = plugin.run(_context(2))
    for result in context.results:
        assert result.metadata["ai_summary"].startswith("fake completion:")
        assert result.metadata["ai_perspectives"].startswith("fake completion:")
        assert "Summarize:" in result.metadata["ai_summary"]
        assert "Perspectives:" in result.metadata["ai_perspectives"]
        assert result.metadata["ai_model"] == "fake"


def test_label_uses_injected_backend_name_not_hardcoded() -> None:
    stub = _StubModel()
    plugin = AISummaryPlugin(model=stub)
    context = plugin.run(_context(1))
    result = context.results[0]
    assert result.metadata["ai_model"] == "stub-backend"
    assert result.metadata["ai_summary"]
    assert result.metadata["ai_perspectives"]


# -- one shared helper, one loop -----------------------------------------------


def test_shared_helper_serves_both_prompts_per_result() -> None:
    stub = _StubModel()
    plugin = AISummaryPlugin({"enrich_top_k": 2}, model=stub)
    plugin.run(_context(3))
    # Exactly two calls (summary + perspectives) for each enriched
    # result, none for the result beyond K: one loop, one helper.
    assert len(stub.prompts) == 4
    assert stub.prompts[0].startswith("Summarize:")
    assert stub.prompts[1].startswith("Perspectives:")
    assert stub.prompts[2].startswith("Summarize:")
    assert stub.prompts[3].startswith("Perspectives:")


def test_max_chars_truncation_respected_and_output_not_retruncated() -> None:
    stub = _StubModel()
    plugin = AISummaryPlugin({"max_chars": 10}, model=stub)
    context = plugin.run(
        SearchContext(
            request=SearchRequest(query="q"),
            results=[_result("1", title="LongTitleHere", snippet="LongSnippetHere")],
        )
    )
    # Source fed to both prompts is the max_chars-truncated text, once.
    assert all("LongTitleHere LongSnippetHere" not in p for p in stub.prompts)
    assert all(len(p) <= len("Perspectives: ") + 10 for p in stub.prompts)
    # Model output is stored verbatim, not cut back to max_chars.
    assert len(context.results[0].metadata["ai_summary"]) > 10
    assert len(context.results[0].metadata["ai_perspectives"]) > 10


# -- cap ----------------------------------------------------------------------


def test_enrichment_capped_at_enrich_top_k() -> None:
    plugin = AISummaryPlugin({"enrich_top_k": 2}, model=FakeModelProvider())
    context = plugin.run(_context(4))
    for result in context.results[:2]:
        assert "ai_summary" in result.metadata
        assert "ai_perspectives" in result.metadata
        assert result.metadata["ai_model"] == "fake"
    for result in context.results[2:]:
        assert "ai_summary" not in result.metadata
        assert "ai_perspectives" not in result.metadata
        assert "ai_model" not in result.metadata


def test_default_cap_is_five() -> None:
    plugin = AISummaryPlugin(model=FakeModelProvider())
    assert plugin.enrich_top_k == 5
    context = plugin.run(_context(7))
    enriched = [r for r in context.results if "ai_summary" in r.metadata]
    assert len(enriched) == 5
    assert all("ai_perspectives" not in r.metadata for r in context.results[5:])


def test_enrich_top_k_zero_yields_no_ai_fields_but_succeeds() -> None:
    stub = _StubModel()
    plugin = AISummaryPlugin({"enrich_top_k": 0}, model=stub)
    context = plugin.run(_context(3))
    assert stub.prompts == []
    for result in context.results:
        assert "ai_summary" not in result.metadata
        assert "ai_perspectives" not in result.metadata
        assert "ai_model" not in result.metadata
    # Same guarantee through the pipeline: execution records ok.
    piped = Pipeline([AISummaryPlugin({"enrich_top_k": 0})]).run(_context(2))
    assert piped.diagnostics()["plugin_executions"][0]["status"] == "ok"


@pytest.mark.parametrize("bad", [-1, 21, "5", 1.5, True])
def test_enrich_top_k_validated(bad: Any) -> None:
    with pytest.raises(ConfigError):
        AISummaryPlugin({"enrich_top_k": bad})


# -- timeout ------------------------------------------------------------------


def test_timeout_enforced_via_model_timeout_s() -> None:
    hanging = _HangingModel()
    hanging.configure({"timeout_s": 0.05})
    plugin = AISummaryPlugin(model=hanging)
    started = time.perf_counter()
    with pytest.raises(PluginError) as exc_info:
        plugin.run(_context(1))
    elapsed = time.perf_counter() - started
    assert elapsed < 2.0
    cause = exc_info.value.__cause__
    assert isinstance(cause, ProviderError)
    assert cause.code == ProviderError.TIMEOUT
    assert cause.provider_name == "hanging"


def test_timeout_enforced_via_plugin_timeout_s() -> None:
    plugin = AISummaryPlugin({"timeout_s": 0.05}, model=_HangingModel())
    with pytest.raises(PluginError) as exc_info:
        plugin.run(_context(1))
    cause = exc_info.value.__cause__
    assert isinstance(cause, ProviderError)
    assert cause.code == ProviderError.TIMEOUT


@pytest.mark.parametrize("bad", [0, -1, 301, "soon", True])
def test_timeout_s_validated(bad: Any) -> None:
    with pytest.raises(ConfigError):
        AISummaryPlugin({"timeout_s": bad})


def test_timeout_request_still_succeeds_minus_ai_fields_via_diagnostics() -> None:
    hanging = _HangingModel()
    hanging.configure({"timeout_s": 0.05})
    plugin = AISummaryPlugin(model=hanging)
    context = Pipeline([plugin]).run(_context(3))
    # Request succeeds, results survive, no AI fields were attached.
    assert [r.id for r in context.results] == ["1", "2", "3"]
    for result in context.results:
        assert "ai_summary" not in result.metadata
        assert "ai_perspectives" not in result.metadata
        assert "ai_model" not in result.metadata
    diagnostics = context.diagnostics()
    executions = diagnostics["plugin_executions"]
    assert len(executions) == 1
    assert executions[0]["plugin"] == "ai-summary"
    assert executions[0]["status"] == "failed"
    assert executions[0]["error"]
    assert "timed out" in executions[0]["error"].lower()
    assert diagnostics["errors"]

"""Engine fill loop: survival-rate depth projection.

The pagination fill loop refetches deeper when post-retrieval filters
thin the held window below the requested page. Each pass is a full
provider round trip plus a complete pipeline/ranker rerun (and an
expansion-variant fan-out), so blind doubling (10, 20, 40, 80, ...)
multiplies query time on heavily filtered modes. The loop instead
projects the depth that should cover the page from the measured
survival rate. Coverage and totals stay exact — only the step size
changes.
"""

from __future__ import annotations

from typing import Any

from openlens.core.context import SearchContext
from openlens.core.engine import SearchEngine
from openlens.core.interfaces import (
    PipelineStage,
    PluginType,
    SearchRequest,
    SearchResult,
)
from openlens.plugins.base import BasePlugin
from openlens.providers.base import BaseSearchProvider


def _hit(doc_id: str, score: float) -> SearchResult:
    return SearchResult(
        id=doc_id,
        title=f"title-{doc_id}",
        snippet=f"snippet-{doc_id}",
        original_score=score,
        final_score=score,
        url=f"https://{doc_id}.example.com/a/{doc_id}",
        provider="a",
    )


class _FakeBackend(BaseSearchProvider):
    """Canned hits honoring offset/limit; records fetch depths."""

    def __init__(self, hits: list[SearchResult]) -> None:
        super().__init__()
        self._hits = hits
        self.seen: list[tuple[int, int]] = []

    @property
    def name(self) -> str:
        return "a"

    def search(self, request: SearchRequest) -> list[SearchResult]:
        self.seen.append((request.offset, request.limit))
        start = request.offset or 0
        return list(self._hits)[start : start + request.limit]

    def health_check(self) -> bool:
        return True


class _ThinFilter(BasePlugin):
    """Keep 3 of every 10 hits by id suffix (deterministic per pass)."""

    @property
    def name(self) -> str:
        return "thin-filter"

    @property
    def plugin_type(self) -> PluginType:
        return PluginType.FILTER

    @property
    def stage(self) -> PipelineStage:
        return PipelineStage.FILTERING

    def run(self, context: SearchContext) -> SearchContext:
        context.results = [
            hit for hit in context.results if int(hit.id.rsplit("-", 1)[-1]) % 10 < 3
        ]
        return context


class _DropAllFilter(BasePlugin):
    @property
    def name(self) -> str:
        return "drop-all"

    @property
    def plugin_type(self) -> PluginType:
        return PluginType.FILTER

    @property
    def stage(self) -> PipelineStage:
        return PipelineStage.FILTERING

    def run(self, context: SearchContext) -> SearchContext:
        context.results = []
        return context


def _engine(backend: _FakeBackend, plugins: list[Any]) -> SearchEngine:
    return SearchEngine(providers=[backend], plugins=plugins)


def test_fill_loop_projects_depth_from_survival_rate() -> None:
    """30% survival: one projected jump covers the page in 2 passes.

    Blind doubling would need 10 -> 20 -> 40 (3 provider round trips
    plus 3 pipeline reruns); the projection (ceil(10 / 0.3 * 1.5) = 50)
    covers it in 2.
    """
    hits = [_hit(f"a-{i}", score=1.0 / (i + 1)) for i in range(200)]
    backend = _FakeBackend(hits)
    engine = _engine(backend, [_ThinFilter()])
    response = engine.search(SearchRequest(query="quantum zebra", limit=10))
    assert len(response.results) == 10
    assert response.total >= 10
    assert backend.seen == [(0, 10), (0, 50)]


def test_fill_loop_doubles_when_nothing_survives() -> None:
    """Zero survival carries no signal: fall back to doubling and still
    terminate on provider exhaustion."""
    hits = [_hit(f"a-{i}", score=1.0 / (i + 1)) for i in range(30)]
    backend = _FakeBackend(hits)
    engine = _engine(backend, [_DropAllFilter()])
    response = engine.search(SearchRequest(query="quantum zebra", limit=10))
    assert response.results == []
    # No survival rate to project from: 10 -> 20 -> 40, where the
    # 30-hit corpus finally exhausts.
    assert backend.seen == [(0, 10), (0, 20), (0, 40)]


def test_fill_loop_stops_immediately_on_exhaustion() -> None:
    """A corpus smaller than the first depth stops after one pass even
    with zero survivors."""
    hits = [_hit(f"a-{i}", score=1.0 / (i + 1)) for i in range(5)]
    backend = _FakeBackend(hits)
    engine = _engine(backend, [_DropAllFilter()])
    response = engine.search(SearchRequest(query="quantum zebra", limit=10))
    assert response.results == []
    assert backend.seen == [(0, 10)]


def test_fill_loop_single_pass_without_thinning() -> None:
    """Unfiltered requests still pay exactly one provider round trip."""
    hits = [_hit(f"a-{i}", score=1.0 / (i + 1)) for i in range(30)]
    backend = _FakeBackend(hits)
    engine = _engine(backend, [])
    response = engine.search(SearchRequest(query="quantum zebra", limit=10))
    assert len(response.results) == 10
    assert backend.seen == [(0, 10)]

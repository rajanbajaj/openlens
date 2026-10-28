"""Cache engine hook: hit/miss, staleness, fail-open, safety (Task 21)."""

from __future__ import annotations

from typing import Any

from openlens.cache.base import CacheProvider
from openlens.cache.memory import MemoryCacheProvider
from openlens.core.engine import SearchEngine
from openlens.core.interfaces import SearchRequest, SearchResult
from openlens.observability.metrics import MetricsRegistry
from openlens.plugins.safety_filter import SafetyFilter
from openlens.providers.base import BaseSearchProvider


class _FakeBackend(BaseSearchProvider):
    """Canned hits; counts calls to prove hit/miss behavior."""

    def __init__(self, hits: list[SearchResult]) -> None:
        super().__init__()
        self._hits = hits
        self.calls = 0

    @property
    def name(self) -> str:
        return "fake"

    def search(self, request: SearchRequest) -> list[SearchResult]:
        self.calls += 1
        return [hit for hit in self._hits]

    def health_check(self) -> bool:
        return True


class _ExplodingCache(CacheProvider):
    """Backend that always fails: the engine must degrade, never raise."""

    def get(self, key: str) -> str | None:
        raise ConnectionError("cache down")

    def set(self, key: str, value: str, ttl_s: int) -> None:
        raise ConnectionError("cache down")

    def invalidate(self, key: str) -> None:
        raise ConnectionError("cache down")

    def clear(self) -> int:
        raise ConnectionError("cache down")

    def health_check(self) -> bool:
        return False

    def close(self) -> None:
        pass


def _hit(doc_id: str, title: str = "title") -> SearchResult:
    return SearchResult(
        id=doc_id,
        title=f"{title}-{doc_id}",
        snippet=f"snippet-{doc_id}",
        original_score=1.0,
        final_score=1.0,
        url=f"https://example.com/{doc_id}",
        provider="fake",
    )


def _request(query: str = "red shoes", **overrides: Any) -> SearchRequest:
    params: dict[str, Any] = {
        "query": query,
        "mode": "default",
        "limit": 10,
        "offset": 0,
    }
    params.update(overrides)
    return SearchRequest(**params)


def test_hit_serves_byte_identical_results_without_retrieval() -> None:
    provider = _FakeBackend([_hit("a"), _hit("b")])
    engine = SearchEngine(providers=[provider], plugins=[], cache=MemoryCacheProvider())
    first = engine.search(_request())
    assert first.diagnostics["cache"] == {"status": "miss"}
    first_calls = provider.calls
    assert first_calls >= 1  # expansion fan-out may retrieve variants too
    second = engine.search(_request())
    assert second.diagnostics["cache"] == {"status": "hit"}
    assert provider.calls == first_calls
    assert [hit.id for hit in second.results] == ["a", "b"]
    assert second.total == first.total == 2
    assert [hit.final_score for hit in second.results] == [
        hit.final_score for hit in first.results
    ]


def test_invalidate_forces_miss_after_write() -> None:
    provider = _FakeBackend([_hit("a")])
    engine = SearchEngine(providers=[provider], plugins=[], cache=MemoryCacheProvider())
    engine.search(_request())
    first_calls = provider.calls
    engine.search(_request())
    assert provider.calls == first_calls
    engine.invalidate_cache()  # same-process index write path
    response = engine.search(_request())
    assert provider.calls == 2 * first_calls
    assert response.diagnostics["cache"] == {"status": "miss"}


def test_zero_ttl_never_stores() -> None:
    provider = _FakeBackend([_hit("a")])
    engine = SearchEngine(
        providers=[provider], plugins=[], cache=MemoryCacheProvider(), cache_ttl_s=0
    )
    engine.search(_request())
    first_search_calls = provider.calls
    assert first_search_calls >= 1
    engine.search(_request())
    assert provider.calls == 2 * first_search_calls


def test_broken_cache_fails_open_with_metric_status() -> None:
    provider = _FakeBackend([_hit("a")])
    engine = SearchEngine(providers=[provider], plugins=[], cache=_ExplodingCache())
    first = engine.search(_request())
    first_calls = provider.calls
    second = engine.search(_request())
    assert [hit.id for hit in first.results] == [hit.id for hit in second.results]
    assert [hit.id for hit in second.results] == ["a"]
    # Uncached every time, never an error.
    assert provider.calls == 2 * first_calls
    assert second.diagnostics["cache"] == {"status": "error"}
    engine.invalidate_cache()  # also swallowed


def test_blocked_stays_blocked_on_hit() -> None:
    safety = SafetyFilter()
    safety.configure({"blocked_terms": ["blockedword"], "blocked_domains": []})
    provider = _FakeBackend(
        [_hit("good", "fine title"), _hit("bad", "blockedword here")]
    )
    engine = SearchEngine(
        providers=[provider], plugins=[safety], cache=MemoryCacheProvider()
    )
    first = engine.search(_request())
    assert [hit.id for hit in first.results] == ["good"]
    first_calls = provider.calls
    second = engine.search(_request())
    assert second.diagnostics["cache"] == {"status": "hit"}
    assert [hit.id for hit in second.results] == ["good"]
    assert provider.calls == first_calls


def test_restriction_sets_never_share_entries() -> None:
    provider = _FakeBackend([_hit("a")])
    engine = SearchEngine(providers=[provider], plugins=[], cache=MemoryCacheProvider())
    engine.search(_request(filters={"max_price": 100}))
    first_calls = provider.calls
    engine.search(_request(filters={"max_price": 100}))
    assert provider.calls == first_calls
    engine.search(_request(filters={"max_price": 50}))
    assert provider.calls == 2 * first_calls


def test_cache_outcomes_reach_metrics_with_fixed_labels() -> None:
    provider = _FakeBackend([_hit("a")])
    engine = SearchEngine(providers=[provider], plugins=[], cache=MemoryCacheProvider())
    registry = MetricsRegistry(enabled=True)
    registry.observe(engine.search(_request()).diagnostics)
    registry.observe(engine.search(_request()).diagnostics)
    text = registry.render()
    assert 'openlens_cache_total{status="hit"} 1' in text
    assert 'openlens_cache_total{status="miss"} 1' in text
    assert "red shoes" not in text  # no query text ever becomes a label

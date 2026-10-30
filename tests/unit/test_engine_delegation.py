"""Engine delegation: manager-backed SearchEngine keeps single/fallback
semantics, enforces timeouts on the live path, and slices pages exactly
once with a pre-slice total."""

from __future__ import annotations

import time
from typing import Any

import pytest

from openlens.core.engine import SearchEngine
from openlens.core.interfaces import (
    ConfigError,
    ProviderError,
    ProviderPrefs,
    SearchRequest,
    SearchResult,
)
from openlens.main import build_engine
from openlens.providers.base import BaseSearchProvider
from openlens.providers.manager import ProviderManager


def _hit(doc_id: str, provider: str, score: float = 1.0) -> SearchResult:
    return SearchResult(
        id=doc_id,
        title=f"title-{doc_id}",
        snippet=f"snippet-{doc_id}",
        original_score=score,
        final_score=score,
        url=f"https://example.com/{provider}/{doc_id}",
        provider=provider,
    )


class _FakeBackend(BaseSearchProvider):
    """Offline provider with canned hits, optional delay/failure, paging."""

    def __init__(
        self,
        entry_name: str = "fake",
        hits: tuple[SearchResult, ...] | list[SearchResult] = (),
        delay_s: float = 0.0,
        error: BaseException | None = None,
    ) -> None:
        super().__init__()
        self._entry_name = entry_name
        self._hits = list(hits)
        self._delay_s = delay_s
        self._error = error
        self.seen: list[tuple[int, int]] = []

    @property
    def name(self) -> str:
        return self._entry_name

    def search(self, request: SearchRequest) -> list[SearchResult]:
        self.seen.append((request.offset, request.limit))
        if self._delay_s:
            time.sleep(self._delay_s)
        if self._error is not None:
            raise self._error
        start = request.offset or 0
        return list(self._hits)[start : start + request.limit]

    def health_check(self) -> bool:
        return True


def _paged_hits(entry: str, count: int) -> list[SearchResult]:
    return [_hit(f"{entry}-{i}", entry, score=1.0 / (i + 1)) for i in range(count)]


def _single_manager(
    hits: list[SearchResult], entry: str = "a", **kwargs: Any
) -> ProviderManager:
    return ProviderManager(
        {entry: _FakeBackend(entry, hits)}, entry, "single", **kwargs
    )


def _federated_manager(backends: dict[str, _FakeBackend]) -> ProviderManager:
    names = list(backends)
    return ProviderManager(dict(backends), names[0], "federated")


# ---------------------------------------------------------------------------
# Single/fallback unchanged after delegation
# ---------------------------------------------------------------------------


def test_single_unchanged_through_manager() -> None:
    engine = SearchEngine.from_manager(_single_manager(_paged_hits("a", 4)))
    resp = engine.search(SearchRequest(query="quantum zebra"))
    assert sorted(h.id for h in resp.results) == [f"a-{i}" for i in range(4)]
    assert resp.total == 4
    assert resp.diagnostics["errors"] == []


def test_fallback_unchanged_through_manager() -> None:
    broken = _FakeBackend("broken", error=ValueError("boom"))
    manager = ProviderManager(
        {
            "broken": broken,
            "fb": _FakeBackend("fb", [_hit("fb-0", "fb")]),
        },
        "broken",
        "fallback",
        fallback="fb",
    )
    engine = SearchEngine.from_manager(manager)
    resp = engine.search(SearchRequest(query="quantum zebra"))
    assert [h.id for h in resp.results] == ["fb-0"]
    assert resp.total == 1
    assert resp.diagnostics["fallback_used"] is True
    assert any("broken" in err for err in resp.diagnostics["errors"])


def test_manager_kwarg_matches_from_manager() -> None:
    manager = _single_manager([_hit("only", "a")])
    via_kwarg = SearchEngine(manager=manager)
    via_factory = SearchEngine.from_manager(manager)
    assert [h.id for h in via_kwarg.search(SearchRequest(query="q")).results] == [
        "only"
    ]
    assert [h.id for h in via_factory.search(SearchRequest(query="q")).results] == [
        "only"
    ]


def test_manager_and_providers_together_rejected() -> None:
    manager = _single_manager([_hit("only", "a")])
    with pytest.raises(ConfigError):
        SearchEngine(providers=[_FakeBackend("x")], manager=manager)


def test_health_check_delegates_to_manager() -> None:
    manager = _single_manager([_hit("only", "a")])
    assert SearchEngine.from_manager(manager).health_check() == {"a": True}


# ---------------------------------------------------------------------------
# Timeout bound is on the live path after delegation
# ---------------------------------------------------------------------------


def test_slow_provider_times_out_through_engine() -> None:
    slow = _FakeBackend("slow", delay_s=5.0)
    slow.configure({"timeout_s": 0.2})
    engine = SearchEngine.from_manager(
        ProviderManager({"slow": slow}, "slow", "single")
    )
    start = time.monotonic()
    with pytest.raises(ProviderError) as exc_info:
        engine.search(SearchRequest(query="quantum zebra"))
    elapsed = time.monotonic() - start
    assert exc_info.value.code == ProviderError.TIMEOUT
    assert elapsed < 4  # bound enforced; far below the 5s hang


# ---------------------------------------------------------------------------
# Paging: one slice after ranking, pre-slice total (all strategies)
# ---------------------------------------------------------------------------


def test_single_paging_slices_once_with_pre_slice_total() -> None:
    backend = _FakeBackend("a", _paged_hits("a", 8))
    engine = SearchEngine.from_manager(ProviderManager({"a": backend}, "a", "single"))
    full = engine.search(SearchRequest(query="quantum zebra", limit=8, offset=0))
    assert full.total == 8
    assert len(full.results) == 8
    page = engine.search(SearchRequest(query="quantum zebra", limit=3, offset=2))
    assert [h.id for h in page.results] == [h.id for h in full.results[2:5]]
    # Pre-slice merged count: the single-strategy window is the fetch depth
    # (offset+limit), not the corpus size.
    assert page.total == 5
    assert all(seen == (0, 5) or seen == (0, 8) for seen in backend.seen)


def test_federated_paging_slices_once_with_pre_slice_total() -> None:
    backends = {
        "a": _FakeBackend("a", _paged_hits("a", 5)),
        "b": _FakeBackend("b", _paged_hits("b", 5)),
    }
    engine = SearchEngine.from_manager(_federated_manager(backends))
    full = engine.search(SearchRequest(query="quantum zebra", limit=10, offset=0))
    assert full.total == 10
    assert len(full.results) == 10
    page = engine.search(SearchRequest(query="quantum zebra", limit=3, offset=2))
    assert [h.id for h in page.results] == [h.id for h in full.results[2:5]]
    assert page.total == 10
    for backend in backends.values():
        assert all(offset == 0 for offset, _ in backend.seen)


def test_legacy_list_constructor_slices_once_with_pre_slice_total() -> None:
    backend = _FakeBackend("a", _paged_hits("a", 8))
    engine = SearchEngine(providers=[backend])
    full = engine.search(SearchRequest(query="quantum zebra", limit=8, offset=0))
    assert full.total == 8
    page = engine.search(SearchRequest(query="quantum zebra", limit=3, offset=2))
    assert [h.id for h in page.results] == [h.id for h in full.results[2:5]]
    # Pre-slice merged count: the legacy window is the fetch depth.
    assert page.total == 5
    assert all(seen == (0, 5) or seen == (0, 8) for seen in backend.seen)


def test_prefs_flow_through_engine_to_manager() -> None:
    backends = {
        "a": _FakeBackend("a", [_hit("a1", "a")]),
        "b": _FakeBackend("b", [_hit("b1", "b")]),
    }
    engine = SearchEngine.from_manager(_federated_manager(backends))
    resp = engine.search(
        SearchRequest(
            query="quantum zebra",
            provider_prefs=ProviderPrefs(include=("b",)),
        )
    )
    assert [h.id for h in resp.results] == ["b1"]
    assert backends["a"].seen == []


# ---------------------------------------------------------------------------
# build_engine takes the manager path
# ---------------------------------------------------------------------------


class _TotalingBackend(_FakeBackend):
    """Fake reporting a repository-style total larger than its window."""

    def __init__(self, entry_name: str, hits: list[SearchResult], total: int) -> None:
        super().__init__(entry_name, hits)
        self._total = total

    def search_with_total(
        self, request: SearchRequest
    ) -> tuple[list[SearchResult], int]:
        return self.search(request), self._total


def test_provider_total_beats_window_size() -> None:
    """A reported provider total sets the response total (pagination).

    The window holds 2 hits of 25 reported matches; the response keeps
    the 2-hit page but reports the true total.
    """
    backend = _TotalingBackend("a", _paged_hits("a", 10), total=25)
    engine = SearchEngine.from_manager(ProviderManager({"a": backend}, "a", "single"))
    resp = engine.search(SearchRequest(query="quantum zebra", limit=2, offset=0))
    assert len(resp.results) == 2
    assert resp.total == 25
    second = engine.search(SearchRequest(query="quantum zebra", limit=2, offset=2))
    assert second.total == 25


def test_legacy_backend_without_totals_keeps_window_count() -> None:
    """Providers without ``search_with_total`` keep the len() behavior."""
    backend = _FakeBackend("a", _paged_hits("a", 8))
    engine = SearchEngine.from_manager(ProviderManager({"a": backend}, "a", "single"))
    page = engine.search(SearchRequest(query="quantum zebra", limit=3, offset=2))
    assert page.total == 5


def test_build_engine_uses_manager_path() -> None:
    config: dict[str, Any] = {
        "providers": {
            "strategy": "single",
            "primary": "custom",
            "custom": {
                "enabled": True,
                "type": "custom_index",
                "db_path": ":memory:",
            },
        },
        "plugins": [],
        "ranking": {"default_profile": "default"},
    }
    engine = build_engine(config)
    # Test-only introspection: the engine must delegate selection to the
    # manager rather than holding a private provider list.
    assert engine._manager is not None
    assert engine._manager.strategy == "single"
    assert engine._providers == []

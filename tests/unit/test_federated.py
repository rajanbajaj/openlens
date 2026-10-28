"""Unit tests for retrieval.fusion (RRF rank-only merging) plus the
federated manager strategy (parallel fan-out, timeouts, prefs selection,
fetch-depth paging ownership)."""

from __future__ import annotations

import time
from typing import Any

import pytest

from openlens.core.context import SearchContext
from openlens.core.interfaces import (
    ConfigError,
    ProviderError,
    ProviderPrefs,
    SearchRequest,
    SearchResult,
)
from openlens.providers import registry
from openlens.providers.base import BaseSearchProvider, MAX_TIMEOUT_S
from openlens.providers.manager import ProviderManager
from openlens.retrieval.fusion import fuse


def _hit(
    doc_id: str,
    provider: str,
    url: str | None = None,
    score: float = 1.0,
) -> SearchResult:
    return SearchResult(
        id=doc_id,
        title=f"title-{doc_id}",
        snippet=f"snippet-{doc_id}",
        original_score=score,
        final_score=score,
        url=url if url is not None else f"https://example.com/{doc_id}",
        provider=provider,
    )


def test_rank_only_merging_ignores_scores() -> None:
    # B's raw scores dwarf A's, but ranks decide: shared doc X (rank 1 in
    # both lists) outscores every single-provider doc, and the rank-1
    # singletons tie on RRF so provider name breaks the tie (a before b).
    list_a = [_hit("x", "a", score=0.001), _hit("a2", "a", score=0.002)]
    list_b = [_hit("x", "b", score=999.0), _hit("b2", "b", score=998.0)]
    merged = fuse({"a": list_a, "b": list_b})
    assert [h.id for h in merged] == ["x", "a2", "b2"]
    expected_shared = 1 / 61 + 1 / 61
    assert merged[0].metadata["rrf_score"] == expected_shared
    assert merged[0].final_score == expected_shared
    assert merged[0].original_score == 0.001


def test_same_url_merge_keeps_best_rank_and_records_also_from() -> None:
    # Same URL modulo case/trailing slash; best rank is b's rank-1 hit, so
    # b's object is kept and "a" lands in also_from.
    from_a = _hit("x-from-a", "a", url="https://Example.com/X/")
    other_a = _hit("other", "a")
    from_b = _hit("x-from-b", "b", url="https://example.com/x")
    merged = fuse({"a": [other_a, from_a], "b": [from_b]})
    assert [h.id for h in merged] == ["x-from-b", "other"]
    top = merged[0]
    assert top.provider == "b"
    assert top.metadata["also_from"] == ["a"]
    assert top.metadata["rrf_score"] == 1 / 62 + 1 / 61
    assert top.final_score == top.metadata["rrf_score"]
    assert top.original_score == from_b.original_score
    assert merged[1].metadata["also_from"] == []


def test_weight_config_changes_order() -> None:
    list_a = [_hit("a1", "a", score=0.1)]
    list_b = [_hit("b1", "b", score=0.1)]
    assert [h.id for h in fuse({"a": list_a, "b": list_b})] == ["a1", "b1"]
    weighted = fuse({"a": list_a, "b": list_b}, weights={"b": 5.0})
    assert [h.id for h in weighted] == ["b1", "a1"]
    assert weighted[0].metadata["rrf_score"] == 5.0 / 61


def test_tie_break_is_deterministic() -> None:
    # Three rank-1 singletons tie on fused score; provider asc dominates
    # (c's lexicographically-smallest id still sorts last), and repeated
    # calls return the identical order.
    ranked = {
        "b": [_hit("b-doc", "b")],
        "c": [_hit("aaa", "c")],
        "a": [_hit("zzz", "a")],
    }
    first = [h.id for h in fuse(ranked)]
    assert first == ["zzz", "b-doc", "aaa"]
    second = [h.id for h in fuse(ranked)]
    assert second == first


def test_empty_inputs() -> None:
    assert fuse({}) == []
    assert fuse({"a": [], "b": []}) == []
    survivor = [_hit("only", "b")]
    merged = fuse({"a": [], "b": survivor})
    assert [h.id for h in merged] == ["only"]
    assert merged[0].metadata["also_from"] == []


# ---------------------------------------------------------------------------
# Federated manager strategy: fakes + fan-out + fusion order
# ---------------------------------------------------------------------------


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


class _ConfigFake(BaseSearchProvider):
    """Registry-constructible fake for from_config federated tests."""

    @property
    def name(self) -> str:
        return "cfg_fake"

    def search(self, request: SearchRequest) -> list[SearchResult]:
        return []

    def health_check(self) -> bool:
        return True


def _federated_manager(
    backends: dict[str, _FakeBackend],
    primary: str | None = None,
    **kwargs: Any,
) -> ProviderManager:
    names = list(backends)
    return ProviderManager(
        dict(backends),
        primary or names[0],
        "federated",
        **kwargs,
    )


def _paged_hits(entry: str, count: int) -> list[SearchResult]:
    return [_hit(f"{entry}-{i}", entry, score=1.0 / (i + 1)) for i in range(count)]


@pytest.fixture()
def _cfg_registry():
    registry.register_builtin_providers()
    registry.register("cfg_fake", _ConfigFake)
    yield registry
    registry.unregister("cfg_fake")


def test_federated_fans_out_and_matches_fuse_order() -> None:
    a_hits = [_hit("x", "a", score=0.001), _hit("a2", "a", score=0.002)]
    b_hits = [_hit("x", "b", score=999.0), _hit("b2", "b", score=998.0)]
    manager = _federated_manager(
        {"a": _FakeBackend("a", a_hits), "b": _FakeBackend("b", b_hits)}
    )
    ctx = SearchContext(request=SearchRequest(query="q"))
    merged = manager.search(SearchRequest(query="q"), context=ctx)
    assert [h.id for h in merged] == [h.id for h in fuse({"a": a_hits, "b": b_hits})]
    assert set(ctx.provider_latencies_ms) == {"a", "b"}
    assert ctx.errors == []


def test_federated_merges_shared_url_with_attribution() -> None:
    from_a = _hit("x-from-a", "a", url="https://Example.com/X/")
    from_b = _hit("x-from-b", "b", url="https://example.com/x")
    manager = _federated_manager(
        {
            "a": _FakeBackend("a", [_hit("other", "a"), from_a]),
            "b": _FakeBackend("b", [from_b]),
        }
    )
    merged = manager.search(SearchRequest(query="q"))
    assert [h.id for h in merged] == ["x-from-b", "other"]
    assert merged[0].provider == "b"
    assert merged[0].metadata["also_from"] == ["a"]


def test_federated_partial_failure_degrades_with_errors_recorded() -> None:
    broken = _FakeBackend("broken", error=ValueError("raw client boom"))
    manager = _federated_manager(
        {
            "fast": _FakeBackend("fast", [_hit("only", "fast")]),
            "broken": broken,
        }
    )
    ctx = SearchContext(request=SearchRequest(query="q"))
    merged = manager.search(SearchRequest(query="q"), context=ctx)
    assert [h.id for h in merged] == ["only"]
    assert any("broken" in err for err in ctx.errors)
    assert set(ctx.provider_latencies_ms) == {"fast", "broken"}


def test_federated_all_failed_raises_naming_every_provider() -> None:
    manager = _federated_manager(
        {
            "a": _FakeBackend("a", error=ValueError("boom-a")),
            "b": _FakeBackend("b", error=ProviderError("down", provider_name="b")),
        }
    )
    ctx = SearchContext(request=SearchRequest(query="q"))
    with pytest.raises(ProviderError) as exc_info:
        manager.search(SearchRequest(query="q"), context=ctx)
    assert exc_info.value.code == ProviderError.UNAVAILABLE
    assert "a" in str(exc_info.value) and "b" in str(exc_info.value)
    assert len(ctx.errors) == 2
    assert any("a" in err for err in ctx.errors)
    assert any("b" in err for err in ctx.errors)


def test_federated_empty_from_all_is_success() -> None:
    manager = _federated_manager({"a": _FakeBackend("a"), "b": _FakeBackend("b")})
    assert manager.search(SearchRequest(query="q")) == []


def test_federated_per_provider_timeout_degrades() -> None:
    slow = _FakeBackend("slow", delay_s=5.0)
    slow.configure({"timeout_s": 0.2})
    manager = _federated_manager(
        {
            "slow": slow,
            "fast": _FakeBackend("fast", [_hit("only", "fast")]),
        }
    )
    ctx = SearchContext(request=SearchRequest(query="q"))
    start = time.monotonic()
    merged = manager.search(SearchRequest(query="q"), context=ctx)
    elapsed = time.monotonic() - start
    assert [h.id for h in merged] == ["only"]
    assert any("slow" in err and "timed out" in err for err in ctx.errors)
    assert elapsed < 5  # far below the fake's 5s hang


def test_federated_overall_deadline_bounds_slow_providers() -> None:
    slow_a = _FakeBackend("a", delay_s=5.0)
    slow_a.configure({"timeout_s": 30.0})
    slow_b = _FakeBackend("b", delay_s=5.0)
    slow_b.configure({"timeout_s": 30.0})
    manager = _federated_manager({"a": slow_a, "b": slow_b}, federated_timeout_s=0.3)
    start = time.monotonic()
    with pytest.raises(ProviderError) as exc_info:
        manager.search(SearchRequest(query="q"))
    elapsed = time.monotonic() - start
    assert exc_info.value.code == ProviderError.UNAVAILABLE
    assert "a" in str(exc_info.value) and "b" in str(exc_info.value)
    assert elapsed < 5  # overall deadline, not the 30s per-provider bound


def test_federated_default_deadline_is_max_bound_plus_one() -> None:
    fast = _FakeBackend("a", [_hit("only", "a")])
    fast.configure({"timeout_s": 0.5})
    other = _FakeBackend("b", [_hit("b1", "b")])
    other.configure({"timeout_s": 0.2})
    manager = _federated_manager({"a": fast, "b": other})
    assert manager.federated_timeout_s is None
    merged = manager.search(SearchRequest(query="q"))
    assert [h.id for h in merged] == ["only", "b1"]


def test_federated_max_merged_caps_window() -> None:
    manager = _federated_manager(
        {
            "a": _FakeBackend("a", _paged_hits("a", 5)),
            "b": _FakeBackend("b", _paged_hits("b", 5)),
        },
        federated_max_merged=3,
    )
    merged = manager.search(SearchRequest(query="q"))
    assert len(merged) == 3


@pytest.mark.parametrize("bad", [0, -1, True, "5", MAX_TIMEOUT_S + 1])
def test_federated_timeout_s_validated(bad: Any) -> None:
    with pytest.raises(ConfigError):
        ProviderManager(
            {"a": _FakeBackend("a")}, "a", "federated", federated_timeout_s=bad
        )


@pytest.mark.parametrize("bad", [0, -1, True, "x"])
def test_federated_boost_validated(bad: Any) -> None:
    with pytest.raises(ConfigError):
        ProviderManager(
            {"a": _FakeBackend("a")}, "a", "federated", federated_primary_boost=bad
        )


@pytest.mark.parametrize("bad", [0, -1, True, "3", 2.5])
def test_federated_max_merged_validated(bad: Any) -> None:
    with pytest.raises(ConfigError):
        ProviderManager(
            {"a": _FakeBackend("a")}, "a", "federated", federated_max_merged=bad
        )


def test_unknown_strategy_still_rejected() -> None:
    with pytest.raises(ConfigError):
        ProviderManager({"a": _FakeBackend("a")}, "a", "round_robin")


# ---------------------------------------------------------------------------
# Preferences selection: include / exclude / primary boost
# ---------------------------------------------------------------------------


def test_prefs_include_restricts_fanout() -> None:
    backends = {
        "a": _FakeBackend("a", [_hit("a1", "a")]),
        "b": _FakeBackend("b", [_hit("b1", "b")]),
    }
    manager = _federated_manager(backends)
    merged = manager.search(
        SearchRequest(query="q", provider_prefs=ProviderPrefs(include=("a",)))
    )
    assert [h.id for h in merged] == ["a1"]
    assert backends["b"].seen == []


def test_prefs_exclude_removes_provider() -> None:
    backends = {
        "a": _FakeBackend("a", [_hit("a1", "a")]),
        "b": _FakeBackend("b", [_hit("b1", "b")]),
    }
    manager = _federated_manager(backends)
    merged = manager.search(
        SearchRequest(query="q", provider_prefs=ProviderPrefs(exclude=("a",)))
    )
    assert [h.id for h in merged] == ["b1"]
    assert backends["a"].seen == []


def test_prefs_primary_boosts_fused_order() -> None:
    manager = _federated_manager(
        {
            "a": _FakeBackend("a", [_hit("x", "a", score=0.001)]),
            "b": _FakeBackend("b", [_hit("y", "b", score=999.0)]),
        }
    )
    plain = manager.search(SearchRequest(query="q"))
    assert [h.id for h in plain] == ["x", "y"]  # tie broken by provider name
    boosted = manager.search(
        SearchRequest(query="q", provider_prefs=ProviderPrefs(primary="b"))
    )
    assert [h.id for h in boosted] == ["y", "x"]
    assert boosted[0].metadata["rrf_score"] == pytest.approx(1.5 / 61)


def test_prefs_custom_boost_honored() -> None:
    manager = _federated_manager(
        {
            "a": _FakeBackend("a", [_hit("x", "a")]),
            "b": _FakeBackend("b", [_hit("y", "b")]),
        },
        federated_primary_boost=5.0,
    )
    boosted = manager.search(
        SearchRequest(query="q", provider_prefs=ProviderPrefs(primary="b"))
    )
    assert [h.id for h in boosted] == ["y", "x"]
    assert boosted[0].metadata["rrf_score"] == pytest.approx(5.0 / 61)


@pytest.mark.parametrize(
    "prefs",
    [
        ProviderPrefs(include=("nope",)),
        ProviderPrefs(exclude=("nope",)),
        ProviderPrefs(primary="nope"),
        ProviderPrefs(include=("a",), exclude=("a",)),
        ProviderPrefs(include=("a",), primary="b"),
    ],
)
def test_prefs_bad_selection_rejected(prefs: ProviderPrefs) -> None:
    manager = _federated_manager(
        {
            "a": _FakeBackend("a", [_hit("a1", "a")]),
            "b": _FakeBackend("b", [_hit("b1", "b")]),
        }
    )
    with pytest.raises(ConfigError):
        manager.search(SearchRequest(query="q", provider_prefs=prefs))


def test_prefs_unknown_name_rejected_on_single_strategy() -> None:
    manager = ProviderManager(
        {"a": _FakeBackend("a", [_hit("a1", "a")])}, "a", "single"
    )
    with pytest.raises(ConfigError):
        manager.search(
            SearchRequest(query="q", provider_prefs=ProviderPrefs(include=("nope",)))
        )


# ---------------------------------------------------------------------------
# Paging ownership: fetch depth at the manager, single slice in the engine
# ---------------------------------------------------------------------------


def test_federated_queries_fetch_depth_with_offset_zero() -> None:
    backends = {
        "a": _FakeBackend("a", _paged_hits("a", 5)),
        "b": _FakeBackend("b", _paged_hits("b", 5)),
    }
    manager = _federated_manager(backends)
    merged = manager.search(SearchRequest(query="q", limit=3, offset=2))
    assert backends["a"].seen == [(0, 5)]
    assert backends["b"].seen == [(0, 5)]
    assert len(merged) == 10  # full fused window; the engine slices the page


def test_single_strategy_queries_fetch_depth() -> None:
    backend = _FakeBackend("a", _paged_hits("a", 8))
    manager = ProviderManager({"a": backend}, "a", "single")
    window = manager.search(SearchRequest(query="q", limit=3, offset=2))
    assert backend.seen == [(0, 5)]
    assert [h.id for h in window] == [f"a-{i}" for i in range(5)]


def test_fallback_strategy_queries_fetch_depth() -> None:
    broken = _FakeBackend("broken", error=ValueError("boom"))
    backend = _FakeBackend("fb", _paged_hits("fb", 8))
    manager = ProviderManager(
        {"broken": broken, "fb": backend}, "broken", "fallback", fallback="fb"
    )
    window = manager.search(SearchRequest(query="q", limit=3, offset=2))
    assert backend.seen == [(0, 5)]
    assert [h.id for h in window] == [f"fb-{i}" for i in range(5)]


# ---------------------------------------------------------------------------
# from_config: federated strategy + knobs
# ---------------------------------------------------------------------------


def _cfg_section(**overrides: Any) -> dict[str, Any]:
    section: dict[str, Any] = {
        "strategy": "federated",
        "primary": "a",
        "a": {"type": "cfg_fake", "enabled": True},
        "b": {"type": "cfg_fake", "enabled": True},
    }
    section.update(overrides)
    return {"providers": section}


def test_from_config_accepts_federated_strategy(_cfg_registry: Any) -> None:
    manager = ProviderManager.from_config(_cfg_section())
    assert manager.strategy == "federated"
    assert manager.provider_names == ["a", "b"]
    assert manager.search(SearchRequest(query="q")) == []


def test_from_config_parses_federated_knobs(_cfg_registry: Any) -> None:
    manager = ProviderManager.from_config(
        _cfg_section(
            federated_timeout_s=7.0,
            federated_primary_boost=2.0,
            federated_max_merged=50,
        )
    )
    assert manager.federated_timeout_s == 7.0
    assert manager.federated_primary_boost == 2.0
    assert manager.federated_max_merged == 50


@pytest.mark.parametrize(
    "overrides",
    [
        {"federated_timeout_s": 0},
        {"federated_timeout_s": -1},
        {"federated_timeout_s": True},
        {"federated_timeout_s": "5"},
        {"federated_timeout_s": MAX_TIMEOUT_S + 1},
        {"federated_primary_boost": 0},
        {"federated_primary_boost": "x"},
        {"federated_max_merged": 0},
        {"federated_max_merged": "3"},
    ],
)
def test_from_config_rejects_bad_federated_knobs(
    _cfg_registry: Any, overrides: dict[str, Any]
) -> None:
    with pytest.raises(ConfigError):
        ProviderManager.from_config(_cfg_section(**overrides))

"""Suggest vocabulary TTL cache: single scan per TTL window (perf)."""

from __future__ import annotations

import pytest

from openlens.core import engine as engine_module
from openlens.core.engine import SearchEngine


class _CountingSuggestProvider:
    """Stub exposing both suggest hooks; counts provider hits."""

    def __init__(self) -> None:
        self.terms_calls = 0
        self.counts_calls = 0

    @property
    def name(self) -> str:
        return "counting"

    def suggest_terms(self, limit: int = 200) -> list[str]:
        self.terms_calls += 1
        return ["Banana", "apple"][:limit]

    def suggest_terms_with_counts(self, limit: int = 2000) -> dict[str, int]:
        self.counts_calls += 1
        return {"apple": 3, "banana": 1}


class _ExplodingSuggestProvider:
    """Hooks always fail: the engine must degrade, never raise."""

    @property
    def name(self) -> str:
        return "exploding"

    def suggest_terms(self, limit: int = 200) -> list[str]:
        raise ConnectionError("index down")

    def suggest_terms_with_counts(self, limit: int = 2000) -> dict[str, int]:
        raise ConnectionError("index down")


def test_suggest_terms_cached_within_ttl() -> None:
    provider = _CountingSuggestProvider()
    eng = SearchEngine(providers=[provider])  # type: ignore[list-item]
    first = eng.suggest_terms(limit=10)
    second = eng.suggest_terms(limit=10)
    assert first == second
    assert provider.terms_calls == 1


def test_suggest_term_weights_cached_within_ttl() -> None:
    provider = _CountingSuggestProvider()
    eng = SearchEngine(providers=[provider])  # type: ignore[list-item]
    first = eng.suggest_term_weights(limit=100)
    second = eng.suggest_term_weights(limit=100)
    assert first == second == {"apple": 3, "banana": 1}
    assert provider.counts_calls == 1


def test_suggest_cache_expiry_refetches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _CountingSuggestProvider()
    eng = SearchEngine(providers=[provider])  # type: ignore[list-item]
    assert eng.suggest_term_weights(limit=100) == {
        "apple": 3,
        "banana": 1,
    }
    assert provider.counts_calls == 1
    monkeypatch.setattr(engine_module, "_SUGGEST_CACHE_TTL_S", 0)
    assert eng.suggest_term_weights(limit=100) == {
        "apple": 3,
        "banana": 1,
    }
    assert provider.counts_calls == 2


def test_suggest_failure_degrades_to_empty() -> None:
    eng = SearchEngine(
        providers=[_ExplodingSuggestProvider()]  # type: ignore[list-item]
    )
    assert eng.suggest_terms(limit=10) == []
    assert eng.suggest_term_weights(limit=100) == {}

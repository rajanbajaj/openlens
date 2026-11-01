"""Popularity-ranked suggestions: index doc-frequency + own-history boost.

Google ranks completions by query popularity (plus your own recent
searches first); offline we keep no query logs by design, so per-term
index document frequency is the popularity proxy and request-scoped
history supplies the personalization. Covers retrieval/suggest.py
weighted ordering, the provider/manager/engine count hooks, and the API
degrade path — all over injectable data, never the network.
"""

from __future__ import annotations

from typing import Any

from openlens.core.engine import SearchEngine
from openlens.providers.custom_index import CustomIndexProvider
from openlens.providers.manager import ProviderManager
from openlens.retrieval.suggest import (
    complete,
    correct_spelling,
    related_queries,
)


class _TitlesRepo:
    def __init__(self, titles: Any) -> None:
        self._titles = titles

    def list_titles(self, limit: int = 500) -> Any:
        if isinstance(self._titles, Exception):
            raise self._titles
        titles = self._titles
        if isinstance(titles, list):
            return titles[:limit]
        return titles


def _provider(titles: Any) -> CustomIndexProvider:
    return CustomIndexProvider(repository=_TitlesRepo(titles))  # type: ignore[arg-type]


def test_complete_prefers_higher_document_frequency() -> None:
    terms = ["wireless headphones", "wireless"]
    weights = {"wireless headphones": 9, "wireless": 1}
    assert complete("wire", terms, weights=weights, limit=5) == [
        "wireless headphones",
        "wireless",
    ]


def test_complete_falls_back_to_length_order_without_weights() -> None:
    terms = ["wireless headphones", "wireless"]
    assert complete("wire", terms, limit=5) == ["wireless", "wireless headphones"]


def test_complete_boosts_own_history_first_capped_at_two() -> None:
    terms = ["wireless headphones", "wireless mouse", "wireless charger"]
    history = ["wireless mouse", "wireless charger", "unrelated query"]
    assert complete("wire", terms, boost=history, limit=5) == [
        "wireless charger",
        "wireless mouse",
        "wireless headphones",
    ]
    crowded = ["wireless mouse", "wireless charger", "wireless headphones"]
    assert complete("wire", terms, boost=crowded, limit=5)[:2] == [
        "wireless headphones",
        "wireless charger",
    ]


def test_complete_boost_ignores_non_matches_and_never_raises() -> None:
    terms = ["wireless mouse"]
    assert complete("wire", terms, boost=["unrelated query"], limit=5) == [
        "wireless mouse"
    ]
    assert complete("wire", terms, boost=object(), limit=5) == ["wireless mouse"]
    assert complete("wire", terms, weights="nope", limit=5) == ["wireless mouse"]


def test_spelling_tie_prefers_popular_term() -> None:
    vocabulary = ["wired", "wires"]
    weights = {"wires": 7, "wired": 1}
    assert correct_spelling("wirex", vocabulary, weights=weights) == "wires"
    assert correct_spelling("wirex", vocabulary) == "wired"


def test_related_prefers_index_weights_over_static_counts() -> None:
    vocabulary = ["wireless mouse", "wireless charger"]
    counts = {"wireless mouse": 100, "wireless charger": 1}
    weights = {"wireless charger": 50}
    assert related_queries(
        "wireless", counts, vocabulary=vocabulary, weights=weights, limit=5
    ) == ["wireless charger", "wireless mouse"]


def test_provider_counts_documents_not_occurrences() -> None:
    provider = _provider(["Wireless Mouse", "wireless MOUSE", "Wired Keyboard"])
    counts = provider.suggest_terms_with_counts()
    folded = {term.casefold(): count for term, count in counts.items()}
    assert folded["wireless"] == 2
    assert folded["wireless mouse"] == 2
    assert folded["wired keyboard"] == 1


def test_provider_counts_degrade_to_empty() -> None:
    assert _provider(None).suggest_terms_with_counts() == {}
    assert _provider(RuntimeError("closed")).suggest_terms_with_counts() == {}
    assert _provider(["ok term"]).suggest_terms_with_counts(limit=0) == {}


def test_manager_and_engine_sum_counts() -> None:
    first = _provider(["Alpha Guide", "Beta Guide"])
    second = _provider(["Alpha Guide", "Gamma Guide"])
    manager = ProviderManager({"a": first, "b": second}, primary="a")
    totals = {
        term.casefold(): count for term, count in manager.suggest_term_weights().items()
    }
    assert totals["alpha guide"] == 2
    assert totals["beta guide"] == 1
    assert totals["gamma guide"] == 1

    engine = SearchEngine(providers=[first, second])  # type: ignore[arg-type]
    engine_totals = {
        term.casefold(): count for term, count in engine.suggest_term_weights().items()
    }
    assert engine_totals["alpha guide"] == 2
    assert SearchEngine().suggest_term_weights() == {}


def test_assistance_degrades_without_weights_or_history() -> None:
    from openlens.api.routes import _assistance

    suggestions, spelling, related = _assistance("wire", ["wireless mouse"])
    assert suggestions == ["wireless mouse"]
    assert spelling is None or isinstance(spelling, str)
    assert isinstance(related, list)

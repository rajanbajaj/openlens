"""Shopping/travel constraint reranker: budget/location/facilities ordering.

EXPLANATION-stage, optional. Reads the current request's ``filters``
(``max_price``, ``location``, ``facilities`` keys — reserved pass-through
by filter-pack T10; consumed here, never schema-validated) plus the
attribute enricher's normalized ``price``/``currency``/``location``/
``facilities`` metadata keys, and emits the ``commerce`` ordering signal
(0..1) consumed by the ``Ranker`` composite.

Signal math: the satisfied fraction of the requested constraints. Each
requested constraint contributes 1.0 (satisfied), 0.0 (unsatisfied), or
0.5 (neutral: currency mismatch, or missing attributes under the keyword
fallback); no constraints requested degrades to neutral 0.5. Prices
compare only when currencies match (or the filter carries no currency).
Location match is case-insensitive equality on the normalized value.
Unsatisfied constraints demote, never exclude (exclusion is
filter-pack's job). Missing attributes fall back to query-keyword
overlap with a reason saying so. At most one concise reason per result.
Deterministic: one in-order pass, no I/O, no randomness.

The pure helpers are stage-agnostic so synthesis can fold them into
``Ranker`` itself without rewriting the math.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from openlens.core.context import SearchContext
from openlens.core.interfaces import PipelineStage, PluginType, SearchResult
from openlens.plugins._signals import NEUTRAL_SIGNAL as NEUTRAL
from openlens.plugins._signals import set_signal
from openlens.plugins.base import BasePlugin

#: Signal key written into ``result.metadata["signals"]``.
SIGNAL_NAME = "commerce"

_TOKEN = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokens (keyword-fallback matching)."""
    return _TOKEN.findall(text.lower())


def _as_float(value: Any) -> Optional[float]:
    """Coerce a normalized price to float; None when absent/unparseable."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _norm_currency(value: Any) -> str:
    """Normalized currency code (uppercased); "" when absent."""
    if isinstance(value, str) and value.strip():
        return value.strip().upper()
    return ""


def _norm_location(value: Any) -> str:
    """Normalized location (stripped, lowercased); "" when absent."""
    if isinstance(value, str) and value.strip():
        return value.strip().lower()
    return ""


def _norm_facilities(value: Any) -> Optional[list[str]]:
    """Normalized facility list; None when the attribute is absent.

    A single string counts as one facility (lenient: filters are never
    schema-validated). Non-list, non-string values count as absent.
    """
    if value is None:
        return None
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return None
    return [v.strip().lower() for v in value if isinstance(v, str) and v.strip()]


@dataclass(frozen=True)
class CommerceConstraints:
    """Requested commerce constraints; absent fields were not requested."""

    max_price: Optional[float] = None
    price_currency: str = ""
    location: str = ""
    facilities: tuple[str, ...] = ()

    @property
    def wants_budget(self) -> bool:
        return self.max_price is not None

    @property
    def wants_location(self) -> bool:
        return bool(self.location)

    @property
    def wants_facilities(self) -> bool:
        return bool(self.facilities)


def constraints_from_filters(filters: Any) -> CommerceConstraints:
    """Leniently read commerce constraints; malformed entries are ignored."""
    if not isinstance(filters, dict):
        return CommerceConstraints()
    max_price: Optional[float] = None
    price_currency = ""
    raw_budget = filters.get("max_price")
    if isinstance(raw_budget, dict):
        for key in ("value", "amount", "price"):
            parsed = _as_float(raw_budget.get(key))
            if parsed is not None:
                max_price = parsed
                break
        price_currency = _norm_currency(raw_budget.get("currency"))
    else:
        max_price = _as_float(raw_budget)
    if max_price is not None and not price_currency:
        price_currency = _norm_currency(filters.get("currency"))
    location = _norm_location(filters.get("location"))
    facilities = _norm_facilities(filters.get("facilities")) or ()
    return CommerceConstraints(
        max_price=max_price,
        price_currency=price_currency,
        location=location,
        facilities=tuple(facilities),
    )


def keyword_fallback(query_terms: list[str], title: str, snippet: str) -> float:
    """Query-term overlap fraction; neutral 0.5 when the query is empty."""
    if not query_terms:
        return NEUTRAL
    haystack = set(tokenize(f"{title}\n{snippet}"))
    matched = sum(1 for term in query_terms if term in haystack)
    return matched / len(query_terms)


def _fallback_part(
    attribute: str,
    result: SearchResult,
    query_terms: list[str],
) -> tuple[float, str]:
    """Keyword-overlap contribution when an attribute is missing."""
    haystack = set(tokenize(f"{result.title}\n{result.snippet}"))
    hits = sum(1 for term in query_terms if term in haystack)
    score = keyword_fallback(query_terms, result.title, result.snippet)
    return score, (
        f"no {attribute} metadata; keyword fallback matched "
        f"{hits}/{len(query_terms)} terms"
    )


def _budget_part(
    cap: float,
    price_currency: str,
    result: SearchResult,
    query_terms: list[str],
) -> tuple[float, str]:
    """Score the budget constraint; never raises on odd metadata."""
    price = _as_float(result.metadata.get("price"))
    if price is None:
        return _fallback_part("price", result, query_terms)
    result_currency = _norm_currency(result.metadata.get("currency"))
    if price_currency:
        if not result_currency:
            return NEUTRAL, "no currency metadata; budget neutral 0.5"
        if result_currency != price_currency:
            return (
                NEUTRAL,
                f"currency mismatch ({result_currency} vs "
                f"{price_currency}); budget neutral 0.5",
            )
    if price <= cap:
        return 1.0, f"within your budget (price {price:g} <= max {cap:g})"
    return 0.0, f"over your budget (price {price:g} > max {cap:g})"


def _location_part(
    wanted: str,
    result: SearchResult,
    query_terms: list[str],
) -> tuple[float, str]:
    """Score the location constraint; never raises on odd metadata."""
    if "location" not in result.metadata or result.metadata.get("location") is None:
        return _fallback_part("location", result, query_terms)
    actual = _norm_location(result.metadata.get("location"))
    if not actual:
        return _fallback_part("location", result, query_terms)
    if actual == wanted:
        return 1.0, f"matches location ('{wanted}')"
    return 0.0, f"location '{actual}' is not '{wanted}'"


def _facilities_part(
    wanted: tuple[str, ...],
    result: SearchResult,
    query_terms: list[str],
) -> tuple[float, str]:
    """Score the facilities constraint; never raises on odd metadata."""
    if "facilities" not in result.metadata or result.metadata.get("facilities") is None:
        return _fallback_part("facilities", result, query_terms)
    actual = _norm_facilities(result.metadata.get("facilities")) or []
    have = set(actual)
    matched_names = [name for name in wanted if name in have]
    missing = [name for name in wanted if name not in have]
    score = len(matched_names) / len(wanted)
    if not missing:
        return 1.0, f"matches facilities ({', '.join(matched_names)})"
    if matched_names:
        return (
            score,
            f"matches facilities {len(matched_names)}/{len(wanted)} "
            f"({', '.join(matched_names)}); missing {', '.join(missing)}",
        )
    return 0.0, f"missing facilities ({', '.join(missing)})"


def commerce_score(
    constraints: CommerceConstraints,
    result: SearchResult,
    query_terms: list[str],
) -> tuple[float, str]:
    """Pure commerce signal for one hit: (signal 0..1, single reason).

    Satisfied fraction of the requested constraints; neutral 0.5 when no
    constraints were requested.
    """
    parts: list[tuple[float, str]] = []
    if constraints.wants_budget and constraints.max_price is not None:
        parts.append(
            _budget_part(
                constraints.max_price,
                constraints.price_currency,
                result,
                query_terms,
            )
        )
    if constraints.wants_location:
        parts.append(_location_part(constraints.location, result, query_terms))
    if constraints.wants_facilities:
        parts.append(_facilities_part(constraints.facilities, result, query_terms))
    if not parts:
        return NEUTRAL, "no commerce constraints requested; commerce neutral 0.5"
    signal = sum(score for score, _ in parts) / len(parts)
    reasons = "; ".join(reason for _, reason in parts)
    return signal, f"commerce {signal:.2f}: {reasons}"


class CommerceReranker(BasePlugin):
    """Orders hits by budget/location/facilities constraint satisfaction."""

    @property
    def name(self) -> str:
        return "commerce-reranker"

    @property
    def plugin_type(self) -> PluginType:
        return PluginType.RANKER

    @property
    def stage(self) -> PipelineStage:
        return PipelineStage.EXPLANATION

    @property
    def mandatory(self) -> bool:
        return False

    def configure(self, config: dict[str, Any]) -> None:
        super().configure(config)

    def run(self, context: SearchContext) -> SearchContext:
        filters = context.request.filters
        constraints = constraints_from_filters(filters)
        query_terms = tokenize(context.request.query)
        for result in context.results:
            signal, reason = commerce_score(constraints, result, query_terms)
            set_signal(result, SIGNAL_NAME, signal)
            result.reasons.append(reason)
        return context

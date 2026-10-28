"""Offline query-assistance helpers: completion, spelling, related queries.

All helpers are pure functions over injectable in-memory data (a term
list, a vocabulary, a query-count map). They never touch the network,
the index, or global state, so an empty backing store degrades
gracefully to ``[]``. Every helper returns ``[]`` (or ``None`` for the
single-correction spelling helper) on empty or malformed input instead
of raising.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Optional

_DEFAULT_LIMIT = 10


def _clean_terms(terms: Any) -> list[str]:
    """Best-effort coercion of an injectable term list to usable strings."""
    if not isinstance(terms, Iterable) or isinstance(terms, (str, bytes, Mapping)):
        return []
    cleaned: list[str] = []
    try:
        iterator = iter(terms)
    except TypeError:
        return []
    for term in iterator:
        if isinstance(term, str) and term.strip():
            cleaned.append(term.strip())
    return cleaned


def _clean_query(query: Any) -> Optional[str]:
    if not isinstance(query, str):
        return None
    stripped = query.strip()
    return stripped if stripped else None


def complete(prefix: Any, terms: Any, *, limit: int = _DEFAULT_LIMIT) -> list[str]:
    """Prefix completion over an injectable term list.

    Case-insensitive prefix match; results are deduplicated (keeping the
    first-seen spelling) and sorted case-insensitively for determinism.
    """
    cleaned_prefix = _clean_query(prefix)
    if cleaned_prefix is None:
        return []
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        return []
    lowered = cleaned_prefix.casefold()
    seen: set[str] = set()
    matches: list[str] = []
    for term in _clean_terms(terms):
        key = term.casefold()
        if key in seen or not key.startswith(lowered):
            continue
        seen.add(key)
        matches.append(term)
    matches.sort(key=str.casefold)
    return matches[:limit]


def _edit_distance(left: str, right: str, ceiling: int) -> int:
    """Levenshtein distance with early exit above ``ceiling``."""
    if abs(len(left) - len(right)) > ceiling:
        return ceiling + 1
    previous = list(range(len(right) + 1))
    for i, left_char in enumerate(left, start=1):
        current = [i]
        row_min = i
        for j, right_char in enumerate(right, start=1):
            cost = 0 if left_char == right_char else 1
            value = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            current.append(value)
            row_min = min(row_min, value)
        if row_min > ceiling:
            return ceiling + 1
        previous = current
    return previous[-1]


def correct_spelling(
    query: Any, vocabulary: Any, *, max_distance: int = 2
) -> Optional[str]:
    """Best spelling correction for ``query`` from an injectable vocabulary.

    Returns the closest vocabulary entry within ``max_distance`` edits, or
    ``None`` when the query is empty/malformed, the vocabulary is empty,
    the query already matches exactly (case-insensitively), or nothing is
    close enough. Ties break alphabetically for determinism.
    """
    cleaned = _clean_query(query)
    if cleaned is None:
        return None
    if (
        not isinstance(max_distance, int)
        or isinstance(max_distance, bool)
        or max_distance < 1
    ):
        return None
    words = _clean_terms(vocabulary)
    if not words:
        return None
    folded = cleaned.casefold()
    unique = sorted({word.casefold(): word for word in words}.items())
    for key, _original in unique:
        if key == folded:
            return None
    best: Optional[str] = None
    best_distance = max_distance + 1
    for key, original in unique:
        distance = _edit_distance(folded, key, max_distance)
        if distance <= max_distance and (
            best is None
            or distance < best_distance
            or (distance == best_distance and key < best.casefold())
        ):
            best = original
            best_distance = distance
    return best


def related_queries(
    query: Any, counts: Any, *, limit: int = _DEFAULT_LIMIT
) -> list[str]:
    """Related queries sharing a token with ``query``, ranked by count.

    ``counts`` maps past query text to observed frequency. Candidates must
    share at least one case-insensitive token with the input and must not
    equal the input itself. Ordered by count (descending), then
    alphabetically for determinism.
    """
    cleaned = _clean_query(query)
    if cleaned is None:
        return []
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        return []
    if not isinstance(counts, Mapping):
        return []
    tokens = set(cleaned.casefold().split())
    candidates: list[tuple[int, str]] = []
    for raw_candidate, raw_count in counts.items():
        if not isinstance(raw_candidate, str) or not raw_candidate.strip():
            continue
        if not isinstance(raw_count, (int, float)) or isinstance(raw_count, bool):
            continue
        candidate = raw_candidate.strip()
        if candidate.casefold() == cleaned.casefold():
            continue
        if tokens.isdisjoint(set(candidate.casefold().split())):
            continue
        candidates.append((int(raw_count), candidate))
    candidates.sort(key=lambda item: (-item[0], item[1].casefold()))
    return [candidate for _, candidate in candidates[:limit]]

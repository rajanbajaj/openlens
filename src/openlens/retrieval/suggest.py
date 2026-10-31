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

#: Small synonym map for query assistance (both directions apply).
_SYNONYM_PAIRS = (
    ("js", "javascript"),
    ("k8s", "kubernetes"),
    ("e-mail", "email"),
)
_SYNONYMS: dict[str, set[str]] = {}
for _left, _right in _SYNONYM_PAIRS:
    _SYNONYMS.setdefault(_left, set()).add(_right)
    _SYNONYMS.setdefault(_right, set()).add(_left)


def _synonym_variants(token: str) -> set[str]:
    """Counterparts of ``token`` in the small synonym map (possibly empty)."""
    if not isinstance(token, str):
        return set()
    return set(_SYNONYMS.get(token.casefold(), set()))


def _prefix_variants(cleaned: str) -> list[str]:
    """Full-query prefix forms: the query plus last-token synonym swaps."""
    tokens = cleaned.split()
    variants = [cleaned]
    if tokens:
        for synonym in sorted(_synonym_variants(tokens[-1])):
            candidate = " ".join([*tokens[:-1], synonym])
            if candidate != cleaned:
                variants.append(candidate)
    return variants


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
    Additive upgrades: last-token completion (for multi-word input, a
    candidate also matches when any of its words starts with the last
    query token) and the small synonym map (``js`` also completes
    ``javascript`` terms, and so on).
    """
    cleaned_prefix = _clean_query(prefix)
    if cleaned_prefix is None:
        return []
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        return []
    cleaned_terms = _clean_terms(terms)
    seen: set[str] = set()
    matches: list[str] = []
    for variant in _prefix_variants(cleaned_prefix):
        lowered = variant.casefold()
        for term in cleaned_terms:
            key = term.casefold()
            if key in seen or not key.startswith(lowered):
                continue
            seen.add(key)
            matches.append(term)
    tokens = cleaned_prefix.split()
    if len(tokens) > 1:
        last_variants = {tokens[-1].casefold()} | _synonym_variants(tokens[-1])
        for term in cleaned_terms:
            key = term.casefold()
            if key in seen:
                continue
            words = key.split()
            if any(word.startswith(last) for word in words for last in last_variants):
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


def _closest(
    folded: str, unique: list[tuple[str, str]], max_distance: int
) -> Optional[str]:
    """Closest vocabulary entry to ``folded`` within ``max_distance``."""
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


def correct_spelling(
    query: Any, vocabulary: Any, *, max_distance: int = 2
) -> Optional[str]:
    """Best spelling correction for ``query`` from an injectable vocabulary.

    Returns the closest vocabulary entry within ``max_distance`` edits, or
    ``None`` when the query is empty/malformed, the vocabulary is empty,
    the query already matches exactly (case-insensitively), or nothing is
    close enough. Ties break alphabetically for determinism. Additive
    upgrade: when the whole query has no close match, each whitespace
    token is corrected individually and the joined result is returned if
    any token changed.
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
    best = _closest(folded, unique, max_distance)
    if best is not None:
        return best
    tokens = cleaned.split()
    if len(tokens) < 2:
        return None
    corrected: list[str] = []
    changed = False
    for token in tokens:
        token_folded = token.casefold()
        if any(key == token_folded for key, _ in unique):
            corrected.append(token)
            continue
        # Scale the tolerance to the token length: a distance-2 rewrite
        # of a 2-4 character token (me->aes, news->aes, give->bike) is
        # almost never a real correction, while longer tokens tolerate 2.
        token_ceiling = min(max_distance, max(1, len(token_folded) // 3))
        fixed = _closest(token_folded, unique, token_ceiling)
        if fixed is None:
            corrected.append(token)
        else:
            corrected.append(fixed)
            changed = True
    return " ".join(corrected) if changed else None


def _related_tokens(text: str) -> set[str]:
    """Overlap tokens: normalized tokens plus hyphen compounds plus synonyms.

    Normalization (never raising: falls back to casefold-split) makes
    ``E-Mail`` overlap ``email``; hyphen compounds keep ``e-mail``
    overlapping ``email setup``; the synonym map links ``js`` with
    ``javascript`` and ``k8s`` with ``kubernetes``.
    """
    from openlens.retrieval.query_normalize import normalize_query
    from openlens.retrieval.query_parser import hyphen_compounds

    try:
        normalized = normalize_query(text).normalized
    except Exception:
        normalized = ""
    tokens = set(normalized.split()) if normalized else set(text.casefold().split())
    tokens |= set(hyphen_compounds(text))
    expanded = set(tokens)
    for token in tokens:
        expanded |= _synonym_variants(token)
    return expanded


def _count_weights(counts: Any) -> dict[str, int]:
    """Best-effort coercion of a count map to casefolded weights."""
    if not isinstance(counts, Mapping):
        return {}
    weights: dict[str, int] = {}
    for raw_candidate, raw_count in counts.items():
        if not isinstance(raw_candidate, str) or not raw_candidate.strip():
            continue
        if not isinstance(raw_count, (int, float)) or isinstance(raw_count, bool):
            continue
        key = raw_candidate.strip().casefold()
        if key not in weights:
            weights[key] = int(raw_count)
    return weights


def related_queries(
    query: Any,
    counts: Any = None,
    *,
    vocabulary: Any = None,
    limit: int = _DEFAULT_LIMIT,
) -> list[str]:
    """Related queries sharing a token with ``query``, ranked by count.

    When ``vocabulary`` (live index terms) is supplied, candidates come
    from vocabulary overlap with the input (normalized-token overlap:
    case and punctuation insensitive, hyphen- and synonym-aware,
    excluding the input itself); ``counts`` then only orders them
    (missing entries weigh 0), so live vocabulary is preferred and
    static counts are just a fallback ordering. Without a vocabulary
    the legacy path applies: candidates come from the ``counts`` map
    itself (past query text to observed frequency), ordered by count
    (descending), then alphabetically for determinism.
    """
    cleaned = _clean_query(query)
    if cleaned is None:
        return []
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        return []
    tokens = _related_tokens(cleaned)
    folded = cleaned.casefold()
    vocab_terms = _clean_terms(vocabulary) if vocabulary is not None else []
    if vocab_terms:
        weights = _count_weights(counts)
        seen: set[str] = set()
        candidates: list[tuple[int, str]] = []
        for term in vocab_terms:
            key = term.casefold()
            if key in seen or key == folded:
                continue
            seen.add(key)
            if tokens.isdisjoint(_related_tokens(term)):
                continue
            candidates.append((weights.get(key, 0), term))
        candidates.sort(key=lambda item: (-item[0], item[1].casefold()))
        return [candidate for _, candidate in candidates[:limit]]
    if not isinstance(counts, Mapping):
        return []
    candidates = []
    for raw_candidate, raw_count in counts.items():
        if not isinstance(raw_candidate, str) or not raw_candidate.strip():
            continue
        if not isinstance(raw_count, (int, float)) or isinstance(raw_count, bool):
            continue
        candidate = raw_candidate.strip()
        if candidate.casefold() == folded:
            continue
        if tokens.isdisjoint(_related_tokens(candidate)):
            continue
        candidates.append((int(raw_count), candidate))
    candidates.sort(key=lambda item: (-item[0], item[1].casefold()))
    return [candidate for _, candidate in candidates[:limit]]

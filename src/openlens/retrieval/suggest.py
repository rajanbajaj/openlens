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


#: Minimum prefix length for typo-tolerant fill. A one-edit slip on a
#: 1-3 character prefix matches nearly everything and reads as noise,
#: not help; longer prefixes make the intent unambiguous.
_TYPO_MIN_PREFIX_LEN = 4


def _weight_of(weights: Any, key: str) -> int:
    """Best-effort popularity weight for a casefolded term (0 when absent)."""
    if not isinstance(weights, Mapping):
        return 0
    try:
        value = weights.get(key, 0)
    except Exception:
        return 0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


def _clean_boost(boost: Any) -> list[str]:
    """Best-effort coercion of own-history queries to usable strings."""
    return _clean_terms(boost)


def complete(
    prefix: Any,
    terms: Any,
    *,
    limit: int = _DEFAULT_LIMIT,
    weights: Any = None,
    boost: Any = (),
) -> list[str]:
    """Prefix completion over an injectable term list.

    Case-insensitive prefix match; results are deduplicated (keeping the
    first-seen spelling). Ranking mirrors the search-engine convention
    (Google ranks by query popularity; offline we keep no query logs by
    design, so per-term index document frequency passed via ``weights``
    is the popularity proxy, replacing completion length as the primary
    order): full-phrase matches first, then synonym-variant phrases,
    then last-token matches (for multi-word input, a candidate also
    matches when any of its words starts with the last query token,
    e.g. ``js`` also completes ``javascript`` terms), each tier ordered
    by weight (descending), then minimal extension (shortest first),
    with an alphabetical tiebreak for determinism. ``boost`` carries
    the caller's own recent queries (request-scoped history, never
    logged); entries matching the prefix rank ahead of everything else
    (at most two, mirroring Google surfacing your own searches first).
    When exact matches leave slots unfilled and the prefix is long
    enough to disambiguate, one-edit prefix slips (``wireles`` for
    ``wireless``) fill the rest, ranked after every exact match.
    """
    cleaned_prefix = _clean_query(prefix)
    if cleaned_prefix is None:
        return []
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        return []
    cleaned_terms = _clean_terms(terms)
    folded = cleaned_prefix.casefold()
    seen: set[str] = set()
    # Own-history first (most recent first; history arrives oldest-first),
    # at most two, mirroring Google surfacing your own searches first.
    # Reserved up front so index matches never crowd them out.
    boosted: list[str] = []
    for candidate in reversed(_clean_boost(boost)):
        key = candidate.casefold()
        if key in seen:
            continue
        if key.startswith(folded) or folded.startswith(key):
            seen.add(key)
            boosted.append(candidate)
            if len(boosted) == 2:
                break
    tiered: list[tuple[int, int, str]] = []
    for variant in _prefix_variants(cleaned_prefix):
        lowered = variant.casefold()
        for term in cleaned_terms:
            key = term.casefold()
            if key in seen or not key.startswith(lowered):
                continue
            seen.add(key)
            tier = 0 if lowered == folded else 1
            tiered.append((tier, -_weight_of(weights, key), term))
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
                tiered.append((2, -_weight_of(weights, key), term))
    tiered.sort(key=lambda item: (item[0], item[1], len(item[2]), item[2].casefold()))
    matches = boosted + [term for _, _, term in tiered]
    if len(matches) < limit and len(folded) >= _TYPO_MIN_PREFIX_LEN:
        typo: list[str] = []
        for term in cleaned_terms:
            key = term.casefold()
            if key in seen or len(key) < len(folded):
                continue
            if _edit_distance(folded, key[: len(folded)], 1) <= 1:
                seen.add(key)
                typo.append(term)
        typo.sort(key=str.casefold)
        matches.extend(typo)
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
    folded: str,
    unique: list[tuple[str, str]],
    max_distance: int,
    weights: Any = None,
) -> Optional[str]:
    """Closest vocabulary entry to ``folded`` within ``max_distance``.

    Ties on edit distance break toward the higher popularity weight
    (index document frequency), then alphabetically for determinism.
    """
    best: Optional[str] = None
    best_distance = max_distance + 1
    best_weight = -1
    for key, original in unique:
        distance = _edit_distance(folded, key, max_distance)
        if distance > max_distance:
            continue
        weight = _weight_of(weights, key)
        if best is not None and distance >= best_distance:
            if distance != best_distance:
                continue
            if weight < best_weight:
                continue
            if weight == best_weight and key >= best.casefold():
                continue
        best = original
        best_distance = distance
        best_weight = weight
    return best


def correct_spelling(
    query: Any, vocabulary: Any, *, max_distance: int = 2, weights: Any = None
) -> Optional[str]:
    """Best spelling correction for ``query`` from an injectable vocabulary.

    Returns the closest vocabulary entry within ``max_distance`` edits, or
    ``None`` when the query is empty/malformed, the vocabulary is empty,
    the query already matches exactly (case-insensitively), or nothing is
    close enough. Ties break toward the higher ``weights`` popularity
    entry, then alphabetically for determinism. Additive upgrade: when
    the whole query has no close match, each whitespace token is
    corrected individually and the joined result is returned if any token
    changed.
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
    best = _closest(folded, unique, max_distance, weights)
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
        fixed = _closest(token_folded, unique, token_ceiling, weights)
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
    weights: Any = None,
) -> list[str]:
    """Related queries sharing a token with ``query``, ranked by count.

    When ``vocabulary`` (live index terms) is supplied, candidates come
    from vocabulary overlap with the input (normalized-token overlap:
    case and punctuation insensitive, hyphen- and synonym-aware,
    excluding the input itself); ``weights`` (live index document
    frequency) then orders them: index support outranks everything
    (document counts and static past-query counts live on different
    scales, so they are never compared directly); ``counts`` only orders
    terms when no live weights are supplied. Without a vocabulary the
    legacy path applies: candidates come from the ``counts`` map itself
    (past query text to observed frequency), ordered by count
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
        index_weights = weights if isinstance(weights, Mapping) and weights else {}
        fallback = {} if index_weights else _count_weights(counts)
        seen: set[str] = set()
        candidates: list[tuple[int, str]] = []
        for term in vocab_terms:
            key = term.casefold()
            if key in seen or key == folded:
                continue
            seen.add(key)
            if tokens.isdisjoint(_related_tokens(term)):
                continue
            if index_weights:
                weight = _weight_of(index_weights, key)
            else:
                weight = fallback.get(key, 0)
            candidates.append((weight, term))
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

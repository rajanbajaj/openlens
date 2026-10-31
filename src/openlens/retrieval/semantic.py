"""Semantic re-rank signal for hybrid retrieval (opt-in, fail open).

Uses :meth:`ModelManager.embed` to score already-retrieved candidates by
cosine similarity between the query vector and each candidate vector, and
returns the candidate ids in semantic-best-first order. The caller
(``providers/custom_index.py``) fuses this order with the FTS5 order via
the read-only ``retrieval/fusion.py`` RRF helper.

Fail-open contract: ``models`` being ``None`` (no embedding backend
enabled), empty inputs, shape mismatches, and ANY exception from the
embedding backend all yield ``[]`` — never a raise. The raw query text is
never logged above DEBUG.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from typing import Any

from openlens.core.interfaces import SearchResult
from openlens.retrieval.query_normalize import normalize_query

_LOGGER = logging.getLogger(__name__)


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Cosine similarity in [-1, 1]; 0.0 when either vector has zero norm."""
    if len(left) != len(right) or not left:
        return 0.0
    dot = 0.0
    left_norm_sq = 0.0
    right_norm_sq = 0.0
    for left_value, right_value in zip(left, right):
        dot += left_value * right_value
        left_norm_sq += left_value * left_value
        right_norm_sq += right_value * right_value
    denominator = math.sqrt(left_norm_sq * right_norm_sq)
    if denominator == 0.0:
        return 0.0
    return dot / denominator


def _candidate_text(result: SearchResult) -> str:
    """Text embedded for one candidate (title carries, snippet supports)."""
    title = result.title or ""
    snippet = result.snippet or ""
    combined = f"{title} {snippet}".strip()
    return combined if combined else result.id


def rank_ids_by_semantic(
    query: str,
    candidates: Sequence[SearchResult],
    models: Any | None,
) -> list[str]:
    """Order candidate ids by embedding cosine similarity to ``query``.

    Args:
        query: Raw query text, normalized with the shared query normalizer
            before embedding (never logged above DEBUG).
        candidates: Already-retrieved FTS5 hits to re-order semantically.
        models: A :class:`ModelManager` (or any object with
            ``embed(texts)``); ``None`` means no embedding backend is
            enabled and yields ``[]``.

    Returns:
        Candidate ids sorted by cosine similarity (descending), ties
        broken by id ascending for determinism. ``[]`` whenever the
        semantic signal is unavailable or unusable (fail open).
    """
    try:
        if models is None:
            return []
        if not isinstance(query, str) or not query.strip():
            return []
        candidate_list = list(candidates)
        if not candidate_list:
            return []
        try:
            embed_query = normalize_query(query).normalized or query
        except Exception:
            _LOGGER.debug("semantic ranking: query normalization failed")
            embed_query = query
        texts = [embed_query, *(_candidate_text(item) for item in candidate_list)]
        vectors = models.embed(texts)
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            _LOGGER.debug("semantic ranking unavailable: bad embedding shape")
            return []
        query_vector = vectors[0]
        if not isinstance(query_vector, list) or not query_vector:
            _LOGGER.debug("semantic ranking unavailable: bad query vector")
            return []
        scored: list[tuple[float, str]] = []
        for item, vector in zip(candidate_list, vectors[1:]):
            if not isinstance(vector, list) or len(vector) != len(query_vector):
                continue
            try:
                score = cosine_similarity(
                    [float(value) for value in query_vector],
                    [float(value) for value in vector],
                )
            except (TypeError, ValueError):
                continue
            scored.append((score, item.id))
        scored.sort(key=lambda entry: (-entry[0], entry[1]))
        return [item_id for _, item_id in scored]
    except Exception:
        _LOGGER.debug("semantic ranking unavailable: embedding backend failed")
        return []

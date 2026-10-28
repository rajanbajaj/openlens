"""Reciprocal rank fusion over per-provider ranks.

Combines ranked hit lists from independent providers into one merged list
using reciprocal rank fusion (RRF):

    fused(doc) = sum_over_providers(weight[p] / (k + rank_p(doc)))

Only per-provider RANKS (1-based position in each provider's list) enter
the score; raw cross-provider ``original_score`` values are never compared
or combined, because provider scores are uncalibrated.

Duplicate handling: hits are keyed by
:func:`openlens.retrieval.deduplicator.dedupe_key` (URL-normalized), so the
same URL returned by two providers merges into one hit. The kept object is
the contributor with the best (lowest) rank; the other provider names are
recorded in ``metadata["also_from"]`` (sorted). The merged hit carries
``metadata["rrf_score"]`` and ``final_score`` initialized to the fused
score; ``original_score`` is left untouched.

Ordering is deterministic: fused score descending, then provider name
(the owning key of ``ranked_lists`` for the kept contributor) ascending,
then result id ascending.

Paging note: callers query every provider at ``offset=0`` with a fetch
depth of ``offset + limit``, fuse the full candidate window, cap the
merged list at M before downstream scoring, and slice
``[offset:offset + limit]`` once after ranking. The ``offset`` is never
passed to providers AND applied again after fusion (that would double-skip).
"""

from __future__ import annotations

from collections.abc import Mapping

from openlens.core.interfaces import SearchResult
from openlens.retrieval.deduplicator import dedupe_key


def fuse(
    ranked_lists: Mapping[str, list[SearchResult]],
    k: int = 60,
    weights: Mapping[str, float] | None = None,
) -> list[SearchResult]:
    """Merge per-provider ranked lists with reciprocal rank fusion.

    Args:
        ranked_lists: Mapping of provider name to that provider's hits in
            rank order (position 1 = best). Empty mappings and empty lists
            are handled and yield no candidates for that provider.
        k: RRF rank-offset constant (higher k compresses rank differences).
        weights: Optional per-provider score multipliers; providers absent
            from the mapping use 1.0.

    Returns:
        New merged :class:`SearchResult` list (inputs are not mutated),
        sorted by fused score desc, provider name asc, result id asc.
    """
    resolved_weights = dict(weights) if weights else {}
    fused_scores: dict[str, float] = {}
    best_rank: dict[str, int] = {}
    kept: dict[str, SearchResult] = {}
    kept_provider: dict[str, str] = {}
    contributors: dict[str, set[str]] = {}

    for provider_name, results in ranked_lists.items():
        weight = resolved_weights.get(provider_name, 1.0)
        seen_in_list: set[str] = set()
        for rank, result in enumerate(results, start=1):
            key = dedupe_key(result)
            if key in seen_in_list:
                continue
            seen_in_list.add(key)
            fused_scores[key] = fused_scores.get(key, 0.0) + weight / (k + rank)
            contributors.setdefault(key, set()).add(provider_name)
            if key not in kept or rank < best_rank[key]:
                kept[key] = result
                kept_provider[key] = provider_name
                best_rank[key] = rank

    ordered_keys = sorted(
        fused_scores,
        key=lambda key: (-fused_scores[key], kept_provider[key], kept[key].id),
    )

    merged: list[SearchResult] = []
    for key in ordered_keys:
        source = kept[key]
        others = sorted(contributors[key] - {kept_provider[key]})
        merged.append(
            SearchResult(
                id=source.id,
                title=source.title,
                snippet=source.snippet,
                original_score=source.original_score,
                final_score=fused_scores[key],
                url=source.url,
                provider=source.provider,
                reasons=list(source.reasons),
                warnings=list(source.warnings),
                metadata={
                    **source.metadata,
                    "rrf_score": fused_scores[key],
                    "also_from": others,
                },
            )
        )
    return merged

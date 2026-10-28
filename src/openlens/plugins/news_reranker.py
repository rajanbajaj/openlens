"""News story grouping + perspective rerank (EXPLANATION stage, optional).

Reads the attribute enricher's documented keys (``published``,
``perspective``) plus the scoring-pack ``trust`` signal with a
``metadata["trusted"]`` bool fallback, and emits a 0..1 ``perspective``
ordering signal consumed by the ``Ranker`` composite. Never re-sorts:
ordering stays with the engine-owned rank.

Grouping rule (deterministic): over the top-K candidates (K=50,
configurable via ``top_k``), tokenize titles with :func:`tokenize_title`,
build word bigrams, index bigram -> candidates, and union-find merge pairs
that share >= 1 bigram with title-token Jaccard >= 0.5 and the same
normalized ``published`` calendar day (or both undated). Blocks over 25
candidates are left unmerged so a shared bigram can never rebuild an
O(K^2) block. Hits beyond K get no ``story_id``.

Perspective mix is credibility-weighted, never equal-weight: the most
credible member of each perspective per group gets its credibility (the
``trust`` signal when present, else 1.0/0.5 from the ``trusted`` bool,
else neutral 0.5); other same-perspective members get half of it;
ungrouped hits get neutral 0.5. Missing perspective normalizes to
``"unknown"``.

Grouping output lives in ``context.metadata["story_groups"]``
(``group id -> {"members": [result ids], "perspectives": [...]}``) plus
per-result ``metadata["story_id"]``. Both are internal pipeline state:
``SearchContext.diagnostics()`` never includes ``context.metadata``, so
they are asserted on ``SearchContext`` in unit tests, not via the API.

Pure helpers (:func:`tokenize_title`, :func:`title_bigrams`,
:func:`token_jaccard`, :func:`group_stories`, :func:`credibility_of`,
:func:`normalize_perspective`, :func:`perspective_signals`) are
stage-agnostic so synthesis can fold them into ``Ranker`` unchanged.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from openlens.core.context import SearchContext
from openlens.core.interfaces import (
    ConfigError,
    PipelineStage,
    PluginType,
)
from openlens.plugins._signals import (
    NEUTRAL_SIGNAL,
    clamp_signal,
    result_date,
    set_signal,
)
from openlens.plugins.base import BasePlugin

#: Signal key written into ``result.metadata["signals"]``.
SIGNAL_NAME = "perspective"

#: Default story-grouping candidate window.
DEFAULT_TOP_K = 50

#: Bigram blocks larger than this are left unmerged (scalability bound).
MAX_BLOCK_SIZE = 25

#: Minimum title-token Jaccard similarity for a story merge.
MIN_JACCARD = 0.5

#: Normalized label for hits without a perspective.
UNKNOWN_PERSPECTIVE = "unknown"

#: Credibility granted by the ``trusted``-bool fallback path.
TRUSTED_TRUE_CREDIBILITY = 1.0
TRUSTED_FALSE_CREDIBILITY = 0.5

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokenize_title(title: str) -> list[str]:
    """Split a title into lowercase alphanumeric tokens (shared tokenizer)."""
    if not isinstance(title, str):
        return []
    return _TOKEN_RE.findall(title.lower())


def title_bigrams(tokens: list[str]) -> set[tuple[str, str]]:
    """Return the adjacent word bigrams of an ordered token list."""
    return {(tokens[i], tokens[i + 1]) for i in range(len(tokens) - 1)}


def token_jaccard(first: set[str], second: set[str]) -> float:
    """Title-token Jaccard similarity in 0..1."""
    if not first and not second:
        return 1.0
    if not first or not second:
        return 0.0
    return len(first & second) / len(first | second)


def group_stories(
    token_lists: list[list[str]],
    days: list[date | None],
    *,
    top_k: int = DEFAULT_TOP_K,
    max_block_size: int = MAX_BLOCK_SIZE,
) -> list[list[int]]:
    """Group candidate positions into stories; pure and stage-agnostic.

    Only the first ``top_k`` candidates are considered. Pairs sharing at
    least one word bigram merge when their title-token Jaccard is >= 0.5
    and their normalized days match (``None`` matches ``None``).
    Bigram blocks over ``max_block_size`` are skipped. Returns member
    position lists (each >= 2 members), sorted by first member.
    """
    count = min(len(token_lists), len(days), top_k)
    token_sets = [set(token_lists[i]) for i in range(count)]
    index: dict[tuple[str, str], list[int]] = {}
    for pos in range(count):
        for bigram in sorted(title_bigrams(token_lists[pos])):
            index.setdefault(bigram, []).append(pos)
    parent = list(range(count))

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for block in index.values():
        if len(block) > max_block_size:
            continue
        for left in range(len(block)):
            for right in range(left + 1, len(block)):
                first, second = block[left], block[right]
                if find(first) == find(second):
                    continue
                if days[first] != days[second]:
                    continue
                if token_jaccard(token_sets[first], token_sets[second]) < MIN_JACCARD:
                    continue
                first_root, second_root = find(first), find(second)
                if first_root != second_root:
                    parent[max(first_root, second_root)] = min(first_root, second_root)
    components: dict[int, list[int]] = {}
    for pos in range(count):
        components.setdefault(find(pos), []).append(pos)
    groups = [sorted(members) for members in components.values() if len(members) > 1]
    groups.sort(key=lambda members: members[0])
    return groups


def credibility_of(metadata: dict[str, Any]) -> float:
    """Return a hit's credibility in 0..1 (trust signal, else fallback).

    Uses the scoring-pack ``trust`` signal when present; otherwise the
    ``metadata["trusted"]`` bool (True -> 1.0, False -> 0.5); otherwise
    neutral 0.5. Never equal-weights: distinct evidence yields distinct
    values.
    """
    signals = metadata.get("signals")
    if isinstance(signals, dict) and "trust" in signals:
        return clamp_signal(signals["trust"])
    trusted = metadata.get("trusted")
    if trusted is True:
        return TRUSTED_TRUE_CREDIBILITY
    if trusted is False:
        return TRUSTED_FALSE_CREDIBILITY
    return NEUTRAL_SIGNAL


def normalize_perspective(value: Any) -> str:
    """Normalize a perspective label; missing/blank/non-string -> "unknown"."""
    if isinstance(value, str):
        cleaned = value.strip().lower()
        if cleaned:
            return cleaned
    return UNKNOWN_PERSPECTIVE


def perspective_signals(
    count: int,
    groups: list[list[int]],
    perspectives: list[str],
    credibilities: list[float],
) -> list[float]:
    """Assign the 0..1 perspective ordering signal; pure and stage-agnostic.

    Within each group, the most credible member of each perspective (first
    wins ties) gets its credibility; other same-perspective members get
    half of it. Ungrouped positions stay neutral 0.5.
    """
    signals = [NEUTRAL_SIGNAL] * count
    for group in groups:
        by_view: dict[str, list[int]] = {}
        for pos in group:
            by_view.setdefault(perspectives[pos], []).append(pos)
        for members in by_view.values():
            ordered = sorted(members, key=lambda pos: (-credibilities[pos], pos))
            top = credibilities[ordered[0]]
            signals[ordered[0]] = clamp_signal(top)
            for other in ordered[1:]:
                signals[other] = clamp_signal(top / 2.0)
    return signals


class NewsReranker(BasePlugin):
    """Groups top-K news hits into stories; emits the perspective signal."""

    @property
    def name(self) -> str:
        return "news-reranker"

    @property
    def plugin_type(self) -> PluginType:
        return PluginType.SCORER

    @property
    def stage(self) -> PipelineStage:
        return PipelineStage.EXPLANATION

    @property
    def mandatory(self) -> bool:
        return False

    def configure(self, config: dict[str, Any]) -> None:
        super().configure(config)
        top_k = self._config.get("top_k", DEFAULT_TOP_K)
        if isinstance(top_k, bool) or not isinstance(top_k, int):
            raise ConfigError("NewsReranker 'top_k' must be an int.")
        if top_k <= 0:
            raise ConfigError("NewsReranker 'top_k' must be > 0.")
        self._top_k = top_k

    def run(self, context: SearchContext) -> SearchContext:
        results = context.results
        count = min(len(results), self._top_k)
        token_lists = [tokenize_title(result.title) for result in results[:count]]
        days: list[date | None] = []
        for result in results[:count]:
            moment = result_date(result)
            days.append(moment.date() if moment is not None else None)
        perspectives = [
            normalize_perspective(result.metadata.get("perspective"))
            for result in results[:count]
        ]
        credibilities = [credibility_of(result.metadata) for result in results[:count]]
        groups = group_stories(token_lists, days, top_k=self._top_k)
        signals = perspective_signals(count, groups, perspectives, credibilities)
        member_story: dict[int, str] = {}
        story_groups: dict[str, dict[str, Any]] = {}
        for number, group in enumerate(groups):
            story_id = f"story-{number}"
            for pos in group:
                member_story[pos] = story_id
            story_groups[story_id] = {
                "members": [results[pos].id for pos in group],
                "perspectives": sorted({perspectives[pos] for pos in group}),
            }
        context.metadata["story_groups"] = story_groups
        for pos, result in enumerate(results):
            if pos < count:
                value = set_signal(result, SIGNAL_NAME, signals[pos])
                assigned = member_story.get(pos)
                if assigned is not None:
                    result.metadata["story_id"] = assigned
                    result.reasons.append(
                        f"perspective {value:.2f} in {assigned} "
                        f"('{perspectives[pos]}' view)"
                    )
                else:
                    result.metadata.pop("story_id", None)
                    result.reasons.append(
                        f"no story group; perspective neutral {value:.2f}"
                    )
            else:
                set_signal(result, SIGNAL_NAME, NEUTRAL_SIGNAL)
                result.metadata.pop("story_id", None)
                result.reasons.append("beyond top-K story window; no story group")
        return context

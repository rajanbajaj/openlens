"""Story clustering: group same-story coverage across outlets.

Pure function over ranked search hits (no I/O, no model): two hits join
a story when their headlines are lexically close (Jaccard over content
tokens) inside a publication window. A subject-slot veto keeps
template headlines about different subjects apart
("Apple announces new phone" vs "Samsung announces new phone" share
most words but lead with different unique subjects). Precision over
recall: a missed cluster just renders as plain results; a wrong merge
would mix stances from different stories.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

#: Content tokens below this Jaccard overlap never form a story.
MIN_JACCARD = 0.25

#: Minimum shared content tokens for a story (kills single-word hits).
MIN_SHARED_TOKENS = 2

#: Max calendar gap between members of one story.
TIME_WINDOW_DAYS = 2

#: Cap: clustering is a top-N refinement, not a corpus operation.
MAX_CLUSTER_HITS = 30

_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "to",
        "of",
        "in",
        "on",
        "for",
        "and",
        "or",
        "by",
        "as",
        "at",
        "from",
        "with",
        "amid",
        "after",
        "over",
        "says",
        "say",
    }
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _content_tokens(title: str) -> list[str]:
    """Lower-cased content tokens in title order (stopwords dropped)."""
    return [
        token
        for token in _TOKEN_RE.findall(title.lower())
        if token not in _STOPWORDS and not token.isdigit()
    ]


def _published_on(metadata: dict[str, Any]) -> date | None:
    raw = metadata.get("published")
    if not isinstance(raw, str):
        return None
    try:
        return date.fromisoformat(raw.strip()[:10])
    except ValueError:
        return None


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _subject_veto(
    left: list[str],
    right: list[str],
    doc_freq: dict[str, int],
) -> bool:
    """True when the headlines lead with different unique subjects.

    Both leads must be set-unique (df == 1) and absent from the other
    headline; shared leads ("Apple sues X" vs "Apple unveils Y") or
    repeated leads fall through to the Jaccard verdict.
    """
    if not left or not right:
        return False
    first, second = left[0], right[0]
    if first == second:
        return False
    if doc_freq.get(first, 0) != 1 or doc_freq.get(second, 0) != 1:
        return False
    return first not in right and second not in left


@dataclass
class StoryCluster:
    """One story: ranked members, representative claim, stable id."""

    story_id: str
    representative_title: str
    claim: str
    member_ids: list[str] = field(default_factory=list)


def _story_id(member_ids: list[str]) -> str:
    digest = hashlib.sha256("|".join(sorted(member_ids)).encode("utf-8")).hexdigest()
    return digest[:12]


def cluster_stories(
    hits: list[Any], *, time_window_days: int = TIME_WINDOW_DAYS
) -> list[StoryCluster]:
    """Group ranked hits into multi-member story clusters.

    Only groups of two or more are returned; loners render as plain
    results. The input order is ranking order, so each cluster's
    representative is its highest-ranked member. ``published`` metadata
    (``YYYY-MM-DD``) gates the time window; hits without a parseable
    date never block a merge.
    """
    from openlens.retrieval.claim_extract import extract_claims

    pool = list(hits)[:MAX_CLUSTER_HITS]
    tokens = [(_content_tokens(getattr(hit, "title", "") or "")) for hit in pool]
    dates = [
        _published_on(
            hit.metadata if isinstance(getattr(hit, "metadata", None), dict) else {}
        )
        for hit in pool
    ]
    doc_freq: dict[str, int] = {}
    for toks in tokens:
        for token in set(toks):
            doc_freq[token] = doc_freq.get(token, 0) + 1

    assigned: list[bool] = [False] * len(pool)
    clusters: list[StoryCluster] = []
    for index, hit in enumerate(pool):
        if assigned[index]:
            continue
        group = [index]
        for other in range(index + 1, len(pool)):
            if assigned[other]:
                continue
            left, right = set(tokens[index]), set(tokens[other])
            if len(left & right) < MIN_SHARED_TOKENS:
                continue
            if _jaccard(left, right) < MIN_JACCARD:
                continue
            if _subject_veto(tokens[index], tokens[other], doc_freq):
                continue
            first, second = dates[index], dates[other]
            if first is not None and second is not None:
                if abs((first - second).days) > time_window_days:
                    continue
            group.append(other)
        if len(group) < 2:
            continue
        for member in group:
            assigned[member] = True
        member_hits = [pool[member] for member in group]
        rep = member_hits[0]
        rep_title = getattr(rep, "title", "") or ""
        rep_snippet = getattr(rep, "snippet", "") or ""
        claims = extract_claims(rep_title, rep_snippet)
        member_ids = [str(getattr(member, "id", "")) for member in member_hits]
        clusters.append(
            StoryCluster(
                story_id=_story_id(member_ids),
                representative_title=rep_title,
                claim=claims[0] if claims else rep_title,
                member_ids=member_ids,
            )
        )
    return clusters

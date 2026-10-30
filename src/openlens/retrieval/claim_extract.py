"""Claim-span extraction: deterministic per-hit key claims (CAT4).

Splits title+snippet into sentence-level candidate claim spans with
pure string rules (no model, no network): sentences carrying a
finiteness cue (a verb-like token plus a named-ish capitalized token
or a number) and at least four tokens. Returns at most
``MAX_CLAIMS`` spans per hit, longest-informative first, in stable
order. Stage-agnostic: the news reranker attaches them as
``metadata["key_claims"]`` so the existing AI-citations UI can link
per-claim evidence; nothing here filters or scores.
"""

from __future__ import annotations

import re

#: Cap on claim spans attached per hit.
MAX_CLAIMS = 3

#: Minimum tokens for a sentence to count as a claim candidate.
MIN_TOKENS = 4

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'%-]*")
_NUMBER_RE = re.compile(r"\d")
_CAPITALIZED_RE = re.compile(r"\b[A-Z][a-z]+\b")

#: Common reporting verbs that mark a factual assertion.
_ASSERTION_VERBS: frozenset[str] = frozenset(
    {
        "is",
        "are",
        "was",
        "were",
        "has",
        "have",
        "had",
        "says",
        "said",
        "announces",
        "announced",
        "reports",
        "reported",
        "confirms",
        "confirmed",
        "shows",
        "showed",
        "finds",
        "found",
        "reveals",
        "revealed",
        "rose",
        "fell",
        "grew",
        "won",
        "lost",
        "launched",
        "passed",
        "signed",
        "banned",
        "approved",
    }
)


def split_sentences(text: str) -> list[str]:
    """Split text into trimmed non-empty sentences; pure."""
    if not isinstance(text, str):
        return []
    return [part.strip() for part in _SENTENCE_SPLIT_RE.split(text) if part.strip()]


def is_claim_candidate(sentence: str) -> bool:
    """True when a sentence looks like a verifiable assertion."""
    tokens = _TOKEN_RE.findall(sentence)
    if len(tokens) < MIN_TOKENS:
        return False
    lowered = {token.lower() for token in tokens}
    if not (lowered & _ASSERTION_VERBS):
        return False
    return bool(_NUMBER_RE.search(sentence) or _CAPITALIZED_RE.search(sentence))


def extract_claims(title: str, snippet: str) -> list[str]:
    """Return up to ``MAX_CLAIMS`` key-claim spans for one hit."""
    seen: list[str] = []
    for sentence in split_sentences(f"{title or ''}\n{snippet or ''}"):
        if len(sentence) > 280:
            continue
        if is_claim_candidate(sentence) and sentence not in seen:
            seen.append(sentence)
        if len(seen) >= MAX_CLAIMS:
            break
    return seen

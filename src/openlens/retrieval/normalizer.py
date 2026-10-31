"""Result normalizer: (Document, raw score) -> SearchResult."""

from __future__ import annotations

import re

from openlens.core.interfaces import Document, SearchResult

_SNIPPET_LEN = 200

#: Sentence ends: punctuation followed by whitespace and a fresh lead
#: (capital, digit, quote, bracket). The lookahead keeps abbreviations
#: such as "e.g. foo" or "U.S. stocks" intact.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[\"'(\[A-Z0-9])")


def make_snippet(content: str, length: int = _SNIPPET_LEN) -> str:
    """Coherent description: whole sentences up to ``length``.

    Sentences accumulate until the cap, so the cut lands on a sentence
    boundary and descriptions read complete instead of trailing off
    mid-clause. A single over-long sentence (or punctuation-free text)
    falls back to a word-boundary cut marked with an ellipsis.
    """
    text = " ".join(content.split())
    if length <= 0:
        return ""
    if len(text) <= length:
        return text
    kept: list[str] = []
    used = 0
    for sentence in _SENTENCE_SPLIT.split(text):
        piece = sentence.strip()
        if not piece:
            continue
        addition = (0 if not kept else 1) + len(piece)
        if used + addition > length:
            break
        kept.append(piece)
        used += addition
    if kept:
        return " ".join(kept)
    head = text[:length].rstrip()
    if " " in head:
        head = head.rsplit(" ", 1)[0]
    return (head or text[:length]) + "..."


def normalize(
    doc: Document, raw_score: float, provider: str, query: str = ""
) -> SearchResult:
    reason = (
        f"matched '{query}' with provider score {raw_score:.3f}"
        if query
        else f"provider score {raw_score:.3f}"
    )
    return SearchResult(
        id=doc.id,
        title=doc.title,
        snippet=make_snippet(doc.content),
        original_score=raw_score,
        final_score=raw_score,
        url=doc.url,
        provider=provider,
        reasons=[reason],
        metadata=dict(doc.metadata),
    )


def normalize_many(
    scored: list[tuple[Document, float]], provider: str, query: str = ""
) -> list[SearchResult]:
    return [normalize(doc, score, provider, query) for doc, score in scored]

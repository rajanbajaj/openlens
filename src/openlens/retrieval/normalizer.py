"""Result normalizer: (Document, raw score) -> SearchResult."""

from __future__ import annotations

import re

from openlens.core.interfaces import Document, SearchResult

_SNIPPET_LEN = 200

#: A meta description shorter than this (or with fewer than
#: ``_MIN_DESCRIPTION_WORDS`` words) is boilerplate or a stub, never the
#: snippet: the keyword window or lead sentences read better.
_MIN_DESCRIPTION_LEN = 40
_MIN_DESCRIPTION_WORDS = 5

#: Sentence ends: punctuation followed by whitespace and a fresh lead
#: (capital, digit, quote, bracket). The lookahead keeps abbreviations
#: such as "e.g. foo" or "U.S. stocks" intact.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[\"'(\[A-Z0-9])")

#: Query terms for the keyword window: Unicode word runs.
_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _is_substantive_description(text: str, title: str = "") -> bool:
    """True when a meta description earns the snippet slot.

    Substantive means long enough to carry meaning (``>= 40`` chars,
    ``>= 5`` words) and not a mere echo of the page title. Short stubs
    (``"Home"``, ``"Products"``) and title duplicates fall through to
    the keyword window instead.
    """
    cleaned = " ".join(text.split())
    if len(cleaned) < _MIN_DESCRIPTION_LEN or len(cleaned.split()) < (
        _MIN_DESCRIPTION_WORDS
    ):
        return False
    if title and cleaned.casefold() == " ".join(title.split()).casefold():
        return False
    return True


def _lead_sentences(text: str, length: int) -> str:
    """Whole sentences up to ``length`` (word-boundary cut + ellipsis)."""
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


def _keyword_window(content: str, query: str, length: int) -> str | None:
    """Window of ``length`` around the first query-term hit, or None.

    The window opens about a third of ``length`` before the match so the
    term reads in context, cuts on word boundaries, and marks both
    cropped edges with an ellipsis. None when no term occurs verbatim
    (the caller falls back to lead sentences).
    """
    terms = [term for term in _WORD_RE.findall(query.casefold()) if term]
    if not terms:
        return None
    lowered = content.casefold()
    hits = [lowered.find(term) for term in terms]
    hits = [hit for hit in hits if hit >= 0]
    if not hits:
        return None
    anchor = min(hits)
    start = max(0, anchor - length // 3)
    end = min(len(content), start + length)
    window = content[start:end]
    if start > 0:
        gap = window.find(" ")
        if gap < 0:
            # No word boundary inside: a lone partial token reads worse
            # than lead sentences, so decline the window.
            return None
        window = "..." + window[gap + 1 :]
    if end < len(content):
        tail = window[3:] if window.startswith("...") else window
        trimmed = tail.rstrip()
        if " " in trimmed:
            trimmed = trimmed.rsplit(" ", 1)[0]
        window = ("..." if window.startswith("...") else "") + trimmed + "..."
    return window.strip() or None


def make_snippet(
    content: str,
    length: int = _SNIPPET_LEN,
    *,
    description: str | None = None,
    query: str = "",
) -> str:
    """Snippet priority: substantive description > query window > lead.

    A substantive meta ``description`` (length/word-count gate, not a
    title echo) wins verbatim in sentence form; otherwise the first
    ``query`` term hit centers a word-boundary window; otherwise the
    lead sentences carry the snippet as before. ``content``-only calls
    keep the legacy lead-sentences behavior exactly.
    """
    text = " ".join(content.split())
    if length <= 0:
        return ""
    if description is not None and isinstance(description, str):
        cleaned = " ".join(description.split())
        if cleaned and _is_substantive_description(cleaned):
            if len(cleaned) <= length:
                return cleaned
            return _lead_sentences(cleaned, length)
    if query and query.strip():
        window = _keyword_window(text, query, length)
        if window:
            return window
    if len(text) <= length:
        return text
    return _lead_sentences(text, length)


def normalize(
    doc: Document, raw_score: float, provider: str, query: str = ""
) -> SearchResult:
    reason = (
        f"matched '{query}' with provider score {raw_score:.3f}"
        if query
        else f"provider score {raw_score:.3f}"
    )
    metadata = doc.metadata or {}
    raw_description = metadata.get("description")
    return SearchResult(
        id=doc.id,
        title=doc.title,
        snippet=make_snippet(
            doc.content,
            description=raw_description if isinstance(raw_description, str) else None,
            query=query,
        ),
        original_score=raw_score,
        final_score=raw_score,
        url=doc.url,
        provider=provider,
        reasons=[reason],
        metadata=dict(metadata),
    )


def normalize_many(
    scored: list[tuple[Document, float]], provider: str, query: str = ""
) -> list[SearchResult]:
    return [normalize(doc, score, provider, query) for doc, score in scored]

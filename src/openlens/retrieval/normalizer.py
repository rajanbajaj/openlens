"""Result normalizer: (Document, raw score) -> SearchResult."""

from __future__ import annotations

from openlens.core.interfaces import Document, SearchResult

_SNIPPET_LEN = 200


def make_snippet(content: str, length: int = _SNIPPET_LEN) -> str:
    text = " ".join(content.split())
    return text if len(text) <= length else text[:length].rstrip() + "..."


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

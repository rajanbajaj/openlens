"""Deduplicator: drop repeat hits, keeping the first (highest-scored)."""

from __future__ import annotations

from openlens.core.interfaces import SearchResult


def dedupe_key(result: SearchResult) -> str:
    if result.url:
        return "url:" + result.url.strip().lower().rstrip("/")
    return "id:" + result.id


def deduplicate(results: list[SearchResult]) -> list[SearchResult]:
    seen: set[str] = set()
    unique: list[SearchResult] = []
    for result in results:
        key = dedupe_key(result)
        if key in seen:
            continue
        seen.add(key)
        unique.append(result)
    return unique

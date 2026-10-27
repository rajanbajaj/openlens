"""Dotted-path extraction and field mapping for generic HTTP providers.

Pure functions only: no network access and no provider imports. Used by
``CustomApiSearchProvider`` (integration plan step 2) to normalize
foreign payload shapes into :class:`SearchResult` lists.
"""

from __future__ import annotations

from typing import Any, Union

from openlens.core.interfaces import SearchResult

# Score configuration: rank-decay string or {"field": "<dotted-path>"}.
ScoreConfig = Union[str, dict[str, Any], None]


class _Missing:
    """Sentinel for a path that could not be resolved."""

    _instance = None

    def __new__(cls) -> "_Missing":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "MISSING"

    def __bool__(self) -> bool:
        return False


MISSING = _Missing()


def extract_path(data: Any, path: str | None) -> Any:
    """Resolve a dotted path (``a.b.0.c``) over dicts and lists.

    Dict segments look up string keys; list segments must be integer
    indices. Anything unresolvable (missing key, bad index, wrong type,
    empty path) returns the ``MISSING`` sentinel instead of raising.
    """
    if not path or not isinstance(path, str):
        return MISSING
    current: Any = data
    for segment in path.split("."):
        if isinstance(current, dict):
            if segment not in current:
                return MISSING
            current = current[segment]
        elif isinstance(current, (list, tuple)):
            try:
                index = int(segment)
            except (TypeError, ValueError):
                return MISSING
            if index < 0 or index >= len(current):
                return MISSING
            current = current[index]
        else:
            return MISSING
    return current


def _resolve_score(hit: Any, rank: int, score: ScoreConfig) -> tuple[float, str] | None:
    """Return ``(value, reason)`` or ``None`` when the hit must be skipped."""
    if score is None or (isinstance(score, str) and score == "rank_decay"):
        return 1.0 / (rank + 1), f"rank_decay rank {rank}"
    if isinstance(score, dict):
        raw = extract_path(hit, score.get("field"))
        if raw is MISSING or isinstance(raw, bool):
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        return value, f"score field '{score.get('field')}'"
    return None


def map_payload_to_results(
    payload: Any,
    *,
    results_path: str | None,
    fields: dict[str, str],
    score: ScoreConfig = "rank_decay",
    provider_name: str = "",
    engine_name: str = "",
    request_mode: str = "default",
) -> list[SearchResult]:
    """Map a decoded JSON payload to normalized :class:`SearchResult` items.

    Hits missing a required field (``id``, ``title``, ``snippet``) or an
    unresolvable/non-numeric score field are skipped, never crash. A
    missing ``results_path`` or a non-list target yields ``[]``.
    """
    raw_hits = extract_path(payload, results_path)
    if raw_hits is MISSING or not isinstance(raw_hits, (list, tuple)):
        return []
    fields = fields or {}
    results: list[SearchResult] = []
    for rank, hit in enumerate(raw_hits):
        if not isinstance(hit, dict):
            continue
        raw_id = extract_path(hit, fields.get("id", ""))
        raw_title = extract_path(hit, fields.get("title", ""))
        raw_snippet = extract_path(hit, fields.get("snippet", ""))
        if raw_id is MISSING or raw_title is MISSING or raw_snippet is MISSING:
            continue
        resolved = _resolve_score(hit, rank, score)
        if resolved is None:
            continue
        value, reason = resolved
        raw_url = extract_path(hit, fields.get("url", ""))
        results.append(
            SearchResult(
                id=str(raw_id),
                title=str(raw_title),
                snippet=str(raw_snippet),
                original_score=value,
                final_score=value,
                url=str(raw_url) if raw_url is not MISSING and raw_url is not None else None,
                provider=provider_name,
                reasons=[reason],
                metadata={"engine": engine_name, "request_mode": request_mode},
            )
        )
    return results

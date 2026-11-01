"""Domain grouping: one entry per website per result page.

Pure function over ranked hits (no I/O, no model): hits sharing a
normalized hostname fold into a single entry. The highest-ranked member
is the representative; the rest ride along as plain member dicts in
``metadata["group_members"]`` with an extractive composite in
``metadata["group_summary"]``. Grouping runs after ranking and before
the page slice, so a page of ``limit`` entries covers ``limit`` distinct
websites instead of several pages from one site.

Grouping key is the normalized hostname (lowercased, port/userinfo
stripped, IDN folded, leading ``www.`` removed) via stdlib only — no
public-suffix list, no network. Subdomains of one site (``news`` vs
``support``) intentionally stay separate, mirroring Google's host-level
treatment. Hits with unusable URLs pass through ungrouped, never
dropped.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from openlens.core.interfaces import SearchResult

#: Max extra members folded into one entry (representative excluded).
MAX_GROUP_MEMBERS = 3

#: Max member titles named in the extractive composite summary.
MAX_SUMMARY_TITLES = 2


def group_key(result: SearchResult) -> str:
    """Normalized hostname for a hit, or ``""`` when unusable."""
    url = getattr(result, "url", None)
    if not isinstance(url, str) or not url.strip():
        return ""
    candidate = url.strip()
    if "://" not in candidate:
        candidate = "//" + candidate
    try:
        host = urlparse(candidate).hostname or ""
    except ValueError:
        return ""
    host = host.rstrip(".").lower()
    if not host:
        return ""
    if host.startswith("www."):
        host = host[4:]
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host


def _member_dict(member: SearchResult) -> dict[str, Any]:
    """JSON-safe member payload (cache serialization stays intact)."""
    url = member.url if isinstance(member.url, str) else None
    return {
        "id": member.id,
        "title": member.title,
        "url": url,
        "snippet": member.snippet,
        "provider": member.provider,
    }


def _composite_summary(host: str, members: list[SearchResult]) -> str:
    """Extractive group summary: representative snippet + member titles."""
    titles = [m.title.strip() for m in members[:MAX_SUMMARY_TITLES]]
    titles = [title for title in titles if title]
    if not titles:
        return ""
    named = "; ".join(titles)
    return f"Also from {host}: {named}"


def group_results(
    results: list[SearchResult], *, max_members: int = MAX_GROUP_MEMBERS
) -> list[SearchResult]:
    """Fold same-host hits into one entry per hostname.

    First-seen (highest-ranked) member per host is the representative
    and keeps its rank position; later same-host hits attach as
    ``metadata["group_members"]`` (plain dicts, representative
    excluded) with ``metadata["group_count"]`` (all members including
    the representative) and ``metadata["group_summary"]`` (extractive
    composite, ``""`` when no member titles exist). Ungrouped hits and
    hits with unusable URLs pass through untouched — no new metadata
    keys are stamped on them. Never raises on malformed input.
    """
    if not isinstance(results, list):
        return []
    if not isinstance(max_members, int) or isinstance(max_members, bool):
        max_members = MAX_GROUP_MEMBERS
    max_members = max(0, max_members)
    grouped: list[SearchResult] = []
    by_host: dict[str, SearchResult] = {}
    member_lists: dict[str, list[SearchResult]] = {}
    for result in results:
        if not isinstance(result, SearchResult):
            continue
        key = group_key(result)
        if not key or key not in by_host:
            if key and key not in by_host:
                by_host[key] = result
                member_lists[key] = []
            grouped.append(result)
            continue
        members = member_lists[key]
        if len(members) >= max_members:
            continue
        members.append(result)
    for host, representative in by_host.items():
        members = member_lists[host]
        if not members:
            continue
        if isinstance(representative.metadata, dict):
            metadata = dict(representative.metadata)
        else:
            metadata = {}
        metadata["group_members"] = [_member_dict(m) for m in members]
        metadata["group_count"] = len(members) + 1
        metadata["group_summary"] = _composite_summary(host, members)
        representative.metadata = metadata
    return grouped

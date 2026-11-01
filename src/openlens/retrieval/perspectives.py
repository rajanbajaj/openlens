"""Query-time perspectives block: story views with stances and caveats.

Assembles the API ``perspectives`` block from ranked hits without
touching ranking: grouping reuses the pipeline's per-result
``story_id`` metadata first and falls back to
:func:`story_clusters.cluster_stories` for orphan hits (all modes,
including non-news). Stances come from :func:`stance.judge_stance`
(model-free in the request path: deterministic, offline-safe, zero
model budget). The block never reorders results and never raises:
every Degenerate input yields ``[]``.
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from openlens.retrieval.stance import StanceVerdict, judge_stance
from openlens.retrieval.story_clusters import MAX_CLUSTER_HITS, cluster_stories

#: Cap on story views per response (a refinement aid, not a corpus view).
MAX_VIEWS = 5

#: Cap on judged members per view (bounds the block for long stories).
MAX_MEMBERS = 8

#: Cap on trust caveats per view (keeps the block readable).
MAX_CAVEATS = 5

#: Cap on fallback answer length (matches summary budgets elsewhere).
MAX_FALLBACK_CHARS = 1200


def _result_id(hit: Any, fallback: str) -> str:
    raw = getattr(hit, "id", "")
    return raw if isinstance(raw, str) and raw else fallback


def _host(url: Any) -> str:
    if not isinstance(url, str) or not url:
        return ""
    try:
        return urllib.parse.urlsplit(url).netloc.lower()
    except ValueError:
        return ""


def _claim_text(hit: Any) -> str:
    metadata = getattr(hit, "metadata", {})
    if isinstance(metadata, dict):
        claims = metadata.get("key_claims")
        if isinstance(claims, list):
            for claim in claims:
                if isinstance(claim, str) and claim.strip():
                    return claim.strip()
    title = getattr(hit, "title", "")
    return title.strip() if isinstance(title, str) else ""


def _credibility_of(hit: Any) -> float | None:
    metadata = getattr(hit, "metadata", {})
    if not isinstance(metadata, dict):
        return None
    signals = metadata.get("signals")
    if isinstance(signals, dict):
        trust = signals.get("trust")
        if isinstance(trust, (int, float)) and not isinstance(trust, bool):
            return float(trust)
    trusted = metadata.get("trusted")
    if trusted is True:
        return 1.0
    if trusted is False:
        return 0.5
    return None


def _trust_caveats(member: Any) -> list[str]:
    """Human-readable caveats for one member (warnings + low trust)."""
    caveats: list[str] = []
    title = getattr(member, "title", "") or ""
    short = title.strip()[:80] if isinstance(title, str) else ""
    label = short or _result_id(member, "this source")
    for warning in getattr(member, "warnings", []) or []:
        if isinstance(warning, str) and warning.strip():
            caveats.append(f"{label}: {warning.strip()[:200]}")
    credibility = _credibility_of(member)
    if credibility is not None and credibility < 0.5:
        caveats.append(f"{label}: low credibility signal ({credibility:.2f})")
    return caveats


@dataclass
class PerspectiveView:
    """One story view: claim, member stances, and honesty notes."""

    story_id: str
    claim: str
    representative_id: str
    member_ids: list[str] = field(default_factory=list)
    stances: list[dict[str, str]] = field(default_factory=list)
    supports: int = 0
    refutes: int = 0
    neutral: int = 0
    multi_source: bool = False
    missing_note: str | None = None
    trust_caveats: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe view for the API wire (no model text, no internals)."""
        return {
            "story_id": self.story_id,
            "claim": self.claim,
            "representative_id": self.representative_id,
            "member_ids": list(self.member_ids),
            "stances": [dict(entry) for entry in self.stances],
            "supports": self.supports,
            "refutes": self.refutes,
            "neutral": self.neutral,
            "multi_source": self.multi_source,
            "missing_note": self.missing_note,
            "trust_caveats": list(self.trust_caveats),
        }


def _missing_note(supports: int, refutes: int) -> str | None:
    if supports == 0 and refutes == 0:
        return "no clear supporting or refuting source in this cluster"
    if refutes == 0:
        return "no refuting source in this cluster"
    if supports == 0:
        return "no supporting source in this cluster"
    return None


def build_perspectives(
    results: list[Any],
    *,
    model: Any | None = None,
    max_views: int = MAX_VIEWS,
) -> list[PerspectiveView]:
    """Group ranked hits into story views with stances and caveats.

    ``results`` are rank-ordered core hits; representative and member
    order follow that order. ``model`` is passed to
    :func:`stance.judge_stance` (``None`` = deterministic keyword
    fallback, used on every request path). Never raises: malformed
    hits are skipped, degenerate input yields ``[]``.
    """
    if not isinstance(results, list) or not results:
        return []
    try:
        max_views = max(0, int(max_views))
    except (TypeError, ValueError):
        max_views = MAX_VIEWS
    if max_views == 0:
        return []

    pool = [hit for hit in results[:MAX_CLUSTER_HITS] if hit is not None]
    if not pool:
        return []

    # Rank-preserving groups: pipeline story_id first, clusterer orphans.
    groups: dict[str, list[Any]] = {}
    order: list[str] = []
    orphans: list[Any] = []
    for hit in pool:
        metadata = getattr(hit, "metadata", {})
        story_id = metadata.get("story_id") if isinstance(metadata, dict) else None
        if isinstance(story_id, str) and story_id:
            if story_id not in groups:
                groups[story_id] = []
                order.append(story_id)
            groups[story_id].append(hit)
        else:
            orphans.append(hit)
    for cluster in cluster_stories(orphans):
        story_id = f"cluster-{cluster.story_id}"
        # Map cluster member ids back to orphan hits by SearchResult id.
        id_to_hit: dict[str, Any] = {}
        for hit in orphans:
            raw = getattr(hit, "id", None)
            if isinstance(raw, str):
                id_to_hit.setdefault(raw, hit)
        resolved = [id_to_hit[mid] for mid in cluster.member_ids if mid in id_to_hit]
        if len(resolved) >= 2:
            groups[story_id] = resolved
            order.append(story_id)
    # Orphans left single render as plain results (never forced in).

    views: list[PerspectiveView] = []
    for story_id in order:
        members = groups[story_id][:MAX_MEMBERS]
        if len(members) < 2:
            continue
        representative = members[0]
        claim = _claim_text(representative)
        if not claim:
            continue
        stances: list[dict[str, str]] = []
        supports = refutes = neutral = 0
        caveats: list[str] = []
        for position, member in enumerate(members):
            snippet = getattr(member, "snippet", "")
            passage = snippet if isinstance(snippet, str) else ""
            try:
                verdict: StanceVerdict = judge_stance(
                    claim,
                    passage,
                    _result_id(member, f"member-{position}"),
                    model=model,
                )
            except Exception:  # noqa: BLE001 — stance never breaks the block
                verdict = StanceVerdict(stance="neutral", evidence="")
            if verdict.stance == "supports":
                supports += 1
            elif verdict.stance == "refutes":
                refutes += 1
            else:
                neutral += 1
            stances.append(
                {
                    "source_id": verdict.source_id,
                    "stance": verdict.stance,
                    "evidence": verdict.evidence,
                }
            )
            for caveat in _trust_caveats(member):
                if len(caveats) < MAX_CAVEATS and caveat not in caveats:
                    caveats.append(caveat)
        hosts = {_host(getattr(member, "url", "")) for member in members}
        hosts.discard("")
        views.append(
            PerspectiveView(
                story_id=story_id,
                claim=claim,
                representative_id=_result_id(representative, "rep-0"),
                member_ids=[
                    _result_id(member, f"m-{i}") for i, member in enumerate(members)
                ],
                stances=stances,
                supports=supports,
                refutes=refutes,
                neutral=neutral,
                multi_source=len(hosts) >= 2,
                missing_note=_missing_note(supports, refutes),
                trust_caveats=caveats,
            )
        )
        if len(views) >= max_views:
            break
    return views


def extractive_fallback_answer(hits: list[Any]) -> str:
    """Coherent extractive answer: first-sentence stitching with markers.

    Takes the lead sentence of each of the top hits (deduped, in rank
    order) and appends ``[n]`` markers matching the citations order, so
    the fallback reads as prose instead of one raw snippet. Returns
    ``""`` when no hit has usable text.
    """
    import re

    sentences: list[str] = []
    seen: set[str] = set()
    for index, hit in enumerate(hits[:5]):
        snippet = getattr(hit, "snippet", "")
        if not isinstance(snippet, str) or not snippet.strip():
            continue
        text = " ".join(snippet.split())
        parts = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)
        lead = (parts[0] if parts else "").strip()
        if len(lead) < 20:
            continue
        key = lead.casefold()
        if key in seen:
            continue
        seen.add(key)
        sentences.append(f"{lead} [{index + 1}]")
    answer = " ".join(sentences).strip()
    if len(answer) > MAX_FALLBACK_CHARS:
        answer = answer[:MAX_FALLBACK_CHARS].rsplit(" ", 1)[0].rstrip() + "…"
    return answer

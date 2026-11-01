"""Stance judging: does a source passage support or refute a claim?

Model-first with a deterministic keyword fallback, so clusters render
stance badges when a model backend is configured and degrade to
``neutral``/``unrated`` offline. The model path parses one strict word;
anything unparseable falls back instead of raising.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

Stance = Literal["supports", "refutes", "neutral", "unrated"]

_SUPPORT_CUES = frozenset(
    {
        "confirm",
        "confirms",
        "confirmed",
        "shows",
        "showed",
        "found",
        "proves",
        "proves",
        "agrees",
        "supports",
        "backs",
        "demonstrates",
        "evidence",
    }
)

_REFUTE_CUES = frozenset(
    {
        "denies",
        "denied",
        "false",
        "refutes",
        "refuted",
        "debunk",
        "debunked",
        "contradicts",
        "disagrees",
        "disagreed",
        "rejects",
        "rejected",
        "no evidence",
        "misleading",
    }
)


@dataclass
class StanceVerdict:
    """One source's stance toward the cluster claim."""

    stance: Stance
    evidence: str
    source_id: str = ""


def _fallback_stance(passage: str) -> tuple[Stance, str]:
    """Keyword-count stance for model-less runs (deterministic)."""
    lowered = passage.lower()
    support = sum(1 for cue in _SUPPORT_CUES if cue in lowered)
    refute = sum(1 for cue in _REFUTE_CUES if cue in lowered)
    if support > refute:
        return "supports", passage.strip()[:280]
    if refute > support:
        return "refutes", passage.strip()[:280]
    return "neutral", ""


def judge_stance(
    claim: str,
    passage: str,
    source_id: str = "",
    *,
    model: Any | None = None,
) -> StanceVerdict:
    """Judge one passage's stance toward ``claim``.

    ``model`` is any object with ``complete(prompt) -> str``; when it is
    ``None`` (or its reply is unparseable) the keyword fallback decides.
    Never raises: empty inputs yield ``neutral``.
    """
    passage = (passage or "").strip()
    if not (claim or "").strip() or not passage:
        return StanceVerdict(stance="neutral", evidence="", source_id=source_id)
    if model is not None:
        try:
            reply = model.complete(
                "Claim: "
                + claim.strip()
                + "\nPassage: "
                + passage[:2000]
                + "\nDoes the passage SUPPORT, REFUTE, or take NO position on "
                "the claim? Reply with exactly one of those words on the "
                "first line, then the supporting quote on the next line."
            )
        except Exception:
            reply = ""
        first = (reply or "").strip().split("\n")[0].strip().lower()
        if first.startswith("support"):
            lines = (reply or "").strip().split("\n")
            quote = lines[1].strip()[:280] if len(lines) > 1 else passage[:280]
            return StanceVerdict(stance="supports", evidence=quote, source_id=source_id)
        if first.startswith("refut"):
            lines = (reply or "").strip().split("\n")
            quote = lines[1].strip()[:280] if len(lines) > 1 else passage[:280]
            return StanceVerdict(stance="refutes", evidence=quote, source_id=source_id)
        if first.startswith(("neutral", "no position", "no stance")):
            return StanceVerdict(stance="neutral", evidence="", source_id=source_id)
    stance, evidence = _fallback_stance(passage)
    return StanceVerdict(stance=stance, evidence=evidence, source_id=source_id)

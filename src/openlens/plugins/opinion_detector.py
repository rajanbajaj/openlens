"""Opinion/commentary detector: advisory fact-vs-opinion labels (CAT4/C3.6).

ENRICHMENT-stage, optional (``mandatory=False``). Writes an advisory
``opinion`` label into ``result.metadata`` (``"opinion"``, ``"fact"``,
or ``"unknown"``) plus a reason; it NEVER filters, reorders, or
scores. The signal is deterministic and offline: an opinion lexicon
matched against title/snippet, first-person pronouns, and the
crawler-truth ``section`` field (``opinion``/``editorial``/``comment``
sections). No model backend, no network.
"""

from __future__ import annotations

import re
from typing import Any

from openlens.core.context import SearchContext
from openlens.core.interfaces import PipelineStage, PluginType
from openlens.plugins.base import BasePlugin

#: Label written into ``result.metadata["opinion"]``.
METADATA_KEY = "opinion"

#: Opinion-signal phrases (lowercase substring match on title+snippet).
OPINION_PHRASES: tuple[str, ...] = (
    "in my opinion",
    "i think",
    "i believe",
    "in our view",
    "editorial",
    "op-ed",
    "commentary",
    "column:",
    "review:",
    "we argue",
    "should be banned",
    "must be stopped",
)

#: First-person markers suggesting commentary rather than reporting.
_FIRST_PERSON_RE = re.compile(r"\b(i|we|my|our|mine|ours)\b", re.IGNORECASE)

#: Crawler-truth sections that mark commentary outright.
EDITORIAL_SECTIONS: frozenset[str] = frozenset(
    {"opinion", "editorial", "comment", "commentary", "column", "op-ed", "review"}
)


def classify_opinion(title: str, snippet: str, section: Any) -> tuple[str, str]:
    """Return the ``(label, reason)`` pair; pure and stage-agnostic.

    ``"opinion"`` when the section is editorial or two independent
    markers fire (lexicon phrase, first-person voice); ``"fact"`` when
    no marker fires; ``"unknown"`` when inputs are unusable.
    """
    if not isinstance(title, str) or not isinstance(snippet, str):
        return "unknown", "opinion: missing title/snippet; unknown"
    if isinstance(section, str) and section.strip().lower() in EDITORIAL_SECTIONS:
        return "opinion", f"opinion: editorial section '{section.strip()}'"
    haystack = f"{title}\n{snippet}".lower()
    markers = 0
    phrase_hit: str | None = None
    for phrase in OPINION_PHRASES:
        if phrase in haystack:
            markers += 1
            phrase_hit = phrase
            break
    first_person = bool(_FIRST_PERSON_RE.search(f"{title}\n{snippet}"))
    if first_person:
        markers += 1
    if markers >= 2 or (phrase_hit is not None and first_person):
        return "opinion", f"opinion: commentary markers ('{phrase_hit}', first-person)"
    if phrase_hit is not None:
        return "opinion", f"opinion: commentary phrase ('{phrase_hit}')"
    if not haystack.strip():
        return "unknown", "opinion: empty title/snippet; unknown"
    return "fact", "opinion: no commentary markers; reads as factual"


class OpinionDetector(BasePlugin):
    """Advisory fact/opinion labels; owns the label, never warns."""

    @property
    def name(self) -> str:
        return "opinion-detector"

    @property
    def plugin_type(self) -> PluginType:
        return PluginType.ENRICHER

    @property
    def stage(self) -> PipelineStage:
        return PipelineStage.ENRICHMENT

    @property
    def mandatory(self) -> bool:
        return False

    def run(self, context: SearchContext) -> SearchContext:
        for result in context.results:
            label, reason = classify_opinion(
                result.title, result.snippet, result.metadata.get("section")
            )
            result.metadata[METADATA_KEY] = label
            result.reasons.append(reason)
        return context

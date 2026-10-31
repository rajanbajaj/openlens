"""Integrity-signal scorer (SCORING stage, optional).

Emits five deterministic signals in ``0..1`` into
``result.metadata["signals"]`` from indexed data only — stored metadata
fields plus the indexed title/snippet. No I/O, no network, no external
engines, no wall-clock reads:

- ``safety``: 0.0 when indexed metadata carries an adult-content label
  (``content_safety`` as emitted by the offline crawler extractor),
  else 1.0. Ordering only; exclusion stays with the safety filter.
- ``privacy``: 0.2 when a pinned PII pattern (email / phone /
  government-id, mirroring the privacy filter defaults) matches the
  indexed title/snippet, else 1.0. Exclusion/redaction stays with the
  privacy filter.
- ``fairness``: 0.4 when pinned gender-generalization phrasing matches
  the indexed title/snippet, else 1.0. A narrow, documented heuristic —
  never a gender classifier.
- ``news_trust``: combines the indexed ``trusted`` bool with the indexed
  ``published`` date (trusted+dated 1.0, trusted 0.8, dated 0.6,
  neither 0.5, distrusted 0.2).
- ``sponsored``: 0.0 when indexed metadata flags the hit as sponsored
  (``sponsored: true`` as emitted by the offline crawler extractor from
  ``rel="sponsored"`` markup), else 1.0, so organic ranking demotes —
  but still labels — sponsored hits.

Every scored hit gets one concise ``integrity: ...`` reason plus a
warning whenever a signal flags (adult label, possible PII,
generalizing phrasing, distrusted/undated news, sponsored label), so
the ranker composite stays explainable end to end.

The pure helpers are stage-agnostic so synthesis can fold them into the
``Ranker`` itself without rewriting the math.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from openlens.core.context import SearchContext
from openlens.core.interfaces import PipelineStage, PluginType
from openlens.plugins._signals import NEUTRAL_SIGNAL, is_sponsored_hit, set_signal
from openlens.plugins.base import BasePlugin
from openlens.plugins.privacy_filter import DEFAULT_PATTERNS

#: Signal keys written into ``result.metadata["signals"]``.
SAFETY_SIGNAL = "safety"
PRIVACY_SIGNAL = "privacy"
FAIRNESS_SIGNAL = "fairness"
NEWS_TRUST_SIGNAL = "news_trust"
SPONSORED_SIGNAL = "sponsored"

#: Indexed ``content_safety`` values treated as an adult-content label.
_UNSAFE_LABELS = frozenset({"adult", "explicit", "unsafe"})

#: PII classes scanned for the privacy signal (email / phone /
#: government-id built-ins only; street addresses intentionally have no
#: default and match only config-supplied patterns, so they are skipped
#: here exactly as in the privacy filter).
_PII_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for cls in ("email", "phone", "government_id")
    for pattern in DEFAULT_PATTERNS.get(cls, ())
)

#: Narrow gender-generalization detectors over indexed text. These match
#: only explicit group-level generalizations ("women can't ...",
#: "men are worse ... than ..."), never the mere mention of a gendered
#: term.
_GENERALIZATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(women|men|girls|boys|females|males)\s+"
        r"(can't|cannot|never|always|shouldn't|should\s+never)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(women|men)\s+are\s+"
        r"(worse|better|naturally|biologically)\b.{0,40}\b(than|at)\b",
        re.IGNORECASE,
    ),
)

#: Indexed date keys consulted by the news-trust signal, in order.
_DATE_KEYS: tuple[str, ...] = ("published", "published_at", "date")


def _has_date(metadata: dict[str, Any]) -> bool:
    for key in _DATE_KEYS:
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return True
    return False


def safety_of(metadata: dict[str, Any]) -> tuple[float, Optional[str]]:
    """Score the indexed adult-content label; warning when flagged."""
    label = metadata.get("content_safety")
    if isinstance(label, str) and label.strip().lower() in _UNSAFE_LABELS:
        return 0.0, "integrity: adult-content label in indexed metadata"
    return 1.0, None


def privacy_of(title: str, snippet: str) -> tuple[float, Optional[str]]:
    """Score PII-pattern matches over indexed text; warning when found."""
    haystack = f"{title or ''}\n{snippet or ''}"
    for pattern in _PII_PATTERNS:
        if pattern.search(haystack):
            return 0.2, "integrity: possible PII in indexed text"
    return 1.0, None


def fairness_of(title: str, snippet: str) -> tuple[float, Optional[str]]:
    """Score gender-generalizing phrasing over indexed text."""
    haystack = f"{title or ''}\n{snippet or ''}"
    for pattern in _GENERALIZATION_PATTERNS:
        if pattern.search(haystack):
            return 0.4, "integrity: gender-generalizing phrasing in indexed text"
    return 1.0, None


def news_trust_of(metadata: dict[str, Any]) -> tuple[float, str]:
    """Combine the indexed trusted bool with the indexed publication date."""
    trusted = metadata.get("trusted")
    dated = _has_date(metadata)
    if trusted is True and dated:
        return 1.0, "trusted source with publication date"
    if trusted is True:
        return 0.8, "trusted source, no publication date"
    if trusted is False:
        return 0.2, "distrusted source"
    if dated:
        return 0.6, "dated source, no trust tier"
    return NEUTRAL_SIGNAL, "no trust tier or publication date"


def sponsored_of(metadata: dict[str, Any]) -> tuple[float, Optional[str]]:
    """Score the indexed sponsored flag; warning when labeled.

    Truthy spellings (``1``/``"true"``/``"yes"``) and the federated
    ``is_sponsored`` marker flag exactly like canonical
    ``sponsored: true``; see ``_signals.is_sponsored_hit``.
    """
    if is_sponsored_hit(metadata):
        return 0.0, "integrity: labeled sponsored in indexed page markup"
    return 1.0, None


class IntegrityScorer(BasePlugin):
    """Scores safety/privacy/fairness/news-trust/sponsored from indexed data."""

    @property
    def name(self) -> str:
        return "integrity-scorer"

    @property
    def plugin_type(self) -> PluginType:
        return PluginType.SCORER

    @property
    def stage(self) -> PipelineStage:
        return PipelineStage.SCORING

    @property
    def mandatory(self) -> bool:
        return False

    def run(self, context: SearchContext) -> SearchContext:
        for result in context.results:
            metadata = result.metadata or {}
            safety, safety_warn = safety_of(metadata)
            privacy, privacy_warn = privacy_of(result.title, result.snippet)
            fairness, fairness_warn = fairness_of(result.title, result.snippet)
            news_trust, news_reason = news_trust_of(metadata)
            sponsored, sponsored_warn = sponsored_of(metadata)
            set_signal(result, SAFETY_SIGNAL, safety)
            set_signal(result, PRIVACY_SIGNAL, privacy)
            set_signal(result, FAIRNESS_SIGNAL, fairness)
            set_signal(result, NEWS_TRUST_SIGNAL, news_trust)
            set_signal(result, SPONSORED_SIGNAL, sponsored)
            result.reasons.append(
                f"integrity: safety {safety:.2f}, privacy {privacy:.2f}, "
                f"fairness {fairness:.2f}, news_trust {news_trust:.2f} "
                f"({news_reason}), sponsored {sponsored:.2f}"
            )
            for warning in (
                safety_warn,
                privacy_warn,
                fairness_warn,
                sponsored_warn,
            ):
                if warning is not None:
                    result.warnings.append(warning)
            if news_trust <= 0.2:
                result.warnings.append(f"integrity: {news_reason}")
        return context

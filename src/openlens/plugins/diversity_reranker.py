"""Source-diversity reranker: soft per-domain demotion as an ordering signal.

Optional EXPLANATION-stage plugin. Emits a ``diversity`` ordering signal in
the shared 0..1 signals namespace (see ``plugins/_signals.py``), consumed by
the engine-owned ``Ranker`` composite downstream:

``signal = 1 / (1 + prior_same_domain_count)`` counted in incoming order, so
the first hit per domain scores 1.0, the second 0.5, the third ~0.33, and so
on. Rank-1 is therefore never demoted by diversity alone.

Signals only: this plugin never re-sorts ``context.results`` (the final
ranker sort would overwrite a private re-sort anyway) and never removes or
excludes any hit — a weighted-sum composite can only demote softly. Single
O(n) dict pass, no I/O. Domains are normalized with ``extract_domain``; hits
with a missing/unusable URL share the ``""`` key and demote against each
other uniformly rather than via a special case.
"""

from __future__ import annotations

from typing import Any

from openlens.core.context import SearchContext
from openlens.core.interfaces import (
    PipelineStage,
    PluginType,
)
from openlens.plugins._signals import extract_domain
from openlens.plugins._signals import set_signal as _set_signal
from openlens.plugins.base import BasePlugin

DIVERSITY_SIGNAL = "diversity"


def compute_diversity_signals(domains: list[str]) -> list[float]:
    """Pure signal math, stage-agnostic for reuse inside the ranker.

    Returns ``1 / (1 + prior_same_domain_count)`` per position in incoming
    order: first occurrence of a domain scores 1.0, second 0.5, and so on.
    """
    counts: dict[str, int] = {}
    signals: list[float] = []
    for domain in domains:
        prior = counts.get(domain, 0)
        signals.append(1.0 / (1.0 + prior))
        counts[domain] = prior + 1
    return signals


class DiversityReranker(BasePlugin):
    """Emits the per-domain ``diversity`` ordering signal; never re-sorts."""

    @property
    def name(self) -> str:
        return "diversity-reranker"

    @property
    def plugin_type(self) -> PluginType:
        return PluginType.SCORER

    @property
    def stage(self) -> PipelineStage:
        return PipelineStage.EXPLANATION

    @property
    def mandatory(self) -> bool:
        return False

    def configure(self, config: dict[str, Any]) -> None:
        super().configure(config)

    def run(self, context: SearchContext) -> SearchContext:
        domains = [extract_domain(result.url or "") for result in context.results]
        for result, domain, signal in zip(
            context.results, domains, compute_diversity_signals(domains)
        ):
            _set_signal(result, DIVERSITY_SIGNAL, signal)
            if signal < 1.0:
                occurrence = int(round(1.0 / signal))
                result.reasons.append(
                    f"source diversity {signal:.2f} "
                    f"({domain or 'unknown source'}, hit #{occurrence})"
                )
        return context

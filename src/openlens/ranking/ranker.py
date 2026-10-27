"""Ranker plugin (RANKING stage): applies the per-mode profile and sorts.

Composite signal per hit::

    final = relevance_weight * final_score
            + trust_boost * trusted?1:0
            + recency_boost * recency_score(0..1)
            + keyword_boost * keyword_hits
            - warning_penalty * len(warnings)

The profile is selected from ``SearchRequest.mode``; the configured
``profile`` is the fallback when the request mode is unknown to the caller.
In practice ``Engine`` passes modes through and unknown modes fail loudly via
``get_profile`` so typos never silently misrank.
"""

from __future__ import annotations

from typing import Any

from openlens.core.context import SearchContext
from openlens.core.interfaces import PipelineStage, PluginType
from openlens.plugins.base import BasePlugin
from openlens.ranking.profiles import RankingProfile, get_profile


class Ranker(BasePlugin):
    """Sorts results by the active mode profile's composite signal."""

    @property
    def name(self) -> str:
        return "ranker"

    @property
    def plugin_type(self) -> PluginType:
        return PluginType.RANKER

    @property
    def stage(self) -> PipelineStage:
        return PipelineStage.RANKING

    @property
    def mandatory(self) -> bool:
        return False

    def configure(self, config: dict[str, Any]) -> None:
        super().configure(config)
        fallback = self._config.get("profile", "default")
        if not isinstance(fallback, str):
            from openlens.core.interfaces import ConfigError

            raise ConfigError("Ranker 'profile' must be a string.")
        get_profile(fallback)  # validate loudly at startup
        self._fallback_profile = fallback

    def composite(self, profile: RankingProfile, result: Any) -> float:
        """Compute the composite score for one result under ``profile``."""
        trusted = 1.0 if result.metadata.get("trusted") is True else 0.0
        try:
            recency = float(result.metadata.get("recency_score", 0.0))
        except (TypeError, ValueError):
            recency = 0.0
        recency = max(0.0, min(1.0, recency))
        keyword_hits = 0
        if profile.keywords:
            haystack = f"{result.title}\n{result.snippet}".lower()
            keyword_hits = sum(haystack.count(kw.lower()) for kw in profile.keywords)
        return (
            profile.relevance_weight * result.final_score
            + profile.trust_boost * trusted
            + profile.recency_boost * recency
            + profile.keyword_boost * keyword_hits
            - profile.warning_penalty * len(result.warnings)
        )

    def run(self, context: SearchContext) -> SearchContext:
        mode = context.request.mode or self._fallback_profile
        profile = get_profile(mode)
        for result in context.results:
            result.final_score = self.composite(profile, result)
            result.reasons.append(f"ranked with profile '{profile.name}'")
        # Stable sort: ties keep provider/scorer order (deterministic).
        context.results.sort(key=lambda r: r.final_score, reverse=True)
        return context

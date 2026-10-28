"""Ranker plugin (RANKING stage): applies the per-mode profile and sorts.

Composite signal per hit::

    final = relevance_weight * final_score
            + trust_boost * trusted?1:0
            + recency_boost * recency_score(0..1)
            + keyword_boost * keyword_hits
            - warning_penalty * len(warnings)
            + trust_weight * signals["trust"]
            + freshness_weight * signals["freshness"]
            + preference_weight * signals["preference"]

Missing signal keys degrade to the neutral 0.5 default (never ``KeyError``),
so with no scorer enabled every hit carries the same constant offset and
ordering is unchanged. The legacy ``trust_boost``/``recency_boost`` terms
stay alongside the new signal terms; no term is removed.

The profile is selected from ``SearchRequest.mode``; the configured
``profile`` is the fallback when the request mode is unknown to the caller.
In practice ``Engine`` passes modes through and unknown modes fail loudly via
``ConfigError`` so typos never silently misrank.
"""

from __future__ import annotations

from typing import Any

from openlens.core.context import SearchContext
from openlens.core.interfaces import (
    ConfigError,
    PipelineStage,
    PluginType,
    SearchResult,
)
from openlens.plugins._signals import get_signal
from openlens.plugins.base import BasePlugin
from openlens.ranking.profiles import BUILTIN_PROFILES, RankingProfile


class Ranker(BasePlugin):
    """Sorts results by the active mode profile's composite signal."""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        profiles: dict[str, RankingProfile] | None = None,
    ) -> None:
        self._profiles: dict[str, RankingProfile] = (
            dict(profiles) if profiles is not None else dict(BUILTIN_PROFILES)
        )
        super().__init__(config)

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

    def _lookup(self, name: str) -> RankingProfile:
        """Return the profile for ``name`` or fail loudly on unknown names."""
        try:
            return self._profiles[name]
        except KeyError:
            raise ConfigError(
                f"Unknown ranking profile '{name}'. "
                f"Known: {', '.join(sorted(self._profiles))}."
            ) from None

    def configure(self, config: dict[str, Any]) -> None:
        super().configure(config)
        fallback = self._config.get("profile", "default")
        if not isinstance(fallback, str):
            raise ConfigError("Ranker 'profile' must be a string.")
        self._lookup(fallback)  # validate loudly at startup
        self._fallback_profile = fallback

    def composite(self, profile: RankingProfile, result: SearchResult) -> float:
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
            + profile.trust_weight * get_signal(result, "trust")
            + profile.freshness_weight * get_signal(result, "freshness")
            + profile.preference_weight * get_signal(result, "preference")
        )

    def run(self, context: SearchContext) -> SearchContext:
        mode = context.request.mode or self._fallback_profile
        profile = self._lookup(mode)
        for result in context.results:
            result.final_score = self.composite(profile, result)
            result.reasons.append(f"ranked with profile '{profile.name}'")
        # Stable sort: ties keep provider/scorer order (deterministic).
        context.results.sort(key=lambda r: r.final_score, reverse=True)
        return context

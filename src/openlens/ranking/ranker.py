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
            + diversity_weight * signals["diversity"]
            + perspective_weight * signals["perspective"]
            + commerce_weight * signals["commerce"]
            + safety_weight * signals["safety"]
            + privacy_weight * signals["privacy"]
            + fairness_weight * signals["fairness"]
            + news_trust_weight * signals["news_trust"]
            + sponsored_weight * signals["sponsored"]

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

import math
from dataclasses import fields, replace
from typing import Any

from openlens.core.context import SearchContext
from openlens.core.interfaces import (
    ConfigError,
    PipelineStage,
    PluginError,
    PluginType,
    SearchResult,
)
from openlens.plugins._signals import get_signal
from openlens.plugins.base import BasePlugin
from openlens.ranking.profiles import BUILTIN_PROFILES, RankingProfile

#: Filter key carrying per-request signal-weight overrides.
WEIGHT_OVERRIDES_FILTER_KEY = "weight_overrides"

#: Profile fields a caller may override per request. Identity fields
#: (``name``, ``description``) and the keyword list are excluded: only
#: numeric signal weights are tunable, and every value must be finite.
_OVERRIDABLE_WEIGHT_FIELDS: frozenset[str] = frozenset(
    field.name
    for field in fields(RankingProfile)
    if field.name not in ("name", "description", "keywords")
)


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
            + profile.diversity_weight * get_signal(result, "diversity")
            + profile.perspective_weight * get_signal(result, "perspective")
            + profile.commerce_weight * get_signal(result, "commerce")
            + profile.safety_weight * get_signal(result, "safety")
            + profile.privacy_weight * get_signal(result, "privacy")
            + profile.fairness_weight * get_signal(result, "fairness")
            + profile.news_trust_weight * get_signal(result, "news_trust")
            + profile.sponsored_weight * get_signal(result, "sponsored")
        )

    def _weight_overrides(self, context: SearchContext) -> dict[str, float]:
        """Validate per-request ``weight_overrides`` (fail closed).

        Returns the validated overrides ({} when the caller sent none).
        Unknown fields, non-mapping payloads, and non-finite values
        raise a mandatory ``PluginError`` so a typo never silently
        serves a misweighted ranking.
        """
        raw = (context.request.filters or {}).get(WEIGHT_OVERRIDES_FILTER_KEY)
        if raw is None:
            return {}
        if not isinstance(raw, dict):
            raise PluginError(
                "weight_overrides must be a mapping of signal weight to number",
                plugin_name=self.name,
                stage=self.stage,
                mandatory=True,
            )
        overrides: dict[str, float] = {}
        for key, value in raw.items():
            if key not in _OVERRIDABLE_WEIGHT_FIELDS:
                raise PluginError(
                    f"Unknown ranking weight '{key}'. "
                    f"Known: {', '.join(sorted(_OVERRIDABLE_WEIGHT_FIELDS))}.",
                    plugin_name=self.name,
                    stage=self.stage,
                    mandatory=True,
                )
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise PluginError(
                    f"Ranking weight '{key}' must be a number.",
                    plugin_name=self.name,
                    stage=self.stage,
                    mandatory=True,
                )
            numeric = float(value)
            if not math.isfinite(numeric):
                raise PluginError(
                    f"Ranking weight '{key}' must be finite.",
                    plugin_name=self.name,
                    stage=self.stage,
                    mandatory=True,
                )
            overrides[key] = numeric
        return overrides

    def run(self, context: SearchContext) -> SearchContext:
        mode = context.request.mode or self._fallback_profile
        profile = self._lookup(mode)
        overrides = self._weight_overrides(context)
        if overrides:
            profile = replace(profile, **dict(overrides))  # type: ignore[arg-type]
        for result in context.results:
            result.final_score = self.composite(profile, result)
            result.reasons.append(f"ranked with profile '{profile.name}'")
            if overrides:
                result.reasons.append(
                    "custom weights: "
                    + ", ".join(f"{key}={overrides[key]}" for key in sorted(overrides))
                )
        if overrides:
            context.applied_filters.append(f"{self.name}:weight_overrides")
        # Stable sort: ties keep provider/scorer order (deterministic).
        context.results.sort(key=lambda r: r.final_score, reverse=True)
        return context

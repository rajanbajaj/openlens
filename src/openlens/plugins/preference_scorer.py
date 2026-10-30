"""Preference scorer: advisory boosts from request-level preferences.

Optional SCORING-stage plugin. Reads only the ``prefer_domains`` /
``prefer_topics`` request filters (passed through by filter-pack): domain
matches are exact-or-subdomain on the normalized domain, topic matches are
case-insensitive token matches over title + snippet.

Matches yield bounded boosts (signal 0..1). Malformed filter values
(non-list values, non-string entries) are ignored and recorded once per
request in ``context.metadata["preference_warnings"]`` (in-process only,
never part of the API diagnostics payload). No cross-query state is kept on
the instance; plugin instances are long-lived so per-request data stays in
``SearchContext``/results.
"""

from __future__ import annotations

import re
from typing import Any

from openlens.core.context import SearchContext
from openlens.core.interfaces import (
    ConfigError,
    PipelineStage,
    PluginType,
)
from openlens.plugins._signals import (
    NEUTRAL_SIGNAL as NEUTRAL,
    extract_domain,
    set_signal as _set_signal,
)
from openlens.plugins.base import BasePlugin

_TOKEN = re.compile(r"[a-z0-9]+")


def _matches_domain(result_domain: str, preference: str) -> bool:
    return (
        bool(result_domain)
        and bool(preference)
        and (result_domain == preference or result_domain.endswith("." + preference))
    )


def _tokens(text: str) -> set[str]:
    return set(_TOKEN.findall(text.lower()))


class PreferenceScorer(BasePlugin):
    """Applies per-request domain/topic preference boosts."""

    @property
    def name(self) -> str:
        return "preference-scorer"

    @property
    def plugin_type(self) -> PluginType:
        return PluginType.SCORER

    @property
    def stage(self) -> PipelineStage:
        return PipelineStage.SCORING

    @property
    def mandatory(self) -> bool:
        return False

    def configure(self, config: dict[str, Any]) -> None:
        super().configure(config)
        try:
            self._domain_boost = float(self._config.get("domain_boost", 0.5))
            self._topic_boost = float(self._config.get("topic_boost", 0.5))
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"PreferenceScorer boosts must be numbers: {exc}"
            ) from exc
        for key, value in (
            ("domain_boost", self._domain_boost),
            ("topic_boost", self._topic_boost),
        ):
            if not 0.0 <= value <= 1.0:
                raise ConfigError(f"PreferenceScorer {key!r} must be within 0..1.")

    def _clean_list(self, raw: Any, key: str, warnings: list[str]) -> list[str]:
        """Return usable string entries; warn once per malformed value."""
        if raw is None:
            return []
        if not isinstance(raw, list):
            warnings.append(f"{key} must be a list of strings; ignoring.")
            return []
        usable: list[str] = []
        bad = 0
        for entry in raw:
            if isinstance(entry, str) and entry.strip():
                usable.append(entry)
            else:
                bad += 1
        if bad:
            warnings.append(f"{key} ignored {bad} non-string entr(y/ies).")
        return usable

    def run(self, context: SearchContext) -> SearchContext:
        filters = context.request.filters or {}
        if filters.get("personalize") is False:
            for result in context.results:
                score = _set_signal(result, "preference", NEUTRAL)
                result.reasons.append(
                    f"personalization off; preference neutral {score:.2f}"
                )
            return context
        warnings: list[str] = []
        domains = [
            extract_domain(d)
            for d in self._clean_list(
                filters.get("prefer_domains"), "prefer_domains", warnings
            )
        ]
        domains = [d for d in domains if d]
        topics = self._clean_list(
            filters.get("prefer_topics"), "prefer_topics", warnings
        )
        topic_token_sets = [_tokens(t) for t in topics]
        topic_token_sets = [s for s in topic_token_sets if s]
        if warnings:
            context.metadata["preference_warnings"] = list(warnings)

        if not domains and not topic_token_sets:
            for result in context.results:
                score = _set_signal(result, "preference", NEUTRAL)
                result.reasons.append(
                    f"no preferences requested; preference neutral {score:.2f}"
                )
            return context

        for result in context.results:
            result_domain = extract_domain(result.url or "")
            domain_hit = any(_matches_domain(result_domain, pref) for pref in domains)
            haystack = _tokens(f"{result.title} {result.snippet}")
            topic_hit = any(bool(wanted & haystack) for wanted in topic_token_sets)
            boost = (self._domain_boost if domain_hit else 0.0) + (
                self._topic_boost if topic_hit else 0.0
            )
            score = _set_signal(result, "preference", boost)
            parts = []
            if domain_hit:
                parts.append("domain match")
            if topic_hit:
                parts.append("topic match")
            detail = " + ".join(parts) if parts else "no preference match"
            result.reasons.append(f"preference {score:.2f} ({detail})")
        return context

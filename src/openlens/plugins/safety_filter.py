"""Mandatory exclusion filter: removes blocklisted / unsafe hits.

Fail-closed: any internal failure raises ``PluginError(mandatory=True)`` so
the pipeline aborts instead of leaking unfiltered results.
"""

from __future__ import annotations

from typing import Any

from openlens.core.context import SearchContext
from openlens.core.interfaces import (
    ConfigError,
    PipelineStage,
    PluginError,
    PluginType,
)
from openlens.plugins.base import BasePlugin

DEFAULT_BLOCKED_TERMS: tuple[str, ...] = (
    "explicit",
    "porn",
    "gore",
    "malware",
    "phishing",
)


class SafetyFilter(BasePlugin):
    """Exclusion filter over title/snippet/url. Mandatory (fail closed)."""

    @property
    def name(self) -> str:
        return "safety-filter"

    @property
    def plugin_type(self) -> PluginType:
        return PluginType.FILTER

    @property
    def stage(self) -> PipelineStage:
        return PipelineStage.FILTERING

    @property
    def mandatory(self) -> bool:
        return True

    def configure(self, config: dict[str, Any]) -> None:
        super().configure(config)
        terms = self._config.get("blocked_terms", list(DEFAULT_BLOCKED_TERMS))
        domains = self._config.get("blocked_domains", [])
        if not isinstance(terms, list) or not all(isinstance(t, str) for t in terms):
            raise ConfigError("SafetyFilter 'blocked_terms' must be a list of strings.")
        if not isinstance(domains, list) or not all(
            isinstance(d, str) for d in domains
        ):
            raise ConfigError(
                "SafetyFilter 'blocked_domains' must be a list of strings."
            )
        self._blocked_terms = [t.lower() for t in terms]
        self._blocked_domains = [d.lower() for d in domains]
        self._simulate_outage = bool(self._config.get("simulate_outage", False))

    def _is_blocked(self, title: str, snippet: str, url: str) -> bool:
        haystack = f"{title}\n{snippet}\n{url}".lower()
        if any(term in haystack for term in self._blocked_terms):
            return True
        return any(domain in url.lower() for domain in self._blocked_domains)

    def run(self, context: SearchContext) -> SearchContext:
        if self._simulate_outage:
            raise PluginError(
                "safety-filter backend unavailable",
                plugin_name=self.name,
                stage=self.stage,
                mandatory=True,
            )
        kept = []
        removed = 0
        for result in context.results:
            if self._is_blocked(result.title, result.snippet, result.url or ""):
                removed += 1
                continue
            kept.append(result)
        context.results = kept
        context.applied_filters.append(self.name)
        context.metadata["safety-filter-removed"] = removed
        return context

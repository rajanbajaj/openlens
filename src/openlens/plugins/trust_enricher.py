"""Optional trust enricher: annotates hits from unverified sources.

Never filters. Failures are isolated by the pipeline (optional plugin), so a
trust-backend outage degrades annotations, never results.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from openlens.core.context import SearchContext
from openlens.core.interfaces import (
    ConfigError,
    PipelineStage,
    PluginError,
    PluginType,
)
from openlens.plugins.base import BasePlugin


def _domain_of(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:  # noqa: BLE001 — malformed URLs are simply untrusted
        return ""


class TrustEnricher(BasePlugin):
    """Adds warnings for results whose domain is not in the trusted list."""

    @property
    def name(self) -> str:
        return "trust-enricher"

    @property
    def plugin_type(self) -> PluginType:
        return PluginType.ENRICHER

    @property
    def stage(self) -> PipelineStage:
        return PipelineStage.ENRICHMENT

    @property
    def mandatory(self) -> bool:
        return False

    def configure(self, config: dict[str, Any]) -> None:
        super().configure(config)
        trusted = self._config.get("trusted_domains", [])
        if not isinstance(trusted, list) or not all(
            isinstance(d, str) for d in trusted
        ):
            raise ConfigError(
                "TrustEnricher 'trusted_domains' must be a list of strings."
            )
        self._trusted_domains = [d.lower() for d in trusted]
        self._simulate_failure = bool(self._config.get("simulate_failure", False))

    def run(self, context: SearchContext) -> SearchContext:
        if self._simulate_failure:
            raise PluginError(
                "trust backend unavailable",
                plugin_name=self.name,
                stage=self.stage,
                mandatory=False,
            )
        for result in context.results:
            domain = _domain_of(result.url or "")
            trusted = bool(domain) and domain in self._trusted_domains
            result.metadata["trust_domain"] = domain
            result.metadata["trusted"] = trusted
            if result.url and not trusted:
                result.warnings.append(
                    f"Unverified source '{domain or result.url}': "
                    "treat claims with caution."
                )
        return context

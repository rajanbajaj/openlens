"""Tier-aware trust enricher: reputation annotations for search hits.

Never filters. Failures are isolated by the pipeline (optional plugin), so a
trust-backend outage degrades annotations, never results.

The enricher owns warnings: the trust scorer emits only the numeric
``trust`` signal and never warns, so running both never double-warns.
"""

from __future__ import annotations

from typing import Any

from openlens.core.context import SearchContext
from openlens.core.interfaces import (
    PipelineStage,
    PluginError,
    PluginType,
)
from openlens.plugins.base import BasePlugin
from openlens.plugins._signals import extract_domain
from openlens.plugins.trust_scorer import (
    NEW_DOMAIN_DAYS,
    is_https_url,
    is_typosquat,
    resolve_reputation,
)


class TrustEnricher(BasePlugin):
    """Adds reputation annotations for results from unverified sources."""

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
        trusted, distrusted = resolve_reputation(self._config)
        self._trusted_domains = trusted
        self._distrusted_domains = distrusted
        tiers = {domain: "trusted" for domain in trusted}
        tiers.update({domain: "distrusted" for domain in distrusted})
        self._tiers = tiers
        self._simulate_failure = bool(self._config.get("simulate_failure", False))

    def run(self, context: SearchContext) -> SearchContext:
        if self._simulate_failure:
            raise PluginError(
                "trust backend unavailable",
                plugin_name=self.name,
                stage=self.stage,
                mandatory=False,
            )
        trusted_only = frozenset(
            name for name, kind in self._tiers.items() if kind == "trusted"
        )
        for result in context.results:
            url = result.url
            domain = extract_domain(url or "")
            tier = self._tiers.get(domain, "unknown") if domain else "unknown"
            result.metadata["trust_domain"] = domain
            result.metadata["trust_tier"] = tier
            result.metadata["trusted"] = tier == "trusted"
            if not url:
                continue
            chain = result.metadata.get("redirect_chain")
            if isinstance(chain, list) and chain:
                hops = [str(hop) for hop in chain if isinstance(hop, str) and hop]
                if hops:
                    result.metadata["redirect_hops"] = len(hops)
                    final_host = extract_domain(url or "")
                    first_host = extract_domain(hops[0])
                    if first_host and final_host and first_host != final_host:
                        warning = (
                            f"Redirected across domains "
                            f"('{first_host}' -> '{final_host}' "
                            f"in {len(hops)} hop(s)): verify the final site."
                        )
                        result.warnings.append(warning)
                        result.reasons.append(
                            f"cross-domain redirect chain ({len(hops)} hops)"
                        )
                    else:
                        result.reasons.append(
                            f"same-domain redirect chain ({len(hops)} hops)"
                        )
            final_url = result.metadata.get("final_url")
            if isinstance(final_url, str) and final_url and final_url != url:
                result.reasons.append(f"final URL differs after redirects: {final_url}")
            age = result.metadata.get("domain_age_days")
            if isinstance(age, bool):
                age = None
            if isinstance(age, float) and age.is_integer():
                age = int(age)
            if isinstance(age, int) and 0 <= age < NEW_DOMAIN_DAYS:
                warning = (
                    f"Newly registered domain '{domain}' "
                    f"({age}d old): treat with caution."
                )
                result.warnings.append(warning)
                result.reasons.append(f"newly registered domain ({age}d old)")
            if domain:
                if "xn--" in domain:
                    result.warnings.append(
                        f"Punycode domain '{domain}': possible lookalike."
                    )
                    result.reasons.append("punycode domain; possible lookalike")
                else:
                    lookalike = is_typosquat(domain, trusted_only)
                    if lookalike is not None:
                        result.warnings.append(
                            f"Domain '{domain}' resembles trusted "
                            f"'{lookalike}': possible impersonation."
                        )
                        result.reasons.append(f"possible typosquat of '{lookalike}'")
            secure = is_https_url(url)
            if tier == "distrusted":
                warning = f"Distrusted domain '{domain}': treat claims with caution."
                if not secure:
                    warning += " Connection is not HTTPS."
                result.warnings.append(warning)
                result.reasons.append(
                    f"distrusted domain '{domain}'" + ("" if secure else "; no HTTPS")
                )
            elif tier == "trusted":
                if not secure:
                    result.warnings.append(
                        f"Trusted domain '{domain}' served without HTTPS: "
                        "treat claims with caution."
                    )
                    result.reasons.append(f"trusted domain '{domain}'; no HTTPS")
            else:
                warning = (
                    f"Unverified source '{domain or url}': treat claims with caution."
                )
                if not secure:
                    warning += " No HTTPS."
                result.warnings.append(warning)
                result.reasons.append(
                    f"unverified source '{domain or url}'"
                    + ("" if secure else "; no HTTPS")
                )
        return context

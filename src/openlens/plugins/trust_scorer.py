"""Trust-signal scorer (SCORING stage, optional).

Emits a deterministic ``trust`` signal in ``0..1`` into
``result.metadata["signals"]`` from operator-configured reputation tiers.
The scorer never warns (the trust enricher owns warnings), never filters,
and never touches ``final_score``.

Shared signal helpers (namespace, clamping, domain parsing) are reused
from ``plugins/_signals.py``; only reputation-tier parsing lives here.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from openlens.core.context import SearchContext
from openlens.core.interfaces import (
    ConfigError,
    PipelineStage,
    PluginType,
)
from openlens.plugins._signals import (
    NEUTRAL_SIGNAL,
    clamp_signal,
    extract_domain,
    set_signal,
)
from openlens.plugins.base import BasePlugin

#: Signal key written into ``result.metadata["signals"]``.
SIGNAL_NAME = "trust"

#: Tier base scores before bonuses.
TRUSTED_BASE = 0.8
DISTRUSTED_BASE = 0.2

#: Bounded bonuses; every write is clamped to 0..1.
HTTPS_BONUS = 0.05
DOMAIN_AGE_BONUS = 0.05


def is_https_url(url: str | None) -> bool:
    """True only when ``url`` carries an ``https`` scheme."""
    if not url:
        return False
    try:
        return urlparse(url).scheme.lower() == "https"
    except Exception:  # noqa: BLE001 — malformed URLs are never secure
        return False


def _as_domain_list(value: Any, *, key: str) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(entry, str) for entry in value
    ):
        raise ConfigError(f"Reputation tier '{key}' must be a list of strings.")
    return [entry.lower() for entry in value]


def parse_reputation_tiers(
    config: dict[str, Any],
) -> tuple[frozenset[str], frozenset[str]]:
    """Parse ``reputation: {trusted: [...], distrusted: [...]}`` config.

    Returns ``(trusted, distrusted)`` as frozensets for O(1) lookup.
    A domain in both tiers is a ConfigError.
    """
    reputation = config.get("reputation", {})
    if not isinstance(reputation, dict):
        raise ConfigError("Reputation config 'reputation' must be a mapping.")
    trusted = _as_domain_list(reputation.get("trusted", []), key="reputation.trusted")
    distrusted = _as_domain_list(
        reputation.get("distrusted", []), key="reputation.distrusted"
    )
    overlap = set(trusted) & set(distrusted)
    if overlap:
        raise ConfigError(
            f"Domains listed in both reputation tiers: {sorted(overlap)}."
        )
    return frozenset(trusted), frozenset(distrusted)


def resolve_reputation(
    config: dict[str, Any],
) -> tuple[frozenset[str], frozenset[str]]:
    """Parse ``reputation`` tiers plus legacy ``trusted_domains``.

    The legacy ``trusted_domains`` list (when present) merges into the
    ``trusted`` tier so old configs keep working; a non-list value or an
    overlap with the ``distrusted`` tier is a ConfigError.
    """
    trusted, distrusted = parse_reputation_tiers(config)
    if "trusted_domains" not in config:
        return trusted, distrusted
    legacy = config.get("trusted_domains", [])
    if not isinstance(legacy, list) or not all(
        isinstance(entry, str) for entry in legacy
    ):
        raise ConfigError("'trusted_domains' must be a list of strings.")
    merged = set(trusted) | {entry.lower() for entry in legacy}
    overlap = merged & set(distrusted)
    if overlap:
        raise ConfigError(
            f"Domains listed in both reputation tiers: {sorted(overlap)}."
        )
    return frozenset(merged), distrusted


class TrustScorer(BasePlugin):
    """Deterministic trust-signal scorer; owns the signal, never warns."""

    @property
    def name(self) -> str:
        return "trust-scorer"

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
        trusted, distrusted = resolve_reputation(self._config)
        tiers = {domain: "trusted" for domain in trusted}
        tiers.update({domain: "distrusted" for domain in distrusted})
        self._tiers = tiers

    def score_result(
        self, url: str | None, metadata: dict[str, Any]
    ) -> tuple[float, str]:
        """Return the ``(signal, reason)`` pair for one hit; no side effects."""
        domain = extract_domain(url or "")
        if not domain:
            return NEUTRAL_SIGNAL, "trust: no URL/domain metadata; neutral 0.5"
        if domain in self._tiers:
            tier = self._tiers[domain]
            value = TRUSTED_BASE if tier == "trusted" else DISTRUSTED_BASE
        else:
            tier = "unknown"
            value = NEUTRAL_SIGNAL
        notes = [f"{tier} domain '{domain}'"]
        if is_https_url(url):
            value += HTTPS_BONUS
            notes.append("https")
        age = metadata.get("domain_age_days")
        if isinstance(age, int) and not isinstance(age, bool) and age >= 0:
            value += DOMAIN_AGE_BONUS
            notes.append(f"domain age {age}d")
        value = clamp_signal(value)
        return value, f"trust: {'; '.join(notes)} -> {value:.2f}"

    def run(self, context: SearchContext) -> SearchContext:
        for result in context.results:
            value, reason = self.score_result(result.url, result.metadata)
            set_signal(result, SIGNAL_NAME, value)
            result.reasons.append(reason)
        return context

"""Per-mode ranking profiles: named signal-weight bundles.

Modes come from ``SearchRequest.mode``. Unknown modes raise ``ConfigError``
loudly at request time so a typo never silently serves misranked results.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openlens.config import load_config
from openlens.core.interfaces import ConfigError

#: Hard cap on per-profile keyword counts (startup validation). The bonded
#: federated cap keeps per-request scans bounded; above this the config is
#: rejected loudly instead of silently slowing every request.
MAX_KEYWORDS = 50


@dataclass(frozen=True)
class RankingProfile:
    """How to combine ranking signals for one search mode."""

    name: str
    description: str = ""
    relevance_weight: float = 1.0
    warning_penalty: float = 0.5
    trust_boost: float = 0.25
    recency_boost: float = 0.0
    keyword_boost: float = 0.0
    keywords: tuple[str, ...] = field(default_factory=tuple)
    trust_weight: float = 0.0
    freshness_weight: float = 0.0
    preference_weight: float = 0.0
    diversity_weight: float = 0.0
    perspective_weight: float = 0.0
    commerce_weight: float = 0.0
    safety_weight: float = 0.0
    privacy_weight: float = 0.0
    fairness_weight: float = 0.0
    news_trust_weight: float = 0.0
    sponsored_weight: float = 0.0


BUILTIN_PROFILES: dict[str, RankingProfile] = {
    "default": RankingProfile(
        name="default",
        description="Baseline: provider-informed relevance, mild warning penalty.",
        trust_weight=0.5,
        freshness_weight=0.25,
        preference_weight=1.0,
        diversity_weight=0.25,
        safety_weight=0.5,
        privacy_weight=0.5,
        fairness_weight=0.25,
        sponsored_weight=0.5,
    ),
    "child-safe": RankingProfile(
        name="child-safe",
        description="Warnings demote heavily; trusted sources boosted.",
        warning_penalty=10.0,
        trust_boost=0.5,
        trust_weight=1.0,
        freshness_weight=0.0,
        preference_weight=0.0,
        diversity_weight=0.25,
        safety_weight=1.5,
        privacy_weight=1.0,
        fairness_weight=1.0,
        sponsored_weight=1.0,
    ),
    "news": RankingProfile(
        name="news",
        description="Boosts recent hits via metadata recency_score (0..1).",
        recency_boost=2.0,
        trust_weight=0.5,
        freshness_weight=2.0,
        preference_weight=0.0,
        diversity_weight=0.25,
        perspective_weight=1.0,
        safety_weight=0.5,
        privacy_weight=0.25,
        fairness_weight=0.5,
        news_trust_weight=1.5,
        sponsored_weight=1.0,
    ),
    "shopping": RankingProfile(
        name="shopping",
        description="Boosts hits mentioning purchase-oriented keywords.",
        keyword_boost=0.5,
        keywords=("buy", "price", "deal", "review", "shop"),
        trust_weight=0.25,
        freshness_weight=0.0,
        preference_weight=1.0,
        diversity_weight=0.25,
        commerce_weight=1.5,
        safety_weight=0.5,
        privacy_weight=0.25,
        fairness_weight=0.25,
        sponsored_weight=0.25,
    ),
    "travel": RankingProfile(
        name="travel",
        description="Boosts hits mentioning travel-oriented keywords.",
        keyword_boost=0.5,
        keywords=("flight", "hotel", "guide", "itinerary", "trip"),
        trust_weight=0.25,
        freshness_weight=0.0,
        preference_weight=1.0,
        diversity_weight=0.25,
        commerce_weight=1.5,
        safety_weight=0.5,
        privacy_weight=0.25,
        fairness_weight=0.25,
        sponsored_weight=0.25,
    ),
}


def list_profiles() -> list[str]:
    """Names of all built-in ranking profiles."""
    return sorted(BUILTIN_PROFILES)


def get_profile(name: str) -> RankingProfile:
    """Return the profile for ``name``. Raises ConfigError when unknown."""
    try:
        return BUILTIN_PROFILES[name]
    except KeyError:
        raise ConfigError(
            f"Unknown ranking profile '{name}'. Known: {', '.join(list_profiles())}."
        ) from None


def _signal_weight(data: dict[str, Any], key: str, profile_name: str) -> float:
    """Read a signal weight; non-numeric or non-finite values fail loudly."""
    try:
        value = float(data.get(key, 0.0))
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            f"Ranking profile '{profile_name}' has invalid values: "
            f"'{key}' must be a number ({exc})"
        ) from exc
    if not math.isfinite(value):
        raise ConfigError(
            f"Ranking profile '{profile_name}' has invalid values: "
            f"'{key}' must be finite."
        )
    return value


def _profile_keywords(data: dict[str, Any], profile_name: str) -> tuple[str, ...]:
    """Validate and normalize the ``keywords`` entry (pre-lowered tuple)."""
    raw = data.get("keywords", ())
    if isinstance(raw, str):
        raise ConfigError(
            f"Ranking profile '{profile_name}' has invalid values: "
            "'keywords' must be a list of strings, got a string."
        )
    if not isinstance(raw, (list, tuple)):
        raise ConfigError(
            f"Ranking profile '{profile_name}' has invalid values: "
            "'keywords' must be a list of strings."
        )
    keywords = tuple(raw)
    for entry in keywords:
        if not isinstance(entry, str):
            raise ConfigError(
                f"Ranking profile '{profile_name}' has invalid values: "
                "'keywords' must be a list of strings."
            )
    if len(keywords) > MAX_KEYWORDS:
        raise ConfigError(
            f"Ranking profile '{profile_name}' has invalid values: "
            f"'keywords' holds {len(keywords)} entries, cap is {MAX_KEYWORDS}."
        )
    return tuple(entry.lower() for entry in keywords)


def profile_from_dict(name: str, data: dict[str, Any]) -> RankingProfile:
    """Build a profile from a config mapping. Raises ConfigError if invalid."""
    if not isinstance(data, dict):
        raise ConfigError(f"Ranking profile '{name}' must be a mapping.")
    known = {
        "description",
        "relevance_weight",
        "warning_penalty",
        "trust_boost",
        "recency_boost",
        "keyword_boost",
        "keywords",
        "trust_weight",
        "freshness_weight",
        "preference_weight",
        "diversity_weight",
        "perspective_weight",
        "commerce_weight",
        "safety_weight",
        "privacy_weight",
        "fairness_weight",
        "news_trust_weight",
        "sponsored_weight",
    }
    unknown = set(data) - known
    if unknown:
        raise ConfigError(
            f"Ranking profile '{name}' has unknown keys: {sorted(unknown)}."
        )
    try:
        return RankingProfile(
            name=name,
            description=str(data.get("description", "")),
            relevance_weight=float(data.get("relevance_weight", 1.0)),
            warning_penalty=float(data.get("warning_penalty", 0.5)),
            trust_boost=float(data.get("trust_boost", 0.25)),
            recency_boost=float(data.get("recency_boost", 0.0)),
            keyword_boost=float(data.get("keyword_boost", 0.0)),
            keywords=_profile_keywords(data, name),
            trust_weight=_signal_weight(data, "trust_weight", name),
            freshness_weight=_signal_weight(data, "freshness_weight", name),
            preference_weight=_signal_weight(data, "preference_weight", name),
            diversity_weight=_signal_weight(data, "diversity_weight", name),
            perspective_weight=_signal_weight(data, "perspective_weight", name),
            commerce_weight=_signal_weight(data, "commerce_weight", name),
            safety_weight=_signal_weight(data, "safety_weight", name),
            privacy_weight=_signal_weight(data, "privacy_weight", name),
            fairness_weight=_signal_weight(data, "fairness_weight", name),
            news_trust_weight=_signal_weight(data, "news_trust_weight", name),
            sponsored_weight=_signal_weight(data, "sponsored_weight", name),
        )
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            f"Ranking profile '{name}' has invalid values: {exc}"
        ) from exc


def load_profiles(path: str | Path) -> dict[str, RankingProfile]:
    """Load named profiles from a YAML file (``profiles: {name: {...}}``)."""
    data = load_config(path)
    raw = data.get("profiles", {})
    if not isinstance(raw, dict):
        raise ConfigError("Ranking config 'profiles' must be a mapping.")
    return {name: profile_from_dict(name, entry or {}) for name, entry in raw.items()}

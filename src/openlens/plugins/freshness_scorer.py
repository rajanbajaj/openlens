"""Freshness scorer: exponential recency decay over publication dates.

Optional SCORING-stage plugin. Reads the first present of
``metadata["published_at"]``, ``metadata["published"]``, ``metadata["date"]``
(ISO-8601 including ``Z``, or epoch seconds as int/float), scores with
exponential decay against a configurable half-life, and writes the
``freshness`` signal plus a ``recency_score`` backfill (the key the ranker
already reads) with the identical value.

Absent or unparseable dates degrade to neutral 0.5 with a reason, never 0.
At most one concise reason per result; single O(R) pass, no I/O.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from openlens.core.context import SearchContext
from openlens.core.interfaces import (
    ConfigError,
    PipelineStage,
    PluginType,
)
from openlens.plugins._signals import (
    DATE_KEYS,
    NEUTRAL_SIGNAL as NEUTRAL,
    parse_date,
    set_signal as _set_signal,
)
from openlens.plugins.base import BasePlugin

_DEFAULT_HALF_LIFE_DAYS = 30.0


def decay_score(age_days: float, half_life_days: float) -> float:
    """Exponential decay: 1.0 at age 0, halving every ``half_life_days``."""
    if age_days <= 0:
        return 1.0
    return float(0.5 ** (age_days / half_life_days))


class FreshnessScorer(BasePlugin):
    """Scores recency with exponential decay; backfills ``recency_score``."""

    @property
    def name(self) -> str:
        return "freshness-scorer"

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
            half_life = float(
                self._config.get("half_life_days", _DEFAULT_HALF_LIFE_DAYS)
            )
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"FreshnessScorer 'half_life_days' must be a number: {exc}"
            ) from exc
        if not math.isfinite(half_life) or half_life <= 0:
            raise ConfigError("FreshnessScorer 'half_life_days' must be > 0.")
        self._half_life_days = half_life

    def _now(self) -> datetime:
        override = self._config.get("now")
        parsed = parse_date(override) if override is not None else None
        if parsed is not None:
            return parsed
        return datetime.now(timezone.utc)

    def run(self, context: SearchContext) -> SearchContext:
        now = self._now()
        for result in context.results:
            raw = None
            used_key = None
            for key in DATE_KEYS:
                if key in result.metadata:
                    raw = result.metadata[key]
                    used_key = key
                    break
            published = parse_date(raw) if used_key is not None else None
            if published is None:
                if used_key is None:
                    reason = "no publication date; freshness neutral 0.5"
                else:
                    reason = f"unparseable {used_key!r}; freshness neutral 0.5"
                score = _set_signal(result, "freshness", NEUTRAL)
            else:
                age_days = (now - published).total_seconds() / 86400.0
                score = _set_signal(
                    result, "freshness", decay_score(age_days, self._half_life_days)
                )
                reason = (
                    f"published {age_days:.1f}d ago ({used_key}); freshness {score:.2f}"
                )
            result.metadata["recency_score"] = score
            result.reasons.append(reason)
        return context

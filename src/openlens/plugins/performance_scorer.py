"""Performance-signal scorer (SCORING stage, optional).

Emits a deterministic ``performance`` signal in ``0..1`` into
``result.metadata["signals"]`` from the indexed fetch latency
(``metadata["fetch_ms"]``: wall-clock fetch duration in milliseconds,
measured by the crawler). Fast pages score near 1.0, slow pages decay
toward 0.0 on a log scale; pages without latency metadata stay
neutral. The scorer never warns, never filters, and never touches
``final_score``.
"""

from __future__ import annotations

import math

from typing import Any

from openlens.core.context import SearchContext
from openlens.core.interfaces import (
    ConfigError,
    PipelineStage,
    PluginType,
)
from openlens.plugins._signals import (
    NEUTRAL_SIGNAL,
    clamp_signal,
    set_signal,
)
from openlens.plugins.base import BasePlugin

#: Signal key written into ``result.metadata["signals"]``.
SIGNAL_NAME = "performance"

#: Fetch latency (ms) scoring a perfect 1.0.
DEFAULT_FAST_MS = 300.0

#: Fetch latency (ms) decaying to ~0.0 (clamped floor).
DEFAULT_SLOW_MS = 8000.0


def performance_score(
    fetch_ms: Any,
    fast_ms: float = DEFAULT_FAST_MS,
    slow_ms: float = DEFAULT_SLOW_MS,
) -> float:
    """Log-decay performance curve over fetch latency; neutral on garbage.

    ``fetch_ms <= fast_ms`` -> 1.0; ``>= slow_ms`` -> 0.0; between, a
    log interpolation so a 3s page is penalized modestly, not linearly.
    Non-numeric or negative latencies are not evidence; neutral. Pure;
    never raises.
    """
    if isinstance(fetch_ms, bool):
        return NEUTRAL_SIGNAL
    try:
        number = float(fetch_ms)
    except (TypeError, ValueError):
        return NEUTRAL_SIGNAL
    if number != number or number < 0:  # NaN or negative: no evidence
        return NEUTRAL_SIGNAL
    if fast_ms <= 0:
        fast_ms = DEFAULT_FAST_MS
    if slow_ms <= fast_ms:
        slow_ms = DEFAULT_SLOW_MS
    if number <= fast_ms:
        return 1.0
    if number >= slow_ms:
        return 0.0
    span = math.log(slow_ms) - math.log(fast_ms)
    if span <= 0:
        return NEUTRAL_SIGNAL
    ratio = (math.log(number) - math.log(fast_ms)) / span
    return clamp_signal(1.0 - ratio)


class PerformanceScorer(BasePlugin):
    """Deterministic performance-signal scorer; owns the signal, never warns."""

    @property
    def name(self) -> str:
        return "performance-scorer"

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
            fast_ms = float(self._config.get("fast_ms", DEFAULT_FAST_MS))
            slow_ms = float(self._config.get("slow_ms", DEFAULT_SLOW_MS))
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"Performance bounds must be numbers: {exc}") from exc
        if fast_ms <= 0 or slow_ms <= fast_ms:
            raise ConfigError("Performance requires 0 < fast_ms < slow_ms.")
        self._fast_ms = fast_ms
        self._slow_ms = slow_ms

    def score_result(self, metadata: dict[str, Any]) -> tuple[float, str]:
        """Return the ``(signal, reason)`` pair for one hit; no side effects."""
        fetch_ms = (metadata or {}).get("fetch_ms")
        if fetch_ms is None:
            return NEUTRAL_SIGNAL, "performance: no fetch latency; neutral 0.5"
        value = performance_score(fetch_ms, self._fast_ms, self._slow_ms)
        return value, f"performance: {fetch_ms}ms fetch -> {value:.2f}"

    def run(self, context: SearchContext) -> SearchContext:
        for result in context.results:
            value, reason = self.score_result(result.metadata)
            set_signal(result, SIGNAL_NAME, value)
            result.reasons.append(reason)
        return context

"""Authority-signal scorer (SCORING stage, optional).

Emits a deterministic ``authority`` signal in ``0..1`` into
``result.metadata["signals"]`` from the indexed inlink count
(``metadata["inlink_count"]``: distinct linking hosts, stamped by the
provider from the ``document_links`` graph at search time). The curve
``n / (n + half)`` saturates so mega-linked pages cannot dwarf
everything else; pages without count metadata stay neutral. The scorer
never warns, never filters, and never touches ``final_score``.
"""

from __future__ import annotations

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
SIGNAL_NAME = "authority"

#: Inlink count that yields a 0.5 signal (saturation half-point).
DEFAULT_HALF_SATURATION = 10.0


def authority_score(count: Any, half: float = DEFAULT_HALF_SATURATION) -> float:
    """Saturating authority curve over an inlink count; neutral on garbage.

    ``count / (count + half)``: 0 -> 0.0, ``half`` -> 0.5, and large
    counts asymptote to 1.0. Non-numeric or negative counts are not
    evidence of authority; they degrade to neutral. Pure; never raises.
    """
    if isinstance(count, bool):
        return NEUTRAL_SIGNAL
    try:
        number = float(count)
    except (TypeError, ValueError):
        return NEUTRAL_SIGNAL
    if number != number or number < 0:  # NaN or negative: no evidence
        return NEUTRAL_SIGNAL
    if half <= 0:
        half = DEFAULT_HALF_SATURATION
    return clamp_signal(number / (number + half))


class AuthorityScorer(BasePlugin):
    """Deterministic authority-signal scorer; owns the signal, never warns."""

    @property
    def name(self) -> str:
        return "authority-scorer"

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
            half = float(self._config.get("half_saturation", DEFAULT_HALF_SATURATION))
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"Authority half_saturation must be a number: {exc}"
            ) from exc
        if half <= 0:
            raise ConfigError("Authority half_saturation must be positive.")
        self._half = half

    def score_result(self, metadata: dict[str, Any]) -> tuple[float, str]:
        """Return the ``(signal, reason)`` pair for one hit; no side effects."""
        count = (metadata or {}).get("inlink_count")
        if count is None:
            return NEUTRAL_SIGNAL, "authority: no inlink data; neutral 0.5"
        value = authority_score(count, self._half)
        return value, f"authority: {count} linking hosts -> {value:.2f}"

    def run(self, context: SearchContext) -> SearchContext:
        for result in context.results:
            value, reason = self.score_result(result.metadata)
            set_signal(result, SIGNAL_NAME, value)
            result.reasons.append(reason)
        return context

"""AI summary enricher stub: attaches offline model summaries to results.

Optional enrichment plugin wired through the :class:`AIModelProvider`
interface — never through a concrete backend. It defaults to
:class:`FakeModelProvider` and accepts any configured backend via its
``model`` config mapping, so it runs fully offline and ships disabled-safe.
"""

from __future__ import annotations

from typing import Any, Optional

from openlens.core.context import SearchContext
from openlens.core.interfaces import (
    ConfigError,
    PipelineStage,
    PluginError,
    PluginType,
)
from openlens.models.fake import FakeModelProvider
from openlens.models.interfaces import AIModelProvider
from openlens.models.registry import create as create_model
from openlens.models.registry import register_builtin_models
from openlens.plugins.base import BasePlugin


class AISummaryPlugin(BasePlugin):
    """Adds an ``ai_summary`` entry to each result's metadata."""

    @property
    def name(self) -> str:
        return "ai-summary"

    @property
    def plugin_type(self) -> PluginType:
        return PluginType.ENRICHER

    @property
    def stage(self) -> PipelineStage:
        return PipelineStage.ENRICHMENT

    @property
    def mandatory(self) -> bool:
        return False

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        model: Optional[AIModelProvider] = None,
    ) -> None:
        self._model: AIModelProvider = model or FakeModelProvider()
        self._max_chars: int = 500
        self._simulate_failure = False
        super().__init__(config)

    def configure(self, config: dict[str, Any]) -> None:
        super().configure(config)
        max_chars = self._config.get("max_chars", 500)
        if isinstance(max_chars, bool) or not isinstance(max_chars, int):
            raise ConfigError("AISummaryPlugin 'max_chars' must be an int.")
        if not 1 <= max_chars <= 10000:
            raise ConfigError("AISummaryPlugin 'max_chars' must be within [1, 10000].")
        self._max_chars = max_chars
        self._simulate_failure = bool(self._config.get("simulate_failure", False))
        model_config = self._config.get("model")
        if model_config is None:
            return
        if not isinstance(model_config, dict):
            raise ConfigError("AISummaryPlugin 'model' must be a backend mapping.")
        model_type = model_config.get("type", "fake")
        if not isinstance(model_type, str) or not model_type:
            raise ConfigError("AISummaryPlugin 'model.type' must be a non-empty string.")
        options = {k: v for k, v in model_config.items() if k != "type"}
        register_builtin_models()
        self._model = create_model(model_type, options)

    @property
    def model(self) -> AIModelProvider:
        """The model backend this plugin calls through (injectable for tests)."""
        return self._model

    def run(self, context: SearchContext) -> SearchContext:
        if self._simulate_failure:
            raise PluginError(
                "ai backend unavailable",
                plugin_name=self.name,
                stage=self.stage,
                mandatory=False,
            )
        for result in context.results:
            source = f"{result.title} {result.snippet}"[: self._max_chars]
            try:
                summary = self._model.complete(f"Summarize: {source}")
            except Exception as exc:
                raise PluginError(
                    f"ai backend failed: {exc}",
                    plugin_name=self.name,
                    stage=self.stage,
                    mandatory=False,
                ) from exc
            result.metadata["ai_summary"] = summary
            result.metadata["ai_model"] = self._model.name
        return context

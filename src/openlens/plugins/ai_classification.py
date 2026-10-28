"""AI safety/privacy classification: advisory labels only.

FILTERING-stage, optional (``mandatory=False``) plugin that writes model
labels into ``result.metadata`` / ``result.warnings``. It NEVER excludes:
it does not add, remove, reorder, or reassign ``context.results``, and it
never touches scores. The mandatory safety filter still excludes
alone — this plugin does not import or call it; the seam is data-only
(labels in metadata).

Model outage or no backend means "no advisory labels", never "gate
opens": with no injected backend, or when the backend raises, the
plugin leaves results unlabeled and returns the context unchanged in
membership. Classification is capped to the first ``classify_top_k``
results (default 5, falling back to ``enrich_top_k``) so it never fans
out over the full result set.
"""

from __future__ import annotations

from typing import Any, Optional

from openlens.core.context import SearchContext
from openlens.core.interfaces import (
    ConfigError,
    PipelineStage,
    PluginType,
)
from openlens.models.interfaces import AIModelProvider
from openlens.models.registry import create as create_model
from openlens.models.registry import register_builtin_models
from openlens.plugins.base import BasePlugin

#: Default label set offered to the model backend.
DEFAULT_LABELS: tuple[str, ...] = ("safe", "unsafe", "spam", "misleading")

#: Default cap on results classified per request.
DEFAULT_CLASSIFY_TOP_K = 5

#: Hard ceiling for the classification cap.
MAX_CLASSIFY_TOP_K = 20


class AIClassificationPlugin(BasePlugin):
    """Attaches advisory ``ai_classification`` labels to results."""

    @property
    def name(self) -> str:
        return "ai-classification"

    @property
    def plugin_type(self) -> PluginType:
        return PluginType.FILTER

    @property
    def stage(self) -> PipelineStage:
        return PipelineStage.FILTERING

    @property
    def mandatory(self) -> bool:
        return False

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        model: Optional[AIModelProvider] = None,
    ) -> None:
        self._model: Optional[AIModelProvider] = model
        self._labels: tuple[str, ...] = DEFAULT_LABELS
        self._classify_top_k: int = DEFAULT_CLASSIFY_TOP_K
        self._max_chars: int = 500
        super().__init__(config)

    def configure(self, config: dict[str, Any]) -> None:
        super().configure(config)
        labels = self._config.get("labels", list(DEFAULT_LABELS))
        if (
            not isinstance(labels, list)
            or not labels
            or not all(isinstance(item, str) and item for item in labels)
        ):
            raise ConfigError(
                "AIClassificationPlugin 'labels' must be a non-empty "
                "list of non-empty strings."
            )
        self._labels = tuple(labels)
        top_k = self._config.get(
            "classify_top_k", self._config.get("enrich_top_k", DEFAULT_CLASSIFY_TOP_K)
        )
        if isinstance(top_k, bool) or not isinstance(top_k, int):
            raise ConfigError("AIClassificationPlugin 'classify_top_k' must be an int.")
        if not 0 <= top_k <= MAX_CLASSIFY_TOP_K:
            raise ConfigError(
                "AIClassificationPlugin 'classify_top_k' must be within "
                f"[0, {MAX_CLASSIFY_TOP_K}]."
            )
        self._classify_top_k = top_k
        max_chars = self._config.get("max_chars", 500)
        if isinstance(max_chars, bool) or not isinstance(max_chars, int):
            raise ConfigError("AIClassificationPlugin 'max_chars' must be an int.")
        if not 1 <= max_chars <= 10000:
            raise ConfigError(
                "AIClassificationPlugin 'max_chars' must be within [1, 10000]."
            )
        self._max_chars = max_chars
        model_config = self._config.get("model")
        if model_config is None:
            return
        if not isinstance(model_config, dict):
            raise ConfigError(
                "AIClassificationPlugin 'model' must be a backend mapping."
            )
        model_type = model_config.get("type", "fake")
        if not isinstance(model_type, str) or not model_type:
            raise ConfigError(
                "AIClassificationPlugin 'model.type' must be a non-empty string."
            )
        options = {k: v for k, v in model_config.items() if k != "type"}
        register_builtin_models()
        self._model = create_model(model_type, options)

    @property
    def model(self) -> Optional[AIModelProvider]:
        """The model backend this plugin calls through (injectable)."""
        return self._model

    def set_model(self, model: Optional[AIModelProvider]) -> None:
        """Inject the manager-primary backend (manager-primary seam)."""
        self._model = model

    def run(self, context: SearchContext) -> SearchContext:
        # No backend (model disabled / all AI off): no advisory labels.
        if self._model is None or self._classify_top_k == 0:
            return context
        labels = list(self._labels)
        for result in context.results[: self._classify_top_k]:
            text = f"{result.title} {result.snippet} {result.url or ''}"[
                : self._max_chars
            ]
            try:
                label = self._model.classify(text, labels)
            except Exception:  # advisory only: outage adds no labels, never opens gate
                label = None
            if not isinstance(label, str) or not label:
                continue
            result.metadata["ai_classification"] = label
            result.metadata["ai_model"] = self._model.name
            if label != "safe":
                result.warnings.append(
                    f"ai-classification ({self._model.name}): {label}"
                )
        return context

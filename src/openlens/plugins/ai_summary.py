"""AI summary/perspective enricher: attaches offline model enrichments.

Optional enrichment plugin wired through the :class:`AIModelProvider`
interface — never through a concrete backend. It defaults to
:class:`FakeModelProvider` and accepts any configured backend via its
``model`` config mapping, so it runs fully offline and ships disabled-safe.

Enrichment is capped by ``enrich_top_k`` (default 5, validated [0, 20];
``0`` disables enrichment without disabling the plugin). The cap is
applied to ``context.results`` as the plugin sees them — i.e. after
filtering/scoring — so K counts surviving results. Results beyond K
get no ``ai_summary``/``ai_perspectives`` keys at all. Both prompts go
through one shared model-call helper (:meth:`AISummaryPlugin._call_model`)
which enforces the per-call ``timeout_s`` exactly once.
"""

from __future__ import annotations

import threading
from typing import Any, Optional

from openlens.core.context import SearchContext
from openlens.core.interfaces import (
    ConfigError,
    PipelineStage,
    PluginError,
    PluginType,
    ProviderError,
)
from openlens.models.fake import FakeModelProvider
from openlens.models.interfaces import AIModelProvider
from openlens.models.registry import create as create_model
from openlens.models.registry import register_builtin_models
from openlens.plugins.base import BasePlugin

#: Default number of top results to enrich when ``enrich_top_k`` is omitted.
DEFAULT_ENRICH_TOP_K = 5

#: Hard ceiling for ``enrich_top_k`` so a misconfigured plugin stays bounded.
MAX_ENRICH_TOP_K = 20

#: Fallback per-call timeout (seconds) when neither the plugin config nor
#: the backend carries a usable ``timeout_s``.
DEFAULT_TIMEOUT_S = 10.0

#: Hard ceiling for a plugin-level ``timeout_s`` (mirrors model backends).
MAX_TIMEOUT_S = 300.0


class AISummaryPlugin(BasePlugin):
    """Adds ``ai_summary``/``ai_perspectives`` metadata to top-K results.

    Each enriched result also carries ``ai_model`` — the generating
    backend's name — for both fields. Results beyond ``enrich_top_k``
    are left untouched (no AI keys at all).
    """

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
        self._enrich_top_k: int = DEFAULT_ENRICH_TOP_K
        self._timeout_s: float | None = None
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
        enrich_top_k = self._config.get("enrich_top_k", DEFAULT_ENRICH_TOP_K)
        if isinstance(enrich_top_k, bool) or not isinstance(enrich_top_k, int):
            raise ConfigError("AISummaryPlugin 'enrich_top_k' must be an int.")
        if not 0 <= enrich_top_k <= MAX_ENRICH_TOP_K:
            raise ConfigError(
                "AISummaryPlugin 'enrich_top_k' must be within "
                f"[0, {MAX_ENRICH_TOP_K}]."
            )
        self._enrich_top_k = enrich_top_k
        timeout_s = self._config.get("timeout_s")
        if timeout_s is None:
            self._timeout_s = None
        else:
            if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)):
                raise ConfigError("AISummaryPlugin 'timeout_s' must be a number.")
            if not 0 < float(timeout_s) <= MAX_TIMEOUT_S:
                raise ConfigError(
                    f"AISummaryPlugin 'timeout_s' must be within (0, {MAX_TIMEOUT_S}]."
                )
            self._timeout_s = float(timeout_s)
        self._simulate_failure = bool(self._config.get("simulate_failure", False))
        model_config = self._config.get("model")
        if model_config is None:
            return
        if not isinstance(model_config, dict):
            raise ConfigError("AISummaryPlugin 'model' must be a backend mapping.")
        model_type = model_config.get("type", "fake")
        if not isinstance(model_type, str) or not model_type:
            raise ConfigError(
                "AISummaryPlugin 'model.type' must be a non-empty string."
            )
        options = {k: v for k, v in model_config.items() if k != "type"}
        register_builtin_models()
        self._model = create_model(model_type, options)

    @property
    def model(self) -> AIModelProvider:
        """The model backend this plugin calls through (injectable for tests)."""
        return self._model

    def set_model(self, model: AIModelProvider) -> None:
        """Replace the backend (manager-primary injection seam)."""
        self._model = model

    @property
    def enrich_top_k(self) -> int:
        """How many top (surviving) results get AI enrichment."""
        return self._enrich_top_k

    def _effective_timeout_s(self) -> float:
        """Plugin ``timeout_s`` wins; else backend's; else the default."""
        if self._timeout_s is not None:
            return self._timeout_s
        raw = getattr(self._model, "timeout_s", None)
        if (
            isinstance(raw, (int, float))
            and not isinstance(raw, bool)
            and 0 < float(raw) <= MAX_TIMEOUT_S
        ):
            return float(raw)
        return DEFAULT_TIMEOUT_S

    def _call_model(self, prompt: str) -> str:
        """One shared bounded model call for both enrichment prompts.

        Enforces the per-call ``timeout_s`` once, here, via a daemon
        thread (mirroring ``BaseSearchProvider.run_bounded``): an
        overrun raises :class:`ProviderError` with code ``TIMEOUT``
        while the hung worker is abandoned. Backend ``ProviderError``s
        propagate unchanged; other exceptions are wrapped via the
        backend's ``wrap_error`` when it offers one.
        """
        timeout_s = self._effective_timeout_s()
        box: dict[str, Any] = {}

        def target() -> None:
            try:
                box["result"] = self._model.complete(prompt)
            except BaseException as exc:  # captured and re-raised below
                box["error"] = exc

        worker = threading.Thread(target=target, daemon=True)
        worker.start()
        worker.join(timeout=timeout_s)
        if worker.is_alive():
            raise ProviderError.timeout(
                self._model.name,
                f"model backend '{self._model.name}' timed out after {timeout_s}s",
            )
        if "error" in box:
            err = box["error"]
            if isinstance(err, ProviderError):
                if not err.provider_name:
                    err.provider_name = self._model.name
                raise err
            wrap = getattr(self._model, "wrap_error", None)
            if callable(wrap):
                wrapped = wrap(err)
                if isinstance(wrapped, ProviderError):
                    raise wrapped
            raise ProviderError(
                f"{self._model.name} failed: {err}",
                provider_name=self._model.name,
                retryable=False,
                code=ProviderError.UNKNOWN,
            )
        return str(box.get("result", ""))

    def run(self, context: SearchContext) -> SearchContext:
        if self._simulate_failure:
            raise PluginError(
                "ai backend unavailable",
                plugin_name=self.name,
                stage=self.stage,
                mandatory=False,
            )
        if self._enrich_top_k == 0:
            return context
        # One loop over the surviving results; the shared helper serves
        # both the summary and the perspective prompt per result.
        for result in context.results[: self._enrich_top_k]:
            source = f"{result.title} {result.snippet}"[: self._max_chars]
            try:
                summary = self._call_model(f"Summarize: {source}")
                perspectives = self._call_model(f"Perspectives: {source}")
            except Exception as exc:
                raise PluginError(
                    f"ai backend failed: {exc}",
                    plugin_name=self.name,
                    stage=self.stage,
                    mandatory=False,
                ) from exc
            # Assigned only after both calls succeed (per-result atomic),
            # and stored as returned — never re-truncated here.
            result.metadata["ai_summary"] = summary
            result.metadata["ai_perspectives"] = perspectives
            result.metadata["ai_model"] = self._model.name
        return context

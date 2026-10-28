"""Concrete Engine: provider search -> plugin pipeline -> ranking -> response."""

from __future__ import annotations

import time
from typing import Any

from openlens.core.context import SearchContext
from openlens.core.interfaces import (
    ConfigError,
    Engine,
    ProviderError,
    SearchProvider,
    SearchRequest,
    SearchResponse,
)
from openlens.core.pipeline import Pipeline, assemble_explanations
from openlens.core.registry import PluginRegistry
from openlens.ranking.profiles import get_profile
from openlens.ranking.ranker import Ranker


class SearchEngine(Engine):
    """Default Engine implementation.

    Providers are supplied by the caller (the provider manager owns them);
    plugins are built from config via :class:`PluginRegistry`. Ranking uses
    the per-mode profile named by ``SearchRequest.mode``.
    """

    def __init__(
        self,
        providers: list[SearchProvider] | tuple[SearchProvider, ...] = (),
        plugins: list[Any] | tuple[Any, ...] = (),
        ranker: Ranker | None = None,
    ) -> None:
        self._providers = list(providers)
        self._pipeline = Pipeline(list(plugins))
        self._ranker = ranker or Ranker()

    @property
    def pipeline(self) -> Pipeline:
        return self._pipeline

    def configure(self, config: dict[str, Any]) -> None:
        """Apply ``{plugins: [...], ranking: {default_profile}}`` config.

        Raises:
            ConfigError: malformed plugin entries or unknown ranking profile.
        """
        if not isinstance(config, dict):
            raise ConfigError("Engine config must be a mapping.")
        plugin_entries = config.get("plugins", [])
        if not isinstance(plugin_entries, list):
            raise ConfigError("Engine config 'plugins' must be a list.")
        ranking_cfg = config.get("ranking", {})
        if not isinstance(ranking_cfg, dict):
            raise ConfigError("Engine config 'ranking' must be a mapping.")
        default_profile = ranking_cfg.get("default_profile", "default")
        get_profile(default_profile)  # validates the name loudly
        plugins = PluginRegistry().build(plugin_entries)
        self._pipeline = Pipeline(plugins)
        self._ranker = Ranker({"profile": default_profile})

    def _select_provider(self) -> SearchProvider:
        if not self._providers:
            raise ConfigError("No search providers configured.")
        return self._providers[0]

    def _search_with_fallback(
        self, request: SearchRequest, context: SearchContext
    ) -> list:
        """Try providers in order: primary first, then each fallback once.

        Only a `ProviderError` moves to the next provider. An empty hit list
        is a success and never triggers a fallback: failure and empty stay
        distinct. Raises the last ProviderError when every provider fails.
        """
        if not self._providers:
            raise ConfigError("No search providers configured.")
        last_error: ProviderError | None = None
        last_provider = ""
        for index, provider in enumerate(self._providers):
            started = time.perf_counter()
            try:
                hits = provider.search(request)
            except ProviderError as exc:
                last_provider = provider.name
                context.record_provider_latency(
                    provider.name, (time.perf_counter() - started) * 1000.0
                )
                context.add_error(f"{provider.name}: {exc}")
                last_error = exc
                if index > 0:
                    context.fallback_used = True
                elif len(self._providers) > 1:
                    context.add_error(
                        f"fallback: primary '{provider.name}' failed; "
                        + "trying next provider"
                    )
                    context.fallback_used = True
                continue
            context.record_provider_latency(
                provider.name, (time.perf_counter() - started) * 1000.0
            )
            if index > 0:
                context.fallback_used = True
            return list(hits)
        if last_error is None:  # guarded by the empty-providers check
            raise ProviderError(
                "search failed with no recorded error",
                provider_name=last_provider or "engine",
            )
        raise last_error

    def search(self, request: SearchRequest) -> SearchResponse:
        context = SearchContext(request=request)
        hits = self._search_with_fallback(request, context)
        # Defensive: providers must init final_score to original_score, but
        # never let a zero final_score wipe out a real provider signal.
        for hit in hits:
            if hit.final_score == 0.0 and hit.original_score != 0.0:
                hit.final_score = hit.original_score
        context.results = list(hits)

        # Mandatory-plugin failures propagate: fail closed, never leak
        # unfiltered results.
        context = self._pipeline.run(context)
        context = self._ranker.run(context)
        context = assemble_explanations(context)
        return SearchResponse(
            query=request.query,
            mode=request.mode,
            results=list(context.results),
            total=len(context.results),
            applied_filters=list(context.applied_filters),
            diagnostics=context.diagnostics(),
        )

    def health_check(self) -> dict[str, bool]:
        statuses: dict[str, bool] = {}
        for provider in self._providers:
            try:
                statuses[provider.name] = bool(provider.health_check())
            except Exception:  # noqa: BLE001 — health must never raise
                statuses[provider.name] = False
        return statuses

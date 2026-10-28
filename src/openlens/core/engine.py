"""Concrete Engine: query intelligence -> provider search -> pipeline -> rank."""

from __future__ import annotations

import time
from dataclasses import replace
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
from openlens.retrieval.query_processor import ProcessedQuery, QueryProcessor

#: Display bound for the per-request ``query`` provenance entry recorded in
#: context metadata and diagnostics (added terms are already hard-capped by
#: the expansion stage; this bounds the diagnostic rendering).
QUERY_ENTRY_CAP = 16


def _query_entry(processed: ProcessedQuery) -> dict[str, Any]:
    """Build the bounded query provenance entry for metadata/diagnostics."""
    return {
        "normalized": processed.normalized,
        "truncated": processed.truncated,
        "intent": processed.intent,
        "suggested_mode": processed.suggested_mode,
        "explicit_mode": processed.explicit_mode,
        "effective_mode": processed.effective_mode,
        "base_terms": list(processed.base_terms[:QUERY_ENTRY_CAP]),
        "added_terms": list(processed.added_terms[:QUERY_ENTRY_CAP]),
        "provenance": {
            term.term: term.source for term in processed.added[:QUERY_ENTRY_CAP]
        },
        "reasons": list(processed.reasons[:QUERY_ENTRY_CAP]),
    }


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
        self._query_processor = QueryProcessor()

    @property
    def pipeline(self) -> Pipeline:
        return self._pipeline

    @property
    def query_processor(self) -> QueryProcessor:
        return self._query_processor

    def configure(self, config: dict[str, Any]) -> None:
        """Apply ``{plugins: [...], ranking: {default_profile}}`` config.

        Also consumes the optional ``query:`` section into the standalone
        query processor (normalize -> intent -> expand); suggested modes
        are validated against ``get_profile`` here so an unknown suggestion
        is impossible by construction.

        Raises:
            ConfigError: malformed plugin entries, unknown ranking profile,
                or malformed ``query:`` section.
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
        # Standalone pre-retrieval stage: absent ``query:`` section means
        # code defaults. Raises ConfigError on a malformed section.
        self._query_processor = QueryProcessor.from_config(config)

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

    def _expansion_variants(
        self, processed: ProcessedQuery
    ) -> list[tuple[str, str, str]]:
        """Build one variant query per added term: (query, term, source).

        Each variant substitutes the added synonym for the base term(s) of
        its source set, so every variant keeps the remaining original terms
        required (OR-of-ANDs). The provider re-parses ``request.query``
        with the AND-joining ``parse_query``, so the structured expansion
        set must travel as these alongside queries — never as a rewritten
        query string, which would AND-require every synonym.
        """
        synonym_sets = self._query_processor.config.expansion.synonym_sets
        folded = {
            set_id: {member.casefold() for member in members}
            for set_id, members in synonym_sets.items()
            if isinstance(members, list)
        }
        variants: list[tuple[str, str, str]] = []
        seen = {processed.normalized}
        for added in processed.added:
            members = folded.get(added.source)
            if not members:
                continue
            variant_terms = [
                added.term if term in members else term for term in processed.base_terms
            ]
            variant = " ".join(variant_terms)
            if not variant or variant in seen:
                continue
            seen.add(variant)
            variants.append((variant, added.term, added.source))
        return variants

    def _merge_expansion_hits(
        self,
        provider_request: SearchRequest,
        context: SearchContext,
        processed: ProcessedQuery,
        hits: list[Any],
    ) -> list[Any]:
        """Fan out one provider search per expansion variant, merging recalls.

        Primary hits keep their order; variant hits with unseen ids append
        (carrying an expansion reason). A failing variant is recorded in
        diagnostics and skipped — it never fails the request.
        """
        if not processed.added:
            return hits
        seen_ids = {hit.id for hit in hits}
        merged = list(hits)
        for variant, term, source in self._expansion_variants(processed):
            try:
                extra = self._search_with_fallback(
                    replace(provider_request, query=variant), context
                )
            except ProviderError as exc:
                context.add_error(f"expansion '{variant}': {exc}")
                continue
            for hit in extra:
                if hit.id in seen_ids:
                    continue
                seen_ids.add(hit.id)
                hit.reasons.append(f"matched expansion '{term}' (source: {source})")
                merged.append(hit)
        limit = provider_request.limit
        if isinstance(limit, int) and limit > 0 and len(merged) > limit:
            merged = merged[:limit]
        return merged

    def search(self, request: SearchRequest) -> SearchResponse:
        # Pre-retrieval query stage: normalize -> intent -> expand ahead of
        # provider selection. Explicit mode always wins (the processor
        # records the suggestion separately); otherwise the suggested mode
        # is the effective one. Unknown explicit modes fail loudly here via
        # get_profile, never silently misrank downstream.
        processed = self._query_processor.process(request.query, mode=request.mode)
        provider_request = (
            request
            if processed.effective_mode == request.mode
            else replace(request, mode=processed.effective_mode)
        )
        context = SearchContext(request=provider_request)
        context.metadata["query"] = _query_entry(processed)
        hits = self._search_with_fallback(provider_request, context)
        hits = self._merge_expansion_hits(provider_request, context, processed, hits)
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
            mode=processed.effective_mode,
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

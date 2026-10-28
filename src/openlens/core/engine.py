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
from openlens.providers.manager import ProviderManager
from openlens.core.pipeline import Pipeline, assemble_explanations
from openlens.core.registry import PluginRegistry
from openlens.ranking.profiles import (
    BUILTIN_PROFILES,
    RankingProfile,
    profile_from_dict,
)
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


def _coerce_profiles(raw: Any) -> dict[str, RankingProfile] | None:
    """Normalize an optional ``ranking.profiles`` mapping for the Ranker.

    Accepts ``None`` (Ranker falls back to builtins), a mapping of names to
    raw profile dicts (parsed via ``profile_from_dict``), or a mapping of
    names to already-built ``RankingProfile`` objects. Anything else is a
    ``ConfigError`` at startup.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("Engine config 'ranking.profiles' must be a mapping.")
    profiles: dict[str, RankingProfile] = {}
    for profile_name, entry in raw.items():
        if isinstance(entry, RankingProfile):
            profiles[profile_name] = entry
        elif isinstance(entry, dict):
            profiles[profile_name] = profile_from_dict(profile_name, entry)
        else:
            raise ConfigError(f"Ranking profile '{profile_name}' must be a mapping.")
    return profiles


class SearchEngine(Engine):
    """Default Engine implementation.

    Provider selection is owned by a :class:`ProviderManager` (single-owner
    routing, which also puts the manager's timeout bound on the live path);
    the engine keeps the query pipeline, ranker, and response assembly.
    Pass ``manager=`` (or use :meth:`from_manager`); the legacy
    ``providers=[...]`` list constructor keeps working by fanning out over
    the list with single/fallback semantics.
    """

    def __init__(
        self,
        providers: list[SearchProvider] | tuple[SearchProvider, ...] = (),
        plugins: list[Any] | tuple[Any, ...] = (),
        ranker: Ranker | None = None,
        manager: ProviderManager | None = None,
    ) -> None:
        if manager is not None and providers:
            raise ConfigError(
                "SearchEngine takes either 'manager' or 'providers', not both."
            )
        self._manager = manager
        self._providers = list(providers)
        self._pipeline = Pipeline(list(plugins))
        self._ranker = ranker or Ranker()
        self._query_processor = QueryProcessor()

    @classmethod
    def from_manager(
        cls,
        manager: ProviderManager,
        plugins: list[Any] | tuple[Any, ...] = (),
        ranker: Ranker | None = None,
    ) -> "SearchEngine":
        """Build an engine that delegates provider selection to ``manager``."""
        return cls(plugins=plugins, ranker=ranker, manager=manager)

    @property
    def pipeline(self) -> Pipeline:
        return self._pipeline

    @property
    def query_processor(self) -> QueryProcessor:
        return self._query_processor

    def configure(self, config: dict[str, Any]) -> None:
        """Apply ``{plugins: [...], ranking: {default_profile, profiles?}}`` config.

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
        profiles = _coerce_profiles(ranking_cfg.get("profiles", None))
        effective = BUILTIN_PROFILES if profiles is None else profiles
        if default_profile not in effective:
            raise ConfigError(
                f"Unknown ranking profile '{default_profile}'. "
                f"Known: {', '.join(sorted(effective))}."
            )
        plugins = PluginRegistry().build(plugin_entries)
        self._pipeline = Pipeline(plugins)
        self._ranker = Ranker({"profile": default_profile}, profiles=profiles)
        # Standalone pre-retrieval stage: absent ``query:`` section means
        # code defaults. Raises ConfigError on a malformed section.
        self._query_processor = QueryProcessor.from_config(config)

    def _select_provider(self) -> SearchProvider:
        if not self._providers:
            raise ConfigError("No search providers configured.")
        return self._providers[0]

    def _retrieve(
        self, fetch_request: SearchRequest, context: SearchContext
    ) -> list[Any]:
        """Run provider selection for one fetch-depth request.

        The manager path delegates selection (and timeout enforcement) to
        the :class:`ProviderManager`; the legacy list path fans out over
        the configured providers in order. Callers always pass the
        fetch-depth request (``offset=0``, ``limit=offset+limit``); the
        engine slices the page once after ranking.
        """
        if self._manager is not None:
            return self._manager.search(fetch_request, context)
        return self._search_with_fallback(fetch_request, context)

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
        diagnostics and skipped — it never fails the request. The request
        is the fetch-depth request, so the merge cap below preserves the
        full fetch window for the single post-ranking slice.
        """
        if not processed.added:
            return hits
        seen_ids = {hit.id for hit in hits}
        merged = list(hits)
        for variant, term, source in self._expansion_variants(processed):
            try:
                extra = self._retrieve(
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
        # Paging ownership: providers are queried once at the fetch depth
        # (offset=0, limit=offset+limit); the single slice below is the only
        # pagination applied, with total set to the pre-slice merged count.
        fetch_request = replace(
            provider_request,
            offset=0,
            limit=provider_request.offset + provider_request.limit,
        )
        context = SearchContext(request=provider_request)
        context.metadata["query"] = _query_entry(processed)
        hits = self._retrieve(fetch_request, context)
        hits = self._merge_expansion_hits(fetch_request, context, processed, hits)
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
        total = len(context.results)
        page = context.results[
            provider_request.offset : provider_request.offset + provider_request.limit
        ]
        return SearchResponse(
            query=request.query,
            mode=processed.effective_mode,
            results=list(page),
            total=total,
            applied_filters=list(context.applied_filters),
            diagnostics=context.diagnostics(),
        )

    def health_check(self) -> dict[str, bool]:
        if self._manager is not None:
            return self._manager.health_check()
        statuses: dict[str, bool] = {}
        for provider in self._providers:
            try:
                statuses[provider.name] = bool(provider.health_check())
            except Exception:  # noqa: BLE001 — health must never raise
                statuses[provider.name] = False
        return statuses

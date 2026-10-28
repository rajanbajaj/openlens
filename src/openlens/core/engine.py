"""Concrete Engine: query intelligence -> provider search -> pipeline -> rank."""

from __future__ import annotations

import time
from contextlib import suppress
from dataclasses import replace
from typing import Any

from openlens.cache.base import (
    BACKEND_ERROR,
    HIT,
    MISS,
    CacheProvider,
    build_cache_key,
    restore_cached_page,
    serialize_cached_page,
)
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
from openlens.core.registry import AI_PLUGIN_NAMES, PluginRegistry, inject_model
from openlens.models.manager import ModelManager
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


def _build_model_manager(models_cfg: Any) -> ModelManager | None:
    """Build the optional manager; zero enabled backends yields ``None``.

    Only the zero-enabled ``ConfigError`` from ``ModelManager.from_config``
    is caught here; unknown primaries and other validation errors propagate.
    """
    if models_cfg is None:
        return None
    if not isinstance(models_cfg, dict):
        raise ConfigError("Engine config 'models' must be a mapping.")
    arg: dict[str, Any] = (
        models_cfg if "models" in models_cfg else {"models": models_cfg}
    )
    try:
        return ModelManager.from_config(arg)
    except ConfigError as exc:
        if "No enabled model backends" in str(exc):
            return None
        raise


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
        model_manager: ModelManager | None = None,
        cache: CacheProvider | None = None,
        cache_ttl_s: int = 120,
    ) -> None:
        if manager is not None and providers:
            raise ConfigError(
                "SearchEngine takes either 'manager' or 'providers', not both."
            )
        self._manager = manager
        self._model_manager = model_manager
        self._cache = cache
        self._cache_ttl_s = cache_ttl_s
        self._ranking_profile = "default"
        self._providers = list(providers)
        self._feedback_counts: dict[tuple[str, str], int] = {}
        self._propagate_models()
        plugin_list = list(plugins)
        if self._model_manager is not None:
            primary = self._model_manager.get(self._model_manager.primary)
            for plugin in plugin_list:
                if getattr(plugin, "name", None) in AI_PLUGIN_NAMES:
                    inject_model(plugin, primary)
        self._pipeline = Pipeline(plugin_list)
        self._ranker = ranker or Ranker()
        self._query_processor = QueryProcessor()

    @classmethod
    def from_manager(
        cls,
        manager: ProviderManager,
        plugins: list[Any] | tuple[Any, ...] = (),
        ranker: Ranker | None = None,
        model_manager: ModelManager | None = None,
        cache: CacheProvider | None = None,
        cache_ttl_s: int = 120,
    ) -> "SearchEngine":
        """Build an engine that delegates provider selection to ``manager``."""
        return cls(
            plugins=plugins,
            ranker=ranker,
            manager=manager,
            model_manager=model_manager,
            cache=cache,
            cache_ttl_s=cache_ttl_s,
        )

    @property
    def model_manager(self) -> ModelManager | None:
        return self._model_manager

    @model_manager.setter
    def model_manager(self, value: ModelManager | None) -> None:
        self._model_manager = value
        self._propagate_models()

    def _propagate_models(self) -> None:
        """Push the model manager into providers exposing ``attach_models``.

        Semantic fan-in call-site: the engine owns the optional
        ``ModelManager`` (``None`` when no embedding backend is enabled)
        and every supporting provider (``custom_index``) mirrors it, so
        enabling ``semantic_enabled`` in provider config takes effect
        without the caller touching providers directly.
        """
        if self._manager is not None:
            self._manager.attach_models(self._model_manager)
            return
        for provider in self._providers:
            hook = getattr(provider, "attach_models", None)
            if callable(hook):
                with suppress(Exception):
                    hook(self._model_manager)

    def suggest_terms(self, limit: int = 200) -> list[str]:
        """Live index vocabulary for query assistance (``[]`` when none).

        Aggregates provider vocabularies via the manager (or the legacy
        provider list); the API layer prefers this hook over its static
        fallback. Never raises: provider failures degrade to ``[]``.
        """
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            return []
        collected: list[str] = []
        seen: set[str] = set()
        if self._manager is not None:
            try:
                return self._manager.suggest_terms(limit=limit)
            except Exception:
                return []
        for provider in self._providers:
            hook = getattr(provider, "suggest_terms", None)
            if not callable(hook):
                continue
            terms: Any = None
            with suppress(Exception):
                terms = hook(limit=limit)
            if not isinstance(terms, list):
                continue
            for term in terms:
                if not isinstance(term, str) or not term.strip():
                    continue
                key = term.strip().casefold()
                if key in seen:
                    continue
                seen.add(key)
                collected.append(term.strip())
        collected.sort(key=str.casefold)
        return collected[:limit]

    def record_feedback(self, doc_id: str, action: str) -> int:
        """Record one anonymous feedback event; return its in-process count.

        Counts only: the store keeps ``(doc_id, action)`` tallies, never
        query text or identifiers. Raises ``ValueError`` on blank inputs
        so API validation stays the single gate for the HTTP path.
        """
        if not isinstance(doc_id, str) or not isinstance(action, str):
            raise ValueError("doc_id and action must not be blank")
        pair = (doc_id.strip(), action.strip())
        if not pair[0] or not pair[1]:
            raise ValueError("doc_id and action must not be blank")
        count = self._feedback_counts.get(pair, 0) + 1
        self._feedback_counts[pair] = count
        return count

    def feedback_counts(self) -> dict[tuple[str, str], int]:
        """Copy of the in-process anonymous feedback tallies."""
        return dict(self._feedback_counts)

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
        if "model_manager" in config:
            direct = config.get("model_manager")
            if direct is not None and not isinstance(direct, ModelManager):
                raise ConfigError(
                    "Engine config 'model_manager' must be a ModelManager."
                )
            self._model_manager = direct
        elif "models" in config:
            self._model_manager = _build_model_manager(config.get("models"))
        # Fan the (possibly None) manager out to supporting providers so
        # the semantic signal follows the engine's model configuration.
        self._propagate_models()
        # Manager-primary seam: with no enabled backend there is no AI, so
        # AI plugins are omitted rather than falling back to a dummy model.
        effective_entries = plugin_entries
        if self._model_manager is None:
            effective_entries = [
                entry
                for entry in plugin_entries
                if not (
                    isinstance(entry, dict) and entry.get("name") in AI_PLUGIN_NAMES
                )
            ]
        plugins = PluginRegistry().build(
            effective_entries, model_manager=self._model_manager
        )
        self._pipeline = Pipeline(plugins)
        self._ranking_profile = default_profile
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

    def invalidate_cache(self) -> None:
        """Drop every cached entry; backend failures are swallowed (fail open)."""
        if self._cache is None:
            return
        try:
            self._cache.clear()
        except Exception:  # noqa: BLE001 — cache must never fail requests
            return

    def _provider_names(self) -> list[str]:
        if self._manager is not None:
            return list(self._manager.provider_names)
        return sorted({provider.name for provider in self._providers})

    def _cache_lookup(self, key: str, context: SearchContext) -> bool:
        """Serve a cached page into ``context``; True on usable hit.

        Every backend failure degrades to a miss and is recorded, never
        raised: the cache fails open by construction.
        """
        if self._cache is None:
            return False
        try:
            raw = self._cache.get(key)
        except Exception:  # noqa: BLE001 — cache must never fail requests
            context.metadata["cache"] = {"status": BACKEND_ERROR}
            return False
        if raw is None:
            context.metadata["cache"] = {"status": MISS}
            return False
        restored = restore_cached_page(raw)
        if restored is None:
            context.metadata["cache"] = {"status": MISS}
            return False
        results, total, applied_filters = restored
        context.results = results
        context.applied_filters = applied_filters
        context.metadata["cache"] = {"status": HIT}
        context.metadata["cache_total"] = total
        return True

    def _cache_store(self, key: str, context: SearchContext, total: int) -> None:
        if self._cache is None:
            return
        try:
            self._cache.set(
                key,
                serialize_cached_page(
                    list(context.results), total, list(context.applied_filters)
                ),
                self._cache_ttl_s,
            )
        except Exception:  # noqa: BLE001 — cache must never fail requests
            context.metadata["cache"] = {"status": BACKEND_ERROR}

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
        # Post-filter cache hook (the single hook point): the cached value
        # is the final post-explanation ranked list, so cached entries can
        # never bypass mandatory filtering. The key carries the normalized
        # query, effective mode, restriction filters, provider prefs and
        # set, ranking profile, and fetch depth.
        cache_key: str | None = None
        cache_hit = False
        if self._cache is not None:
            prefs = request.provider_prefs
            cache_key = build_cache_key(
                normalized_query=processed.normalized,
                mode=processed.effective_mode,
                filters=dict(request.filters),
                provider_prefs=(
                    {
                        "include": list(prefs.include),
                        "exclude": list(prefs.exclude),
                        "primary": prefs.primary,
                    }
                    if prefs is not None
                    else None
                ),
                provider_names=self._provider_names(),
                ranking_profile=self._ranking_profile,
                fetch_limit=fetch_request.limit,
            )
            cache_hit = self._cache_lookup(cache_key, context)
        if cache_hit:
            total = int(context.metadata.get("cache_total", len(context.results)))
        else:
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
            if cache_key is not None and self._cache is not None:
                self._cache_store(cache_key, context, total)
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

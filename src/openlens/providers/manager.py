"""Provider manager: startup validation plus routing strategies.

Only providers with ``enabled: true`` are instantiated — disabled entries
are never imported beyond their type lookup, constructed, or contacted, so
offline/secret-less operation is the default. Timeouts are bounded per
provider (``timeout_s``) via worker threads: a hung backend surfaces as
:class:`ProviderError` with code ``timeout`` instead of stalling the
pipeline. Per-request latency is recorded into :class:`SearchContext`
diagnostics when a context is supplied.

Strategies:

* ``single`` — route to the primary provider under its timeout bound.
* ``fallback`` — primary first; on primary *failure* (including timeout)
  the fallback is tried once under its own bound. An empty hit list is a
  success and never triggers the fallback.
* ``federated`` — fan out over the preference-selected providers in
  parallel (bounded ``ThreadPoolExecutor``), fuse the per-provider ranked
  lists with reciprocal rank fusion (:func:`fuse`), and return the merged
  window. Partial failure degrades (survivor hits plus per-provider errors
  recorded in diagnostics); empty results from every provider are a
  success with zero hits, while every provider failing raises one
  :class:`ProviderError` with code ``unavailable``.

Paging ownership: every provider is queried at ``offset=0`` with a fetch
depth of ``offset + limit``; the engine slices ``[offset:offset + limit]``
once after ranking with ``total`` set to the pre-slice merged count. The
original ``offset`` is never passed to providers (that would double-skip).
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import replace
from typing import Any, Optional

from openlens.core.context import SearchContext
from openlens.core.interfaces import (
    ConfigError,
    ProviderError,
    SearchRequest,
    SearchResult,
    SearchProvider,
)
from openlens.providers.base import DEFAULT_TIMEOUT_S, MAX_TIMEOUT_S
from openlens.providers.registry import create, register_builtin_providers
from openlens.retrieval.fusion import fuse

#: Config keys that live in the ``providers:`` strategy section rather than
#: naming a provider entry. Shared with ``openlens.main._manager_section``
#: so the app config translator forwards them instead of treating them as
#: provider entries.
STRATEGY_SECTION_KEYS = (
    "strategy",
    "primary",
    "fallback",
    "federated_timeout_s",
    "federated_primary_boost",
    "federated_max_merged",
)

_RESERVED_KEYS = STRATEGY_SECTION_KEYS

#: Route every request to the primary provider.
SINGLE_STRATEGY = "single"

#: Primary plus bounded fallback: on primary *failure* the fallback provider
#: is tried once. An empty hit list is a success, never a trigger.
FALLBACK_STRATEGY = "fallback"

#: Parallel fan-out over the selected providers with RRF fusion.
FEDERATED_STRATEGY = "federated"

SUPPORTED_STRATEGIES = (SINGLE_STRATEGY, FALLBACK_STRATEGY, FEDERATED_STRATEGY)

#: Upper bound on federated fan-out threads (one submit per provider).
MAX_FANOUT_WORKERS = 16

#: Default multiplier applied to the ``primary`` provider's fusion weight
#: (only when ``provider_prefs.primary`` is set).
DEFAULT_FEDERATED_PRIMARY_BOOST = 1.5

#: Default cap on the merged federated window before the engine slices.
DEFAULT_FEDERATED_MAX_MERGED = 200


def _check_timeout_s(name: str, value: Any) -> float:
    """Validate a timeout bound. Returns the float value."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(
            f"'{name}' must be a number, got {value!r}. Startup validation failed."
        )
    bound = float(value)
    if not 0 < bound <= MAX_TIMEOUT_S:
        raise ConfigError(
            f"'{name}' must be within (0, {MAX_TIMEOUT_S}], got {value!r}. "
            "Startup validation failed."
        )
    return bound


def _check_boost(name: str, value: Any) -> float:
    """Validate the federated primary fusion-weight multiplier."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(
            f"'{name}' must be a number, got {value!r}. Startup validation failed."
        )
    boost = float(value)
    if not boost > 0:
        raise ConfigError(
            f"'{name}' must be positive, got {value!r}. Startup validation failed."
        )
    return boost


def _check_cost_per_call(name: str, value: Any) -> float:
    """Validate a per-provider estimated call cost. Returns the float value."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(
            f"'{name}' cost_per_call must be a number, got {value!r}. "
            "Startup validation failed."
        )
    cost = float(value)
    if cost < 0:
        raise ConfigError(
            f"'{name}' cost_per_call must be >= 0, got {value!r}. "
            "Startup validation failed."
        )
    return cost


def _check_max_merged(name: str, value: Any) -> int:
    """Validate the merged-window cap."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(
            f"'{name}' must be a positive integer, got {value!r}. "
            "Startup validation failed."
        )
    if value <= 0:
        raise ConfigError(
            f"'{name}' must be a positive integer, got {value!r}. "
            "Startup validation failed."
        )
    return value


class ProviderManager:
    """Owns provider instances and routes searches under one strategy."""

    def __init__(
        self,
        providers: dict[str, SearchProvider],
        primary: str,
        strategy: str = SINGLE_STRATEGY,
        fallback: str | None = None,
        *,
        federated_timeout_s: float | None = None,
        federated_primary_boost: float = DEFAULT_FEDERATED_PRIMARY_BOOST,
        federated_max_merged: int = DEFAULT_FEDERATED_MAX_MERGED,
        provider_costs: dict[str, float] | None = None,
    ) -> None:
        if strategy not in SUPPORTED_STRATEGIES:
            raise ConfigError(
                f"Unknown provider strategy '{strategy}'. "
                f"Supported: {sorted(SUPPORTED_STRATEGIES)}. Startup validation failed."
            )
        if not providers:
            raise ConfigError(
                "No enabled providers. Enable at least one provider under "
                "'providers:'. Startup validation failed."
            )
        if primary not in providers:
            raise ConfigError(
                f"Primary provider '{primary}' is not enabled. Enabled "
                f"providers: {sorted(providers)}. Startup validation failed."
            )
        if strategy == FALLBACK_STRATEGY and not fallback:
            raise ConfigError(
                "Provider strategy 'fallback' requires a 'fallback:' provider name. "
                "Startup validation failed."
            )
        if fallback is not None:
            if fallback not in providers:
                raise ConfigError(
                    f"Fallback provider '{fallback}' is not enabled. Enabled "
                    f"providers: {sorted(providers)}. Startup validation failed."
                )
            if fallback == primary:
                raise ConfigError(
                    f"Fallback provider '{fallback}' must differ from primary "
                    "'{primary}'. Startup validation failed."
                )
        if federated_timeout_s is not None:
            federated_timeout_s = _check_timeout_s(
                "federated_timeout_s", federated_timeout_s
            )
        federated_primary_boost = _check_boost(
            "federated_primary_boost", federated_primary_boost
        )
        federated_max_merged = _check_max_merged(
            "federated_max_merged", federated_max_merged
        )
        self._providers = dict(providers)
        self._primary = primary
        self._strategy = strategy
        self._fallback = fallback
        self._federated_timeout_s = federated_timeout_s
        self._federated_primary_boost = federated_primary_boost
        self._federated_max_merged = federated_max_merged
        self._provider_costs = dict(provider_costs or {})

    # -- construction ----------------------------------------------------

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "ProviderManager":
        """Validate config and instantiate only enabled providers.

        Accepts either the full loaded config (with a top-level
        ``providers:`` mapping) or the ``providers:`` section itself.
        Raises ConfigError on any invalid section; disabled providers are
        skipped entirely.
        """
        if not isinstance(config, dict):
            raise ConfigError(
                "Provider config must be a mapping, "
                f"got {type(config).__name__}. Startup validation failed."
            )
        section = config.get("providers", config)
        if not isinstance(section, dict):
            raise ConfigError(
                "Config section 'providers' must be a mapping, "
                f"got {type(section).__name__}. Startup validation failed."
            )
        strategy = section.get("strategy", SINGLE_STRATEGY)
        if strategy not in SUPPORTED_STRATEGIES:
            raise ConfigError(
                f"Unknown provider strategy '{strategy}'. "
                f"Supported: {sorted(SUPPORTED_STRATEGIES)}. Startup validation failed."
            )
        federated_timeout_s = section.get("federated_timeout_s", None)
        if federated_timeout_s is not None:
            _check_timeout_s("federated_timeout_s", federated_timeout_s)
        federated_primary_boost = section.get(
            "federated_primary_boost", DEFAULT_FEDERATED_PRIMARY_BOOST
        )
        _check_boost("federated_primary_boost", federated_primary_boost)
        federated_max_merged = section.get(
            "federated_max_merged", DEFAULT_FEDERATED_MAX_MERGED
        )
        _check_max_merged("federated_max_merged", federated_max_merged)
        register_builtin_providers()
        providers: dict[str, SearchProvider] = {}
        provider_costs: dict[str, float] = {}
        for entry_name, entry in section.items():
            if entry_name in _RESERVED_KEYS:
                continue
            if not isinstance(entry, dict):
                raise ConfigError(
                    f"Provider entry '{entry_name}' must be a mapping, "
                    f"got {type(entry).__name__}. Startup validation failed."
                )
            if not entry.get("enabled", False):
                continue  # disabled: never instantiate, never contact
            provider_type = entry.get("type", entry_name)
            if not isinstance(provider_type, str) or not provider_type:
                raise ConfigError(
                    f"Provider entry '{entry_name}': 'type' must be a "
                    "non-empty string. Startup validation failed."
                )
            # Estimated call cost is manager accounting only: validate it
            # here and never pass it to the provider constructor.
            provider_costs[entry_name] = _check_cost_per_call(
                entry_name, entry.get("cost_per_call", 0.0)
            )
            options = {
                k: v
                for k, v in entry.items()
                if k not in ("enabled", "type", "cost_per_call")
            }
            providers[entry_name] = create(provider_type, options)
        if not providers:
            raise ConfigError(
                "No enabled providers. Enable at least one provider under "
                "'providers:'. Startup validation failed."
            )
        primary = section.get("primary")
        if primary is None:
            primary = next(iter(providers))
        if not isinstance(primary, str) or primary not in providers:
            raise ConfigError(
                f"Primary provider {primary!r} is not enabled. Enabled "
                f"providers: {sorted(providers)}. Startup validation failed."
            )
        fallback = section.get("fallback")
        if fallback is not None and (
            not isinstance(fallback, str) or fallback not in providers
        ):
            raise ConfigError(
                f"Fallback provider {fallback!r} is not enabled. Enabled "
                f"providers: {sorted(providers)}. Startup validation failed."
            )
        return cls(
            providers,
            primary,
            strategy,
            fallback,
            federated_timeout_s=federated_timeout_s,
            federated_primary_boost=federated_primary_boost,
            federated_max_merged=federated_max_merged,
            provider_costs=provider_costs,
        )

    # -- runtime ---------------------------------------------------------

    @property
    def primary(self) -> str:
        return self._primary

    @property
    def strategy(self) -> str:
        return self._strategy

    @property
    def fallback(self) -> str | None:
        """Configured fallback provider name, or None when no fallback."""
        return self._fallback

    @property
    def federated_timeout_s(self) -> float | None:
        """Overall federated deadline, or None for max-per-provider-plus-1s."""
        return self._federated_timeout_s

    @property
    def federated_primary_boost(self) -> float:
        """Fusion weight multiplier for the preferred provider."""
        return self._federated_primary_boost

    @property
    def federated_max_merged(self) -> int:
        """Cap on the merged federated window before engine slicing."""
        return self._federated_max_merged

    @property
    def provider_names(self) -> list[str]:
        return sorted(self._providers)

    def get(self, name: str) -> SearchProvider:
        """Return the instance for ``name``. Raises ConfigError if absent."""
        try:
            return self._providers[name]
        except KeyError:
            raise ConfigError(
                f"Provider '{name}' is not enabled. Enabled providers: "
                f"{sorted(self._providers)}."
            ) from None

    def attach_models(self, models: Any | None) -> None:
        """Inject the embedding model manager into supporting providers.

        Semantic fan-in call-site: providers exposing ``attach_models``
        (currently ``custom_index``) receive the engine's model manager
        (or ``None`` when no embedding backend is enabled, restoring
        keyword-only behavior). Providers without the hook are skipped;
        a provider that rejects the manager keeps its previous setting
        rather than failing startup.
        """
        for provider in self._providers.values():
            hook = getattr(provider, "attach_models", None)
            if not callable(hook):
                continue
            with suppress(Exception):
                hook(models)

    def suggest_terms(self, limit: int = 200) -> list[str]:
        """Aggregate the live index vocabulary for query assistance.

        Collects terms from every provider exposing ``suggest_terms``,
        deduplicated (first-seen spelling wins) and sorted
        case-insensitively for determinism. Returns ``[]`` when no
        provider offers a vocabulary, so callers fall back gracefully.
        """
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            return []
        collected: list[str] = []
        seen: set[str] = set()
        for name in sorted(self._providers):
            hook = getattr(self._providers[name], "suggest_terms", None)
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

    def _select_providers(self, prefs: Any) -> list[str]:
        """Resolve per-query provider preferences to entry names.

        ``include`` restricts to the named providers, ``exclude`` removes,
        and ``primary`` names the provider whose fusion weight is boosted.
        All names reference config entry names and are validated loudly:
        unknown names, a ``primary`` outside the selection, and an empty
        selection are all request errors (ConfigError), never silent
        fallback to all providers.
        """
        enabled = set(self._providers)
        for name in (*prefs.include, *prefs.exclude):
            if name not in enabled:
                raise ConfigError(
                    f"Unknown provider {name!r} in provider_prefs. Enabled "
                    f"providers: {sorted(enabled)}. Request rejected."
                )
        if prefs.primary is not None and prefs.primary not in enabled:
            raise ConfigError(
                f"Unknown provider {prefs.primary!r} as provider_prefs.primary. "
                f"Enabled providers: {sorted(enabled)}. Request rejected."
            )
        if prefs.include:
            selected = list(dict.fromkeys(prefs.include))
        else:
            selected = sorted(enabled)
        excluded = set(prefs.exclude)
        selected = [name for name in selected if name not in excluded]
        if prefs.primary is not None and prefs.primary not in selected:
            raise ConfigError(
                f"provider_prefs.primary {prefs.primary!r} is not in the selected "
                f"providers {selected}. Request rejected."
            )
        if not selected:
            raise ConfigError(
                f"provider_prefs selects no providers (include={list(prefs.include)}, "
                f"exclude={list(prefs.exclude)}). Request rejected."
            )
        return selected

    def search(
        self, request: SearchRequest, context: Optional[SearchContext] = None
    ) -> list[SearchResult]:
        """Run the request under the configured strategy.

        Every provider is queried at ``offset=0`` with a fetch depth of
        ``offset + limit`` (paging ownership); the engine slices the page
        once after ranking. Preferences are validated for every strategy
        (unknown names and empty selections fail loudly); only the
        federated strategy fans out over the selection.
        """
        selected = self._select_providers(request.provider_prefs)
        if self._strategy == FEDERATED_STRATEGY:
            return self._search_federated(request, selected, context)
        fetch = replace(request, offset=0, limit=request.offset + request.limit)
        try:
            return self._search_one(self._primary, fetch, context)
        except ProviderError as primary_exc:
            if self._fallback is None:
                raise
            if context is not None:
                context.add_error(
                    f"fallback: primary '{self._primary}' failed; "
                    + f"trying '{self._fallback}'"
                )
                context.fallback_used = True
            try:
                return self._search_one(self._fallback, fetch, context)
            except ProviderError as fallback_exc:
                if context is not None:
                    context.add_error(f"fallback '{self._fallback}' also failed")
                # Prefer the fallback error (it is the freshest signal), but
                # keep the primary failure visible in diagnostics.
                raise fallback_exc from primary_exc

    def _record_accounting(
        self,
        name: str,
        hits: list[SearchResult] | None,
        context: Optional[SearchContext],
    ) -> None:
        """Write per-request result counts and estimated costs.

        Successes record their hit count; every attempt (success or
        failure) records its estimated call cost, since a timed-out or
        failed call still spends the backend invocation.
        """
        if context is None:
            return
        if hits is not None:
            context.record_provider_results(name, len(hits))
        context.record_provider_cost(name, self._provider_costs.get(name, 0.0))

    def _invoke(
        self, name: str, request: SearchRequest
    ) -> tuple[list[SearchResult], int | None]:
        """Run one provider call with error mapping but no thread/timeout.

        Thread-free core shared by the thread-bound single/fallback wrapper
        (:meth:`_search_one`) and the federated executor fan-out, which
        enforces its own per-provider and overall deadlines. Never leaks
        raw client exceptions: non-ProviderError failures surface as
        ProviderError.

        Returns the hit window plus the provider's full match count when
        it implements the optional ``search_with_total`` (feeds true
        pagination totals); ``None`` for the legacy list-only contract.
        Callers record the count into the context.
        """
        provider = self._providers[name]
        try:
            search_with_total = getattr(provider, "search_with_total", None)
            if search_with_total is not None:
                hits, total = search_with_total(request)
                return list(hits), (
                    int(total) if isinstance(total, int) and total >= 0 else None
                )
            return list(provider.search(request)), None
        except ProviderError as exc:
            if not exc.provider_name:
                exc.provider_name = name
            raise
        except BaseException as exc:
            raise ProviderError(
                f"provider '{name}' failed: {exc}",
                provider_name=name,
                retryable=False,
                code=ProviderError.UNKNOWN,
            ) from exc

    def _search_one(
        self, name: str, request: SearchRequest, context: Optional[SearchContext]
    ) -> list[SearchResult]:
        """Run one provider under its bounded timeout. May raise ProviderError."""
        timeout_s = getattr(self._providers[name], "timeout_s", DEFAULT_TIMEOUT_S)
        start = time.monotonic()
        box: dict[str, Any] = {}

        def target() -> None:
            try:
                box["result"] = self._invoke(name, request)
            except BaseException as exc:  # re-raised below as ProviderError
                box["error"] = exc

        # Daemon worker: a hung backend yields a timeout error without ever
        # stalling the pipeline or interpreter shutdown.
        worker = threading.Thread(target=target, daemon=True)
        worker.start()
        worker.join(timeout=timeout_s)
        elapsed_ms = (time.monotonic() - start) * 1000.0
        if context is not None:
            context.record_provider_latency(name, elapsed_ms)
        if worker.is_alive():
            self._record_accounting(name, None, context)
            raise ProviderError.timeout(
                name,
                f"provider '{name}' timed out after {timeout_s}s",
            )
        if "error" in box:
            err = box["error"]
            self._record_accounting(name, None, context)
            if isinstance(err, ProviderError):
                if not err.provider_name:
                    err.provider_name = name
                raise err
            raise ProviderError(
                f"provider '{name}' failed: {err}",
                provider_name=name,
                retryable=False,
                code=ProviderError.UNKNOWN,
            ) from err
        window, reported_total = box.get("result", ([], None))
        hits = list(window)
        if context is not None and reported_total is not None:
            context.record_provider_total(name, reported_total)
        self._record_accounting(name, hits, context)
        return hits

    def _search_federated(
        self,
        request: SearchRequest,
        selected: list[str],
        context: Optional[SearchContext],
    ) -> list[SearchResult]:
        """Fan out over ``selected`` in parallel and fuse with RRF.

        Each provider is queried at ``offset=0`` with fetch depth
        ``offset + limit``; per-provider ``timeout_s`` bounds bound each
        call and the overall ``federated_timeout_s`` deadline (default:
        max per-provider bound + 1s) bounds the whole fan-out. Partial
        failure degrades (survivor hits plus per-provider errors recorded
        in diagnostics); empty results from every provider are a success
        with zero hits, while every provider failing raises one
        ProviderError with code ``unavailable`` naming every attempted
        provider.
        """
        prefs = request.provider_prefs
        fetch = replace(request, offset=0, limit=request.offset + request.limit)
        weights: dict[str, float] | None = None
        if prefs.primary is not None:
            weights = {
                name: (self._federated_primary_boost if name == prefs.primary else 1.0)
                for name in selected
            }
        bounds = {
            name: float(getattr(self._providers[name], "timeout_s", DEFAULT_TIMEOUT_S))
            for name in selected
        }
        overall = self._federated_timeout_s
        if overall is None:
            overall = max(bounds.values()) + 1.0
        started = time.monotonic()
        provider_deadlines = {name: started + bound for name, bound in bounds.items()}
        overall_deadline = started + overall

        ranked: dict[str, list[SearchResult]] = {}
        failures: dict[str, BaseException] = {}
        latencies: dict[str, float] = {}
        state_lock = threading.Lock()
        expired = threading.Event()

        def _fan_out() -> None:
            # Runs on a daemon thread so the executor's worker threads
            # inherit daemon status (threading.Thread defaults daemon to
            # the creating thread's flag): abandoned provider calls can
            # never stall interpreter shutdown.
            executor = ThreadPoolExecutor(
                max_workers=min(len(selected), MAX_FANOUT_WORKERS),
                thread_name_prefix="openlens-federated",
            )
            try:
                futures = {
                    executor.submit(self._invoke, name, fetch): name
                    for name in selected
                }
                for future, name in futures.items():
                    if expired.is_set():
                        break
                    remaining = (
                        min(provider_deadlines[name], overall_deadline)
                        - time.monotonic()
                    )
                    try:
                        if remaining <= 0:
                            raise TimeoutError(
                                f"provider '{name}' exceeded its deadline"
                            )
                        hits = future.result(timeout=remaining)
                    except TimeoutError:
                        with state_lock:
                            failures[name] = ProviderError.timeout(
                                name,
                                f"provider '{name}' timed out after {bounds[name]}s",
                            )
                            latencies[name] = (time.monotonic() - started) * 1000.0
                    except ProviderError as exc:
                        with state_lock:
                            failures[name] = exc
                            latencies[name] = (time.monotonic() - started) * 1000.0
                    else:
                        with state_lock:
                            window, reported_total = hits
                            ranked[name] = list(window)
                            latencies[name] = (time.monotonic() - started) * 1000.0
                            if context is not None and reported_total is not None:
                                context.record_provider_total(name, reported_total)
            finally:
                # Never awaited: on overall expiry late workers are daemon
                # threads recorded as timeouts above; the executor is not
                # joined so collection can never stall the request.
                executor.shutdown(wait=False)

        launcher = threading.Thread(target=_fan_out, daemon=True)
        launcher.start()
        launcher.join(timeout=overall)
        if launcher.is_alive():
            expired.set()
            with state_lock:
                for name in selected:
                    if name not in ranked and name not in failures:
                        failures[name] = ProviderError.timeout(
                            name,
                            f"federated search exceeded overall deadline of "
                            f"{overall}s; provider '{name}' did not respond "
                            "in time",
                        )
                        latencies.setdefault(
                            name, (time.monotonic() - started) * 1000.0
                        )
        with state_lock:
            snap_ranked = dict(ranked)
            snap_failures = dict(failures)
            snap_latencies = dict(latencies)
        if context is not None:
            for name in selected:
                context.record_provider_latency(
                    name,
                    snap_latencies.get(name, (time.monotonic() - started) * 1000.0),
                )
                self._record_accounting(name, snap_ranked.get(name), context)
            for name in sorted(snap_failures):
                context.add_error(f"{name}: {snap_failures[name]}")
        if not snap_ranked:
            attempted = sorted(selected)
            first = next(iter(snap_failures.values()), None)
            raise ProviderError(
                "federated search failed: all "
                f"{len(attempted)} provider(s) failed "
                f"({', '.join(attempted)})",
                provider_name="",
                retryable=False,
                code=ProviderError.UNAVAILABLE,
            ) from first
        merged = fuse(snap_ranked, weights=weights)
        if len(merged) > self._federated_max_merged:
            merged = merged[: self._federated_max_merged]
        return merged

    def health_check(self) -> dict[str, bool]:
        """Per-provider health keyed by enabled provider name. Never raises."""
        health: dict[str, bool] = {}
        for name, provider in self._providers.items():
            try:
                health[name] = bool(provider.health_check())
            except Exception:
                health[name] = False
        return health

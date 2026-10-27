"""Provider manager: startup validation plus the single-provider strategy.

Only providers with ``enabled: true`` are instantiated — disabled entries
are never imported beyond their type lookup, constructed, or contacted, so
offline/secret-less operation is the default. Timeouts are bounded per
provider (``timeout_s``) via a worker thread: a hung backend surfaces as
:class:`ProviderError` with code ``timeout`` instead of stalling the
pipeline. Per-request latency is recorded into :class:`SearchContext`
diagnostics when a context is supplied.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Optional

from openlens.core.context import SearchContext
from openlens.core.interfaces import (
    ConfigError,
    ProviderError,
    SearchRequest,
    SearchResult,
    SearchProvider,
)
from openlens.providers.base import DEFAULT_TIMEOUT_S
from openlens.providers.registry import create, register_builtin_providers

_RESERVED_KEYS = ("strategy", "primary", "fallback")

#: The only routing strategy the skeleton ships. Federated search arrives later.
SINGLE_STRATEGY = "single"

#: Primary plus bounded fallback: on primary *failure* the fallback provider
#: is tried once. An empty hit list is a success, never a trigger.
FALLBACK_STRATEGY = "fallback"

SUPPORTED_STRATEGIES = (SINGLE_STRATEGY, FALLBACK_STRATEGY)


class ProviderManager:
    """Owns provider instances and routes searches under one strategy."""

    def __init__(
        self,
        providers: dict[str, SearchProvider],
        primary: str,
        strategy: str = SINGLE_STRATEGY,
        fallback: str | None = None,
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
                    f"'{primary}'. Startup validation failed."
                )
        self._providers = dict(providers)
        self._primary = primary
        self._strategy = strategy
        self._fallback = fallback

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
        register_builtin_providers()
        providers: dict[str, SearchProvider] = {}
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
            options = {k: v for k, v in entry.items() if k not in ("enabled", "type")}
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
        return cls(providers, primary, strategy, fallback)

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

    def search(
        self, request: SearchRequest, context: Optional[SearchContext] = None
    ) -> list[SearchResult]:
        """Run the request against the primary provider with a bounded timeout.

        On primary *failure* (including timeout) the configured fallback, if
        any, is tried once under its own bound. An empty hit list is a
        success and never triggers the fallback: failure and empty are
        distinct outcomes. Raises ProviderError when every attempted backend
        fails; never leaks raw client exceptions.
        """
        try:
            return self._search_one(self._primary, request, context)
        except ProviderError as primary_exc:
            if self._fallback is None:
                raise
            if context is not None:
                context.add_error(f"fallback: primary '{self._primary}' failed; trying '{self._fallback}'")
                context.fallback_used = True
            try:
                return self._search_one(self._fallback, request, context)
            except ProviderError as fallback_exc:
                if context is not None:
                    context.add_error(f"fallback '{self._fallback}' also failed")
                # Prefer the fallback error (it is the freshest signal), but
                # keep the primary failure visible in diagnostics.
                raise fallback_exc from primary_exc

    def _search_one(
        self, name: str, request: SearchRequest, context: Optional[SearchContext]
    ) -> list[SearchResult]:
        """Run one provider under its bounded timeout. May raise ProviderError."""
        provider = self._providers[name]
        timeout_s = getattr(provider, "timeout_s", DEFAULT_TIMEOUT_S)
        start = time.monotonic()
        box: dict[str, Any] = {}

        def target() -> None:
            try:
                box["result"] = provider.search(request)
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
            raise ProviderError.timeout(
                name,
                f"provider '{name}' timed out after {timeout_s}s",
            )
        if "error" in box:
            err = box["error"]
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
        return list(box.get("result", []))

    def health_check(self) -> dict[str, bool]:
        """Per-provider health keyed by enabled provider name. Never raises."""
        health: dict[str, bool] = {}
        for name, provider in self._providers.items():
            try:
                health[name] = bool(provider.health_check())
            except Exception:
                health[name] = False
        return health

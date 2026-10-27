"""Shared base class for search providers.

Every concrete provider extends :class:`BaseSearchProvider` instead of
implementing :class:`SearchProvider` from scratch. The base owns the
``timeout_s`` option (read in :meth:`configure`, enforced by the manager)
and the helpers that map raw client failures onto the shared
:class:`ProviderError` taxonomy so no backend ever leaks its own
exceptions.
"""

from __future__ import annotations

from abc import ABC
from typing import Any, Callable

from openlens.core.interfaces import (
    ConfigError,
    ProviderError,
    SearchRequest,
    SearchResult,
    SearchProvider,
)

#: Default per-request timeout (seconds) when a provider section omits it.
DEFAULT_TIMEOUT_S = 5.0

#: Hard ceiling for ``timeout_s``; larger values fail startup validation so
#: a misconfigured provider can never hang the pipeline unbounded.
MAX_TIMEOUT_S = 300.0


class BaseSearchProvider(SearchProvider, ABC):
    """Common plumbing for all search backends.

    Subclasses must implement :attr:`name`, :meth:`search`, and
    :meth:`health_check`; they should call ``super().configure(config)``
    first so ``timeout_s`` is validated consistently.
    """

    def __init__(self) -> None:
        self._config: dict[str, Any] = {}
        self.timeout_s: float = DEFAULT_TIMEOUT_S

    def configure(self, config: dict[str, Any]) -> None:
        """Validate shared options. Raise ConfigError if invalid."""
        if not isinstance(config, dict):
            raise ConfigError(
                f"Provider '{self.name}' config must be a mapping, "
                f"got {type(config).__name__}. Startup validation failed."
            )
        timeout = config.get("timeout_s", DEFAULT_TIMEOUT_S)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ConfigError(
                f"Provider '{self.name}': 'timeout_s' must be a number, "
                f"got {timeout!r}. Startup validation failed."
            )
        if not 0 < float(timeout) <= MAX_TIMEOUT_S:
            raise ConfigError(
                f"Provider '{self.name}': 'timeout_s' must be within "
                f"(0, {MAX_TIMEOUT_S}], got {timeout!r}. "
                "Startup validation failed."
            )
        self.timeout_s = float(timeout)
        self._config = dict(config)

    def wrap_error(
        self,
        exc: BaseException,
        *,
        message: str = "",
        retryable: bool = False,
        code: str = ProviderError.UNKNOWN,
    ) -> ProviderError:
        """Wrap a raw client exception as a ProviderError (never re-raised)."""
        if isinstance(exc, ProviderError):
            if not exc.provider_name:
                exc.provider_name = self.name
            return exc
        return ProviderError(
            message or f"{self.name} failed: {exc}",
            provider_name=self.name,
            retryable=retryable,
            code=code,
        )

    def run_bounded(
        self, func: Callable[[], list[SearchResult]], *, timeout_s: float | None = None
    ) -> list[SearchResult]:
        """Run ``func`` with a bounded timeout, mapping overruns to ProviderError.

        The manager normally enforces timeouts; providers that fan out to
        their own clients use this helper so nested calls stay bounded too.
        The worker is a daemon thread, so a hung backend can never stall
        shutdown — the timeout error is raised while the thread is abandoned.
        """
        import threading

        bound = self.timeout_s if timeout_s is None else timeout_s
        box: dict[str, Any] = {}

        def target() -> None:
            try:
                box["result"] = func()
            except BaseException as exc:  # captured and re-raised below
                box["error"] = exc

        worker = threading.Thread(target=target, daemon=True)
        worker.start()
        worker.join(timeout=bound)
        if worker.is_alive():
            raise ProviderError.timeout(
                self.name,
                f"provider '{self.name}' timed out after {bound}s",
            )
        if "error" in box:
            err = box["error"]
            if isinstance(err, ProviderError):
                if not err.provider_name:
                    err.provider_name = self.name
                raise err
            raise self.wrap_error(err)
        return box.get("result", [])

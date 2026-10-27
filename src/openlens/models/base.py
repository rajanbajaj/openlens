"""Shared base class for AI model backends.

Owns the ``timeout_s`` option (validated in :meth:`configure`) and the
helper that maps raw client failures onto the shared :class:`ProviderError`
taxonomy, so no backend ever leaks its own exceptions. Reuses
``ProviderError.provider_name`` to carry the model-backend name.
"""

from __future__ import annotations

from abc import ABC
from typing import Any

from openlens.core.interfaces import ConfigError, ProviderError
from openlens.models.interfaces import AIModelProvider

#: Default per-call timeout (seconds) when a model section omits it.
DEFAULT_TIMEOUT_S = 10.0

#: Hard ceiling for ``timeout_s``; larger values fail startup validation.
MAX_TIMEOUT_S = 300.0


class BaseModelProvider(AIModelProvider, ABC):
    """Common plumbing for all AI backends.

    Subclasses implement :attr:`name`, :meth:`complete`, :meth:`embed`,
    :meth:`classify`, and :meth:`health_check`; they call
    ``super().configure(config)`` first so ``timeout_s`` is consistent.
    """

    def __init__(self) -> None:
        self._config: dict[str, Any] = {}
        self.timeout_s: float = DEFAULT_TIMEOUT_S

    def configure(self, config: dict[str, Any]) -> None:
        """Validate shared options. Raise ConfigError if invalid."""
        if not isinstance(config, dict):
            raise ConfigError(
                f"Model backend '{self.name}' config must be a mapping, "
                f"got {type(config).__name__}. Startup validation failed."
            )
        timeout = config.get("timeout_s", DEFAULT_TIMEOUT_S)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ConfigError(
                f"Model backend '{self.name}': 'timeout_s' must be a number, "
                f"got {timeout!r}. Startup validation failed."
            )
        if not 0 < float(timeout) <= MAX_TIMEOUT_S:
            raise ConfigError(
                f"Model backend '{self.name}': 'timeout_s' must be within "
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

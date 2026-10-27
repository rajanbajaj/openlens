"""Offline-safe stubs for the real AI backends (Claude, Muse, local models).

Each stub validates its own config at startup (so enabling one without
credentials fails loudly) but never touches the network: inference calls
raise :class:`ProviderError` with code ``unavailable`` and ``health_check``
returns False without contacting anything. Real clients arrive after the
skeleton. All three ship **disabled by default** in ``configs/models.yaml``.
"""

from __future__ import annotations

from typing import Any

from openlens.core.interfaces import ConfigError, ProviderError
from openlens.models.base import BaseModelProvider

_UNAVAILABLE_MESSAGE = (
    "live model calls are not implemented in the skeleton; "
    "use the 'fake' backend for offline runs"
)


class _UnavailableModelBackend(BaseModelProvider):
    """Shared stub behavior: configured, but never performs inference."""

    def complete(self, prompt: str, **kwargs: Any) -> str:
        raise ProviderError(
            f"model backend '{self.name}': {_UNAVAILABLE_MESSAGE}",
            provider_name=self.name,
            retryable=False,
            code=ProviderError.UNAVAILABLE,
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise ProviderError(
            f"model backend '{self.name}': {_UNAVAILABLE_MESSAGE}",
            provider_name=self.name,
            retryable=False,
            code=ProviderError.UNAVAILABLE,
        )

    def classify(self, text: str, labels: list[str]) -> str:
        raise ProviderError(
            f"model backend '{self.name}': {_UNAVAILABLE_MESSAGE}",
            provider_name=self.name,
            retryable=False,
            code=ProviderError.UNAVAILABLE,
        )

    def health_check(self) -> bool:
        return False


class ClaudeModelProvider(_UnavailableModelBackend):
    """Anthropic Claude backend stub (disabled by default)."""

    @property
    def name(self) -> str:
        return "claude"

    def configure(self, config: dict[str, Any]) -> None:
        super().configure(config)
        api_key = self._config.get("api_key", "")
        if not isinstance(api_key, str) or not api_key:
            raise ConfigError(
                f"Model backend '{self.name}': 'api_key' is required when the "
                "backend is enabled. Set it via '${VAR}' env resolution. "
                "Startup validation failed."
            )
        self.api_key = api_key
        self.model = str(self._config.get("model", "claude-3-5-sonnet-latest"))


class MuseModelProvider(_UnavailableModelBackend):
    """Muse backend stub (disabled by default)."""

    @property
    def name(self) -> str:
        return "muse"

    def configure(self, config: dict[str, Any]) -> None:
        super().configure(config)
        api_key = self._config.get("api_key", "")
        if not isinstance(api_key, str) or not api_key:
            raise ConfigError(
                f"Model backend '{self.name}': 'api_key' is required when the "
                "backend is enabled. Set it via '${VAR}' env resolution. "
                "Startup validation failed."
            )
        self.api_key = api_key
        self.model = str(self._config.get("model", "muse-3.5"))


class LocalModelProvider(_UnavailableModelBackend):
    """Self-hosted / local model backend stub (disabled by default)."""

    @property
    def name(self) -> str:
        return "local"

    def configure(self, config: dict[str, Any]) -> None:
        super().configure(config)
        endpoint = self._config.get("endpoint", "")
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise ConfigError(
                f"Model backend '{self.name}': 'endpoint' must be a non-empty "
                f"URL, got {endpoint!r}. Startup validation failed."
            )
        self.endpoint = endpoint.strip()
        self.model = str(self._config.get("model", "default"))

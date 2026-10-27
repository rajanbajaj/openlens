"""AIModelProvider interface: the contract every AI backend implements.

Separate from ``SearchProvider`` on purpose: model backends (Muse,
local/self-hosted models, Claude) have their own lifecycle (complete, embed,
classify) and their own config section (``models:`` in ``models.yaml``).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class AIModelProvider(ABC):
    """Plug-and-play AI backend (fake, Muse, local, Claude)."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique backend name used in config and diagnostics."""
        raise NotImplementedError

    @abstractmethod
    def configure(self, config: dict[str, Any]) -> None:
        """Apply this backend's config section. Raise ConfigError if invalid."""
        raise NotImplementedError

    @abstractmethod
    def complete(self, prompt: str, **kwargs: Any) -> str:
        """Generate a completion for ``prompt``. May raise ProviderError."""
        raise NotImplementedError

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed ``texts`` (one vector per input, same order). May raise ProviderError."""
        raise NotImplementedError

    @abstractmethod
    def classify(self, text: str, labels: list[str]) -> str:
        """Pick the best label for ``text``. May raise ProviderError."""
        raise NotImplementedError

    @abstractmethod
    def health_check(self) -> bool:
        """Return True when this backend is reachable and usable. Never raises."""
        raise NotImplementedError

"""Deterministic offline model backend for tests and default development.

``FakeModelProvider`` implements the full :class:`AIModelProvider` interface
with no network, no keys, and no randomness: completions echo a truncated
prompt, embeddings derive deterministically from the input bytes, and
classification prefers keyword matches with a first-label fallback.
"""

from __future__ import annotations

import hashlib
from typing import Any

from openlens.core.interfaces import ConfigError, ProviderError
from openlens.models.base import BaseModelProvider

#: Default embedding width; configurable per backend entry.
DEFAULT_EMBEDDING_DIM = 8

#: Hard ceiling for ``embedding_dim`` so a misconfigured fake stays cheap.
MAX_EMBEDDING_DIM = 64


def _deterministic_vector(text: str, dim: int) -> list[float]:
    """Map ``text`` to a stable pseudo-embedding in [-1, 1]."""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    values: list[float] = []
    counter = 0
    while len(values) < dim:
        block = hashlib.sha256(digest + counter.to_bytes(4, "big")).digest()
        for byte in block:
            values.append((byte / 127.5) - 1.0)
            if len(values) >= dim:
                break
        counter += 1
    return values


class FakeModelProvider(BaseModelProvider):
    """Offline stand-in backend. Always healthy, never calls the network."""

    @property
    def name(self) -> str:
        return "fake"

    def __init__(self) -> None:
        super().__init__()
        self.response_prefix: str = "fake"
        self.embedding_dim: int = DEFAULT_EMBEDDING_DIM

    def configure(self, config: dict[str, Any]) -> None:
        super().configure(config)
        prefix = self._config.get("response_prefix", "fake")
        if not isinstance(prefix, str) or not prefix:
            raise ConfigError(
                f"Model backend '{self.name}': 'response_prefix' must be a "
                f"non-empty string, got {prefix!r}. Startup validation failed."
            )
        dim = self._config.get("embedding_dim", DEFAULT_EMBEDDING_DIM)
        if isinstance(dim, bool) or not isinstance(dim, int):
            raise ConfigError(
                f"Model backend '{self.name}': 'embedding_dim' must be an "
                f"int, got {dim!r}. Startup validation failed."
            )
        if not 1 <= dim <= MAX_EMBEDDING_DIM:
            raise ConfigError(
                f"Model backend '{self.name}': 'embedding_dim' must be within "
                f"[1, {MAX_EMBEDDING_DIM}], got {dim!r}. "
                "Startup validation failed."
            )
        self.response_prefix = prefix
        self.embedding_dim = dim

    def complete(self, prompt: str, **kwargs: Any) -> str:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ProviderError(
                "fake backend requires a non-empty prompt",
                provider_name=self.name,
                retryable=False,
                code=ProviderError.INVALID_REQUEST,
            )
        return f"{self.response_prefix} completion: {prompt.strip()[:120]}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not isinstance(texts, list) or not all(isinstance(t, str) for t in texts):
            raise ProviderError(
                "fake backend requires a list of strings to embed",
                provider_name=self.name,
                retryable=False,
                code=ProviderError.INVALID_REQUEST,
            )
        return [_deterministic_vector(text, self.embedding_dim) for text in texts]

    def classify(self, text: str, labels: list[str]) -> str:
        if not labels:
            raise ProviderError(
                "fake backend requires at least one label",
                provider_name=self.name,
                retryable=False,
                code=ProviderError.INVALID_REQUEST,
            )
        lowered = text.lower()
        for label in labels:
            if label.lower() in lowered:
                return label
        return labels[0]

    def health_check(self) -> bool:
        return True

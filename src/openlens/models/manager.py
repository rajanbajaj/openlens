"""Model manager: startup validation plus primary-backend routing.

Mirrors ``providers/manager.py``: only backends with ``enabled: true`` are
instantiated — disabled entries are never constructed, configured, or
contacted, so the default all-disabled ``models.yaml`` loads with no keys
and no network. Inference calls route to the primary backend.
"""

from __future__ import annotations

from typing import Any

from openlens.core.interfaces import ConfigError
from openlens.models.interfaces import AIModelProvider
from openlens.models.registry import create, register_builtin_models

_RESERVED_KEYS = ("primary",)


class ModelManager:
    """Owns model-backend instances and routes inference to the primary."""

    def __init__(self, backends: dict[str, AIModelProvider], primary: str) -> None:
        if not backends:
            raise ConfigError(
                "No enabled model backends. Enable at least one backend under "
                "'models:'. Startup validation failed."
            )
        if primary not in backends:
            raise ConfigError(
                f"Primary model backend '{primary}' is not enabled. Enabled "
                f"backends: {sorted(backends)}. Startup validation failed."
            )
        self._backends = dict(backends)
        self._primary = primary

    # -- construction ----------------------------------------------------

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "ModelManager":
        """Validate config and instantiate only enabled backends.

        Accepts either the full loaded config (with a top-level ``models:``
        mapping) or the ``models:`` section itself. Raises ConfigError on any
        invalid section; disabled backends are skipped entirely.
        """
        if not isinstance(config, dict):
            raise ConfigError(
                "Model config must be a mapping, "
                f"got {type(config).__name__}. Startup validation failed."
            )
        section = config.get("models", config)
        if not isinstance(section, dict):
            raise ConfigError(
                "Config section 'models' must be a mapping, "
                f"got {type(section).__name__}. Startup validation failed."
            )
        register_builtin_models()
        backends: dict[str, AIModelProvider] = {}
        for entry_name, entry in section.items():
            if entry_name in _RESERVED_KEYS:
                continue
            if not isinstance(entry, dict):
                raise ConfigError(
                    f"Model entry '{entry_name}' must be a mapping, "
                    f"got {type(entry).__name__}. Startup validation failed."
                )
            if not entry.get("enabled", False):
                continue  # disabled: never instantiate, never contact
            model_type = entry.get("type", entry_name)
            if not isinstance(model_type, str) or not model_type:
                raise ConfigError(
                    f"Model entry '{entry_name}': 'type' must be a "
                    "non-empty string. Startup validation failed."
                )
            options = {k: v for k, v in entry.items() if k not in ("enabled", "type")}
            backends[entry_name] = create(model_type, options)
        if not backends:
            raise ConfigError(
                "No enabled model backends. Enable at least one backend under "
                "'models:'. Startup validation failed."
            )
        primary = section.get("primary")
        if primary is None:
            primary = next(iter(backends))
        if not isinstance(primary, str) or primary not in backends:
            raise ConfigError(
                f"Primary model backend {primary!r} is not enabled. Enabled "
                f"backends: {sorted(backends)}. Startup validation failed."
            )
        return cls(backends, primary)

    # -- runtime ---------------------------------------------------------

    @property
    def primary(self) -> str:
        return self._primary

    @property
    def backend_names(self) -> list[str]:
        return sorted(self._backends)

    def get(self, name: str) -> AIModelProvider:
        """Return the instance for ``name``. Raises ConfigError if absent."""
        try:
            return self._backends[name]
        except KeyError:
            raise ConfigError(
                f"Model backend '{name}' is not enabled. Enabled backends: "
                f"{sorted(self._backends)}."
            ) from None

    def complete(self, prompt: str, **kwargs: Any) -> str:
        """Run a completion against the primary backend."""
        return self._backends[self._primary].complete(prompt, **kwargs)

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed texts with the primary backend."""
        return self._backends[self._primary].embed(texts)

    def classify(self, text: str, labels: list[str]) -> str:
        """Classify text with the primary backend."""
        return self._backends[self._primary].classify(text, labels)

    def health_check(self) -> dict[str, bool]:
        """Per-backend health keyed by enabled backend name. Never raises."""
        health: dict[str, bool] = {}
        for name, backend in self._backends.items():
            try:
                health[name] = bool(backend.health_check())
            except Exception:
                health[name] = False
        return health

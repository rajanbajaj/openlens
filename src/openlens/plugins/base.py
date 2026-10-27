"""Convenience base class for plugins: config storage + validation hook."""

from __future__ import annotations

from abc import ABC
from typing import Any

from openlens.core.interfaces import SearchPlugin


class BasePlugin(SearchPlugin, ABC):
    """Stores config on construction; subclasses validate in ``configure``."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config: dict[str, Any] = {}
        self.configure(dict(config or {}))

    def configure(self, config: dict[str, Any]) -> None:
        if not isinstance(config, dict):
            from openlens.core.interfaces import ConfigError

            raise ConfigError(f"Plugin '{self.name}' config must be a mapping.")
        self._config = dict(config)

    @property
    def config(self) -> dict[str, Any]:
        """This plugin's current config (copy)."""
        return dict(self._config)

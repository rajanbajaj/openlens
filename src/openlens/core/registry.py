"""Plugin registry: name -> SearchPlugin class resolution and construction.

Plugins are referenced either by a registered short name (including the
built-ins below) or by an explicit ``module:Class`` import path. Construction
always goes through :meth:`PluginRegistry.create` so config errors surface as
``ConfigError`` at startup, never as import-time tracebacks mid-request.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

from openlens.core.interfaces import ConfigError, SearchPlugin

BUILTIN_PLUGINS: dict[str, str] = {
    "safety-filter": "openlens.plugins.safety_filter:SafetyFilter",
    "trust-enricher": "openlens.plugins.trust_enricher:TrustEnricher",
    "baseline-scorer": "openlens.plugins.relevance_scorer:BaselineRelevanceScorer",
    "ranker": "openlens.ranking.ranker:Ranker",
}


def _class_plugin_name(plugin_cls: type[SearchPlugin]) -> str | None:
    """Read ``name`` off the class itself. None when absent or non-string."""
    raw = getattr(plugin_cls, "name", None)
    return raw if isinstance(raw, str) else None


class PluginRegistry:
    """Resolves plugin names/paths to configured plugin instances."""

    def __init__(self) -> None:
        self._classes: dict[str, type[SearchPlugin]] = {}

    def register(self, plugin_cls: type[SearchPlugin]) -> type[SearchPlugin]:
        """Register a plugin class under its ``name``. Raises ConfigError."""
        if not isinstance(plugin_cls, type) or not issubclass(plugin_cls, SearchPlugin):
            raise ConfigError(
                f"Cannot register {plugin_cls!r}: not a SearchPlugin subclass."
            )
        # Instantiating `name` requires an instance; read it off a throwaway
        # only when the class exposes it without construction is fragile, so
        # resolve via a lightweight probe: classes must expose `name` either
        # way, but most define it as a property. Construct with no args when
        # possible, else fall back to the class attribute.
        plugin_name: str | None = None
        try:
            probe = plugin_cls()  # type: ignore[call-arg]
            if isinstance(probe.name, str):
                plugin_name = probe.name
            else:
                plugin_name = _class_plugin_name(plugin_cls)
        except Exception:
            plugin_name = _class_plugin_name(plugin_cls)
        if plugin_name is None:
            raise ConfigError(
                f"Cannot register {plugin_cls!r}: could not determine plugin name."
            )
        if plugin_name in self._classes or plugin_name in BUILTIN_PLUGINS:
            raise ConfigError(
                f"Duplicate plugin name '{plugin_name}'. Startup validation failed."
            )
        self._classes[plugin_name] = plugin_cls
        return plugin_cls

    def registered_names(self) -> list[str]:
        """All resolvable short names (built-ins plus registered)."""
        return sorted(set(BUILTIN_PLUGINS) | set(self._classes))

    def resolve(self, ref: str) -> type[SearchPlugin]:
        """Resolve a short name or ``module:Class`` path to a plugin class."""
        if ref in self._classes:
            return self._classes[ref]
        path = BUILTIN_PLUGINS.get(ref, ref)
        module_name, sep, class_name = path.partition(":")
        if not sep or not module_name or not class_name:
            raise ConfigError(
                f"Unknown plugin '{ref}'. Known: {', '.join(self.registered_names())}. "
                "Use a registered name or 'module:Class' path."
            )
        try:
            module = import_module(module_name)
        except ImportError as exc:
            raise ConfigError(
                f"Cannot import plugin module '{module_name}' for '{ref}': {exc}"
            ) from exc
        try:
            plugin_cls = getattr(module, class_name)
        except AttributeError as exc:
            raise ConfigError(
                f"Plugin class '{class_name}' not found in '{module_name}' for '{ref}'."
            ) from exc
        if not isinstance(plugin_cls, type) or not issubclass(plugin_cls, SearchPlugin):
            raise ConfigError(
                f"Plugin '{ref}' ({path}) is not a SearchPlugin subclass."
            )
        return plugin_cls

    def create(
        self,
        name: str,
        ref: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> SearchPlugin:
        """Instantiate and configure one plugin. Raises ConfigError."""
        plugin_cls = self.resolve(ref or name)
        try:
            plugin = plugin_cls()
        except Exception as exc:
            raise ConfigError(
                f"Cannot instantiate plugin '{name}': {exc}. Startup validation failed."
            ) from exc
        try:
            plugin.configure(dict(config or {}))
        except ConfigError:
            raise
        except Exception as exc:
            raise ConfigError(
                f"Invalid config for plugin '{name}': {exc}. Startup validation failed."
            ) from exc
        return plugin

    def build(self, entries: list[dict[str, Any]]) -> list[SearchPlugin]:
        """Build plugins from config entries.

        Each entry: ``{name, class?, enabled?, config?}``. Disabled entries are
        skipped. Raises ConfigError on missing names or bad entries.
        """
        plugins: list[SearchPlugin] = []
        for entry in entries:
            if not isinstance(entry, dict) or "name" not in entry:
                raise ConfigError(
                    f"Invalid plugin entry {entry!r}: each entry needs a 'name'."
                )
            if entry.get("enabled", True) is False:
                continue
            if "config" in entry and not isinstance(entry["config"], dict):
                raise ConfigError(
                    f"Invalid config for plugin '{entry['name']}': "
                    + "'config' must be a mapping."
                )
            plugins.append(
                self.create(
                    name=entry["name"],
                    ref=entry.get("class"),
                    config=entry.get("config"),
                )
            )
        return plugins

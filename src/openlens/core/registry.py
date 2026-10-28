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
    "privacy-filter": "openlens.plugins.privacy_filter:PrivacyFilter",
    "user-restrictions": "openlens.plugins.user_restrictions:UserRestrictionsFilter",
    "trust-enricher": "openlens.plugins.trust_enricher:TrustEnricher",
    "baseline-scorer": "openlens.plugins.relevance_scorer:BaselineRelevanceScorer",
    "trust-scorer": "openlens.plugins.trust_scorer:TrustScorer",
    "freshness-scorer": "openlens.plugins.freshness_scorer:FreshnessScorer",
    "preference-scorer": "openlens.plugins.preference_scorer:PreferenceScorer",
    "attribute-enricher": "openlens.plugins.attribute_enricher:AttributeEnricher",
    "diversity-reranker": "openlens.plugins.diversity_reranker:DiversityReranker",
    "news-reranker": "openlens.plugins.news_reranker:NewsReranker",
    "commerce-reranker": "openlens.plugins.commerce_reranker:CommerceReranker",
    "ranker": "openlens.ranking.ranker:Ranker",
}

#: Mandated FILTERING order for the mandatory filter pack (filter-pack
#: plan Step 4): user restrictions run first, then the blocklist/allowlist
#: safety filter, then the privacy filter. ``PluginRegistry.build`` turns
#: this chain into ``dependencies`` declarations on the built instances
#: (each filter depends on its predecessor *when that predecessor is also
#: built*), so the pipeline's topological sort — not config declaration
#: order — enforces the execution order. Restricting dependencies to
#: built predecessors keeps single-filter pipelines constructible: the
#: pipeline rejects unknown dependency names at startup.
FILTER_PACK_ORDER: tuple[str, ...] = (
    "user-restrictions",
    "safety-filter",
    "privacy-filter",
)

_FILTER_PACK_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    name: (FILTER_PACK_ORDER[index - 1],)
    for index, name in enumerate(FILTER_PACK_ORDER)
    if index
}

_ORDERED_CLASS_CACHE: dict[
    tuple[type[SearchPlugin], tuple[str, ...]], type[SearchPlugin]
] = {}


def _with_dependencies(
    plugin: SearchPlugin, dependencies: tuple[str, ...]
) -> SearchPlugin:
    """Return ``plugin`` reclassed so ``dependencies`` reports the chain.

    The filter classes deliberately declare no dependencies of their own
    (each must stay usable standalone in a pipeline), so the registry —
    the one place that knows which pack members are being built together
    — stamps the mandated chain onto the built instances. Reclassing to a
    generated subclass keeps ``isinstance`` checks, ``name``, config, and
    ``run`` behavior identical; only ``dependencies`` is overridden.
    """
    if not dependencies or tuple(plugin.dependencies) == dependencies:
        return plugin
    key = (type(plugin), dependencies)
    ordered_cls = _ORDERED_CLASS_CACHE.get(key)
    if ordered_cls is None:

        def _get_dependencies(
            _self: SearchPlugin, _deps: tuple[str, ...] = dependencies
        ) -> tuple[str, ...]:
            return _deps

        ordered_cls = type(
            f"Ordered{type(plugin).__name__}",
            (type(plugin),),
            {"dependencies": property(_get_dependencies)},
        )
        _ORDERED_CLASS_CACHE[key] = ordered_cls
    # Same layout as the original class (subclass adds no fields), so
    # reclassing the live instance is safe and preserves its state.
    plugin.__class__ = ordered_cls  # type: ignore[misc]
    return plugin


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
        return self._apply_filter_pack_order(plugins)

    @staticmethod
    def _apply_filter_pack_order(plugins: list[SearchPlugin]) -> list[SearchPlugin]:
        """Stamp the mandated FILTERING chain onto built filter instances.

        Each filter-pack member declares a dependency on its immediate
        predecessor in ``FILTER_PACK_ORDER`` when that predecessor is in
        the same build; members built without their predecessor stay
        dependency-free so standalone pipelines still validate.
        """
        built_names = {plugin.name for plugin in plugins}
        ordered: list[SearchPlugin] = []
        for plugin in plugins:
            dependencies = tuple(
                dep
                for dep in _FILTER_PACK_DEPENDENCIES.get(plugin.name, ())
                if dep in built_names
            )
            ordered.append(_with_dependencies(plugin, dependencies))
        return ordered

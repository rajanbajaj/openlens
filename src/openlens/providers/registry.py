"""Provider-type registry: type name -> SearchProvider subclass.

The registry maps *type* names (``opensearch``, ``bing``, ``custom_index``)
to classes, never to instances. The manager instantiates one object per
*enabled* config entry, so two entries may share a type. Builtin types are
registered lazily via :func:`register_builtin_providers` to avoid import
cycles and to keep hard dependencies (e.g. ``opensearch-py``) out of the
import path until actually used.
"""

from __future__ import annotations

from typing import Any

from openlens.core.interfaces import ConfigError, SearchProvider

_REGISTRY: dict[str, type[SearchProvider]] = {}
_BUILTINS_REGISTERED = False


def register(provider_type: str, cls: type[SearchProvider]) -> None:
    """Register ``cls`` under ``provider_type``. Raises ConfigError if invalid."""
    if not provider_type or not isinstance(provider_type, str):
        raise ConfigError(
            f"Provider type name must be a non-empty string, got {provider_type!r}."
        )
    if not (isinstance(cls, type) and issubclass(cls, SearchProvider)):
        raise ConfigError(
            f"Provider type '{provider_type}' must be a SearchProvider subclass, "
            f"got {cls!r}. Startup validation failed."
        )
    if getattr(cls, "__abstractmethods__", frozenset()):
        raise ConfigError(
            f"Provider type '{provider_type}' ({cls.__name__}) is still abstract: "
            f"{sorted(cls.__abstractmethods__)}. Startup validation failed."
        )
    _REGISTRY[provider_type] = cls


def unregister(provider_type: str) -> None:
    """Remove a registration (used by tests). Missing names are ignored."""
    _REGISTRY.pop(provider_type, None)


def get(provider_type: str) -> type[SearchProvider]:
    """Return the class for ``provider_type``. Raises ConfigError if unknown."""
    try:
        return _REGISTRY[provider_type]
    except KeyError:
        known = sorted(_REGISTRY)
        raise ConfigError(
            f"Unknown provider type '{provider_type}'. "
            f"Known types: {known or ['<none registered>']}. "
            "Startup validation failed."
        ) from None


def available() -> list[str]:
    """Return all registered provider-type names, sorted."""
    return sorted(_REGISTRY)


def create(provider_type: str, config: dict[str, Any]) -> SearchProvider:
    """Instantiate and configure a provider. Raises ConfigError if invalid."""
    cls = get(provider_type)
    instance = cls()
    instance.configure(config)
    return instance


def register_builtin_providers(*, force: bool = False) -> None:
    """Register the skeleton's builtin provider types (idempotent)."""
    global _BUILTINS_REGISTERED
    if _BUILTINS_REGISTERED and not force:
        return
    from openlens.providers.external import BingSearchProvider
    from openlens.providers.opensearch import OpenSearchProvider

    register("opensearch", OpenSearchProvider)
    register("bing", BingSearchProvider)

    # The custom-index backend is owned by another wave; register it when its
    # module exists so configs can reference it without a hard dependency.
    try:
        from openlens.providers.custom_index import CustomIndexProvider

        register("custom_index", CustomIndexProvider)
    except ImportError:
        pass
    _BUILTINS_REGISTERED = True

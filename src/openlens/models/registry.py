"""Model-type registry: type name -> AIModelProvider subclass.

Mirrors ``providers/registry.py``: the registry maps *type* names (``fake``,
``claude``, ``muse``, ``local``, ``openai_compat``) to classes, never to
instances. The manager
instantiates one object per *enabled* config entry. Builtins register lazily
via :func:`register_builtin_models` to keep optional client libraries out of
the import path until actually used.
"""

from __future__ import annotations

from typing import Any

from openlens.core.interfaces import ConfigError
from openlens.models.interfaces import AIModelProvider

_REGISTRY: dict[str, type[AIModelProvider]] = {}
_BUILTINS_REGISTERED = False


def register(model_type: str, cls: type[AIModelProvider]) -> None:
    """Register ``cls`` under ``model_type``. Raises ConfigError if invalid."""
    if not model_type or not isinstance(model_type, str):
        raise ConfigError(
            f"Model type name must be a non-empty string, got {model_type!r}."
        )
    if not (isinstance(cls, type) and issubclass(cls, AIModelProvider)):
        raise ConfigError(
            f"Model type '{model_type}' must be an AIModelProvider subclass, "
            f"got {cls!r}. Startup validation failed."
        )
    if getattr(cls, "__abstractmethods__", frozenset()):
        raise ConfigError(
            f"Model type '{model_type}' ({cls.__name__}) is still abstract: "
            f"{sorted(cls.__abstractmethods__)}. Startup validation failed."
        )
    _REGISTRY[model_type] = cls


def unregister(model_type: str) -> None:
    """Remove a registration (used by tests). Missing names are ignored."""
    _REGISTRY.pop(model_type, None)


def get(model_type: str) -> type[AIModelProvider]:
    """Return the class for ``model_type``. Raises ConfigError if unknown."""
    try:
        return _REGISTRY[model_type]
    except KeyError:
        known = sorted(_REGISTRY)
        raise ConfigError(
            f"Unknown model type '{model_type}'. "
            f"Known types: {known or ['<none registered>']}. "
            "Startup validation failed."
        ) from None


def available() -> list[str]:
    """Return all registered model-type names, sorted."""
    return sorted(_REGISTRY)


def create(model_type: str, config: dict[str, Any]) -> AIModelProvider:
    """Instantiate and configure a model backend. Raises ConfigError if invalid."""
    cls = get(model_type)
    instance = cls()
    instance.configure(config)
    return instance


def register_builtin_models(*, force: bool = False) -> None:
    """Register the skeleton's builtin model types (idempotent)."""
    global _BUILTINS_REGISTERED
    if _BUILTINS_REGISTERED and not force:
        return
    from openlens.models.backends import (
        ClaudeModelProvider,
        LocalModelProvider,
        MuseModelProvider,
    )
    from openlens.models.fake import FakeModelProvider
    from openlens.models.openai_compat import OpenAICompatModelProvider

    register("fake", FakeModelProvider)
    register("claude", ClaudeModelProvider)
    register("muse", MuseModelProvider)
    register("local", LocalModelProvider)
    register("openai_compat", OpenAICompatModelProvider)
    _BUILTINS_REGISTERED = True

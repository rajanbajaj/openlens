"""OpenLens API entrypoint: `uvicorn openlens.main:app`.

`create_app` builds the FastAPI app and attaches the search router plus the
shared error envelope. Pass an `Engine` explicitly to serve search (tests do
this), or rely on the module-level `app`, which boots the real Engine from
`configs/default.yaml` via :func:`build_engine` (leaving the engine unset —
HTTP 503 — when that config cannot boot, so importing this module never
crashes).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from openlens.api.errors import register_exception_handlers
from openlens.api.routes import router as search_router
from openlens.config import load_config
from openlens.core.engine import SearchEngine
from openlens.core.interfaces import ConfigError, Engine
from openlens.providers.manager import ProviderManager
from openlens.retrieval.query_processor import extract_query_section

DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent.parent / "configs" / "default.yaml"
)

_PROVIDER_RESERVED = ("strategy", "primary", "fallback", "enabled")


def _candidate_config_paths(path: str | Path | None) -> list[Path]:
    """Ordered config locations: explicit path, $OPENLENS_CONFIG, source tree, CWD.

    The installed package (site-packages) has no sibling configs/, so after
    the source-tree default also try ./configs/default.yaml — the docker
    WORKDIR layout.
    """
    if path is not None:
        return [Path(path)]
    candidates = [DEFAULT_CONFIG_PATH, Path.cwd() / "configs" / "default.yaml"]
    env = os.environ.get("OPENLENS_CONFIG")
    if env:
        candidates.insert(0, Path(env))
    return candidates


def load_app_config(path: str | Path | None = None) -> dict[str, Any]:
    """Best-effort load of the app config; missing file yields {}."""
    for candidate in _candidate_config_paths(path):
        try:
            config = load_config(candidate)
        except (ConfigError, OSError):
            continue
        if isinstance(config, dict) and config:
            return config
    return {}


def _manager_section(config: dict[str, Any]) -> dict[str, Any]:
    """Translate the app `providers:` section to `ProviderManager` shape.

    Accepts the registry shape (`{name: {enabled, type, ...}}` with optional
    `primary:` / `fallback:` / `strategy:`) and the shorthand list form
    (`enabled: [names]`, entries defaulting their type to the entry name).
    """
    section = config.get("providers", {})
    if not isinstance(section, dict):
        raise ConfigError("Config section 'providers' must be a mapping.")
    enabled_list = section.get("enabled")
    names: list[str] | None = None
    if isinstance(enabled_list, list):
        names = [str(n) for n in enabled_list]
    normalized: dict[str, Any] = {}
    for key, value in section.items():
        if key in ("strategy", "primary", "fallback"):
            normalized[key] = value
        elif key == "enabled":
            continue
        elif isinstance(value, dict):
            entry = dict(value)
            if names is not None:
                entry.setdefault("enabled", key in names)
            normalized[key] = entry
        else:
            raise ConfigError(
                f"Provider entry '{key}' must be a mapping. Startup validation failed."
            )
    if names is not None:
        for name in names:
            if name not in normalized:
                raise ConfigError(
                    f"Enabled provider '{name}' has no config entry under "
                    "'providers:'. Startup validation failed."
                )
    return {"providers": normalized}


def _plugin_entries(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize the app `plugins:` section to registry entries.

    Accepts a list of `{name, class?, enabled?, config?}` entries, a mapping
    of `{name: config}`, or a mapping with an `enabled: [names]` list.
    """
    section = config.get("plugins", [])
    if isinstance(section, list):
        return list(section)
    if not isinstance(section, dict):
        raise ConfigError("Config section 'plugins' must be a list or a mapping.")
    enabled = section.get("enabled", None)
    if isinstance(enabled, list):
        enabled_names = [str(n) for n in enabled]
        entries: list[dict[str, Any]] = []
        for name in enabled_names:
            entry = {"name": name, "enabled": True}
            per_plugin = section.get(name)
            if isinstance(per_plugin, dict):
                if per_plugin.get("enabled") is False:
                    entry["enabled"] = False
                if "config" in per_plugin and isinstance(per_plugin["config"], dict):
                    entry["config"] = per_plugin["config"]
                elif any(k != "enabled" for k in per_plugin):
                    entry["config"] = {
                        k: v for k, v in per_plugin.items() if k != "enabled"
                    }
            entries.append(entry)
        return entries
    return [
        {"name": name, "config": dict(value) if isinstance(value, dict) else {}}
        for name, value in section.items()
    ]


def _ranking_config(config: dict[str, Any]) -> dict[str, Any]:
    """Normalize the app `ranking:` section to `Engine.configure` shape."""
    section = config.get("ranking", {})
    if not isinstance(section, dict):
        raise ConfigError("Config section 'ranking' must be a mapping.")
    profile = section.get("default_profile", section.get("default_mode", "default"))
    if not isinstance(profile, str) or not profile:
        raise ConfigError("Ranking 'default_profile' must be a non-empty string.")
    return {"default_profile": profile}


def build_engine(config: dict[str, Any]) -> SearchEngine:
    """Wire the real Engine from app config: providers, plugins, ranking.

    Provider order is primary first, then the configured fallback, so the
    engine's bounded fallback (failure moves on, empty never does) follows
    the YAML without core edits.

    Raises:
        ConfigError: the config is missing, malformed, or names no enabled
            provider.
    """
    if not isinstance(config, dict) or not config:
        raise ConfigError("Cannot build engine: config is empty or not a mapping.")
    # SQLite file backends need their directory to exist before the provider
    # constructs its store; create it here so boot never fails on a fresh
    # checkout (data/ is gitignored by design).
    section = config.get("providers", {})
    if isinstance(section, dict):
        for entry in section.values():
            if isinstance(entry, dict):
                db_path = entry.get("db_path")
                if isinstance(db_path, str) and db_path not in (":memory:", ""):
                    parent = os.path.dirname(os.path.abspath(db_path))
                    if parent:
                        os.makedirs(parent, exist_ok=True)
    manager = ProviderManager.from_config(_manager_section(config))
    ordered_names = [manager.primary]
    if manager.fallback is not None:
        ordered_names.append(manager.fallback)
    providers = [manager.get(name) for name in ordered_names]
    entries = _plugin_entries(config)
    engine = SearchEngine(providers=providers, plugins=[])
    engine.configure(
        {
            "plugins": entries,
            "ranking": _ranking_config(config),
            "query": extract_query_section(config),
        }
    )
    return engine


def create_app(engine: Optional[Engine] = None) -> FastAPI:
    app = FastAPI(title="OpenLens", version="0.1.0")
    # Browser frontends call the API cross-origin in dev (vite :5173 ->
    # backend :8000); answer preflights for local origins only.
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization"],
    )
    app.state.engine = engine
    app.state.config = load_app_config()
    register_exception_handlers(app)
    app.include_router(search_router)

    @app.get("/healthz", tags=["health"])
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


def _boot_default_engine() -> Optional[Engine]:
    """Build the engine from the default config; None when it cannot boot."""
    config = load_app_config()
    if not config:
        return None
    try:
        return build_engine(config)
    except Exception:  # noqa: BLE001 — import must never crash; API reports 503
        return None


app = create_app()
_booted = _boot_default_engine()
if _booted is not None:
    app.state.engine = _booted
    try:
        app.state.config = load_app_config() or {}
    except (ConfigError, OSError):
        pass

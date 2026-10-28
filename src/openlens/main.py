"""OpenLens API entrypoint: `uvicorn openlens.main:app`.

`create_app` builds the FastAPI app and attaches the search router plus the
shared error envelope. Pass an `Engine` explicitly to serve search (tests do
this), or rely on the module-level `app`, which boots the real Engine from
`configs/default.yaml` via :func:`build_engine` (leaving the engine unset —
HTTP 503 — when that config cannot boot, so importing this module never
crashes).
"""

from __future__ import annotations

import logging as stdlib_logging
import os
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, Response

from openlens.api.errors import register_exception_handlers
from openlens.api.routes import router as search_router
from openlens.cache.factory import (
    DEFAULT_TTL_S,
    build_cache_provider,
    validate_cache_section,
)
from openlens.config import load_config
from openlens.core.context import SearchContext
from openlens.core.engine import SearchEngine
from openlens.core.interfaces import (
    ConfigError,
    Engine,
    ProviderError,
    SearchRequest,
    SearchResponse,
)
from openlens.observability import logging as obs_logging
from openlens.observability.metrics import get_registry
from openlens.observability.request_id import REQUEST_ID_HEADER, resolve_request_id
from openlens.providers.manager import STRATEGY_SECTION_KEYS, ProviderManager
from openlens.ranking.profiles import RankingProfile, load_profiles
from openlens.retrieval.query_processor import extract_query_section

DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent.parent / "configs" / "default.yaml"
)
DEFAULT_MODELS_PATH = DEFAULT_CONFIG_PATH.parent / "models.yaml"

_PROVIDER_RESERVED = ("strategy", "primary", "fallback", "enabled")


def _storage_section(config: dict[str, Any]) -> dict[str, Any]:
    """Return the normalized top-level `storage:` section ({} when absent).

    The `storage.backend` key is the single backend switch
    (`sqlite` | `mysql` | `postgres`, default `sqlite`); per-backend
    connection fields live under `storage.sqlite` / `storage.mysql` /
    `storage.postgres`. Raises ConfigError on malformed sections or
    unknown backend names so boot fails loudly at startup.
    """
    from openlens.providers.custom_index import SUPPORTED_BACKENDS

    section = config.get("storage", None)
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise ConfigError("Config section 'storage' must be a mapping.")
    backend = section.get("backend", "sqlite")
    if not isinstance(backend, str) or backend not in SUPPORTED_BACKENDS:
        raise ConfigError(
            f"Unknown storage backend {backend!r}. "
            f"Supported: {sorted(SUPPORTED_BACKENDS)}. Startup validation failed."
        )
    for key in ("sqlite", "mysql", "postgres"):
        subsection = section.get(key, None)
        if subsection is not None and not isinstance(subsection, dict):
            raise ConfigError(
                f"Config section 'storage.{key}' must be a mapping. "
                "Startup validation failed."
            )
    return dict(section)


def _sqlite_db_path(storage: dict[str, Any]) -> str | None:
    """Resolve the SQLite file path from `storage.sqlite` (or None)."""
    if storage.get("backend", "sqlite") != "sqlite":
        return None
    sqlite_cfg = storage.get("sqlite", {})
    if not isinstance(sqlite_cfg, dict):
        return None
    db_path = sqlite_cfg.get("db_path")
    if isinstance(db_path, str) and db_path and db_path != ":memory:":
        return db_path
    return None


def _ensure_parent_dir(db_path: str) -> None:
    parent = os.path.dirname(os.path.abspath(db_path))
    if parent:
        os.makedirs(parent, exist_ok=True)


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


def _models_candidates_for(app_candidate: Path) -> list[Path]:
    """Models files to try alongside an app config, in priority order."""
    candidates = [
        app_candidate.parent / "models.yaml",
        DEFAULT_MODELS_PATH,
        Path.cwd() / "configs" / "models.yaml",
    ]
    seen: set[Path] = set()
    unique: list[Path] = []
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            unique.append(candidate)
    return unique


def _load_models_section(app_candidate: Path) -> dict[str, Any] | None:
    """Load the ``models:`` section from the sibling ``models.yaml``.

    ``load_config`` resolves ``${VAR}`` per-file, so secrets in the second
    file get identical handling to ``default.yaml``. Missing/invalid files
    yield ``None`` (no AI) rather than failing the app-config load.
    """
    for candidate in _models_candidates_for(app_candidate):
        if not candidate.is_file():
            continue
        try:
            data = load_config(candidate)
        except (ConfigError, OSError):
            continue
        section = data.get("models") if isinstance(data, dict) else None
        if isinstance(section, dict):
            return section
    return None


def load_models_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load ``models.yaml`` as a second file; missing file yields {}."""
    if path is not None:
        try:
            data = load_config(Path(path))
        except (ConfigError, OSError):
            return {}
        section = data.get("models") if isinstance(data, dict) else None
        return {"models": section} if isinstance(section, dict) else {}
    section = _load_models_section(DEFAULT_CONFIG_PATH)
    return {"models": section} if section is not None else {}


def load_app_config(path: str | Path | None = None) -> dict[str, Any]:
    """Best-effort load of the app config; missing file yields {}.

    When the loaded config has no ``models:`` section, ``configs/models.yaml``
    is loaded as a second file alongside ``default.yaml`` and merged in.
    """
    for candidate in _candidate_config_paths(path):
        try:
            config = load_config(candidate)
        except (ConfigError, OSError):
            continue
        if isinstance(config, dict) and config:
            if "models" not in config:
                section = _load_models_section(candidate)
                if section is not None:
                    config = {**config, "models": section}
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
    # The top-level `storage:` section is the single backend switch for
    # custom_index providers: forward it into each such entry (an entry
    # that already carries its own `storage:` mapping keeps it).
    storage = config.get("storage", None)
    normalized: dict[str, Any] = {}
    for key, value in section.items():
        if key in STRATEGY_SECTION_KEYS:
            normalized[key] = value
        elif key == "enabled":
            continue
        elif isinstance(value, dict):
            entry = dict(value)
            if names is not None:
                entry.setdefault("enabled", key in names)
            if (
                storage is not None
                and "storage" not in entry
                and entry.get("type", key) == "custom_index"
            ):
                entry["storage"] = storage
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


def ranking_config_candidates() -> list[Path]:
    """Where to look for the ranking profiles file (source tree, then CWD)."""
    return [
        DEFAULT_CONFIG_PATH.parent / "ranking.yaml",
        Path.cwd() / "configs" / "ranking.yaml",
    ]


def load_ranking_profiles(
    candidates: list[Path] | None = None,
) -> dict[str, RankingProfile] | None:
    """Load ``ranking.yaml`` profiles from the first existing candidate.

    Returns ``None`` when no candidate exists so the engine falls back to
    builtins. A present-but-invalid file still fails loudly via ConfigError.
    """
    resolved = candidates if candidates is not None else ranking_config_candidates()
    for candidate in resolved:
        if candidate.is_file():
            return load_profiles(candidate)
    return None


def _ranking_config(config: dict[str, Any]) -> dict[str, Any]:
    """Normalize the app `ranking:` section to `Engine.configure` shape."""
    section = config.get("ranking", {})
    if not isinstance(section, dict):
        raise ConfigError("Config section 'ranking' must be a mapping.")
    profile = section.get("default_profile", section.get("default_mode", "default"))
    if not isinstance(profile, str) or not profile:
        raise ConfigError("Ranking 'default_profile' must be a non-empty string.")
    normalized: dict[str, Any] = {"default_profile": profile}
    file_profiles = load_ranking_profiles()
    inline = section.get("profiles")
    if inline is not None and not isinstance(inline, dict):
        raise ConfigError("Config section 'ranking.profiles' must be a mapping.")
    if file_profiles is not None or isinstance(inline, dict):
        merged: dict[str, Any] = dict(file_profiles) if file_profiles else {}
        if isinstance(inline, dict):
            merged.update(inline)
        normalized["profiles"] = merged
    return normalized


def build_engine(config: dict[str, Any]) -> SearchEngine:
    """Wire the real Engine from app config: providers, plugins, ranking.

    Provider selection is owned by the ProviderManager (single, fallback,
    or federated per the YAML strategy); the engine delegates to it, which
    also puts the manager's per-provider timeout bound on the live path.

    Raises:
        ConfigError: the config is missing, malformed, or names no enabled
            provider.
    """
    if not isinstance(config, dict) or not config:
        raise ConfigError("Cannot build engine: config is empty or not a mapping.")
    # Validate the `storage:` section first so an unknown backend or a
    # malformed section fails loudly at startup, before any provider boots.
    storage = _storage_section(config)
    # SQLite file backends need their directory to exist before the provider
    # constructs its store; create it here so boot never fails on a fresh
    # checkout (data/ is gitignored by design). The `storage.sqlite.db_path`
    # field is honored first, then the legacy provider-entry `db_path`.
    sqlite_path = _sqlite_db_path(storage)
    if sqlite_path is not None:
        _ensure_parent_dir(sqlite_path)
    section = config.get("providers", {})
    if isinstance(section, dict):
        for entry in section.values():
            if isinstance(entry, dict):
                db_path = entry.get("db_path")
                if isinstance(db_path, str) and db_path not in (":memory:", ""):
                    _ensure_parent_dir(db_path)
    effective = config
    if "models" not in effective:
        section = _load_models_section(DEFAULT_CONFIG_PATH)
        if section is not None:
            effective = {**config, "models": section}
    manager = ProviderManager.from_config(_manager_section(effective))
    entries = _plugin_entries(effective)
    cache_section = validate_cache_section(effective.get("cache", None))
    cache_ttl_s = cache_section.get("ttl_s", DEFAULT_TTL_S)
    cache_provider = None
    if cache_section.get("enabled", True):
        if cache_section.get("backend", "memory") == "sqlite":
            sqlite_cfg = cache_section.get("sqlite", {})
            if isinstance(sqlite_cfg, dict):
                cache_path = sqlite_cfg.get("path")
                if (
                    isinstance(cache_path, str)
                    and cache_path
                    and cache_path != ":memory:"
                ):
                    _ensure_parent_dir(cache_path)
        try:
            # Absent section means memory defaults (cache on). Malformed
            # values raise ConfigError here via the factory validators.
            cache_provider = build_cache_provider(cache_section or None)
        except ProviderError as exc:
            if exc.code == ProviderError.UNAVAILABLE:
                stdlib_logging.getLogger("openlens.cache").warning(
                    "cache backend unavailable (%s); continuing uncached", exc
                )
                cache_provider = None
            else:
                raise
    engine = SearchEngine.from_manager(
        manager, plugins=[], cache=cache_provider, cache_ttl_s=cache_ttl_s
    )
    engine_config: dict[str, Any] = {
        "plugins": entries,
        "ranking": _ranking_config(effective),
        "query": extract_query_section(effective),
    }
    if "models" in effective:
        engine_config["models"] = effective["models"]
    engine.configure(engine_config)
    return engine


#: Request id for the in-flight HTTP request, set by the middleware
#: before the route runs and read by :class:`_ObservedEngine` after
#: ``engine.search`` returns. Empty outside a request (direct engine use).
_REQUEST_ID_CTX: ContextVar[str] = ContextVar("openlens_request_id", default="")


def _metrics_enabled(config: dict[str, Any]) -> bool:
    """Return whether ``metrics.enabled`` is truthy (default False)."""
    section = config.get("metrics", {})
    if not isinstance(section, dict):
        return False
    return bool(section.get("enabled", False))


def _context_from_diagnostics(
    request: SearchRequest,
    diagnostics: dict[str, Any],
    request_id: str,
) -> SearchContext:
    """Rebuild a context from a response diagnostics payload for logging.

    The engine owns the live context; this reconstructs an equivalent one
    from the fixed-shape diagnostics dict so the per-request INFO record is
    emitted through the single tested :func:`log_search_completed` path.
    Malformed entries are skipped — logging must never fail a request.
    """
    context = SearchContext(request=request, request_id=request_id)
    latencies = diagnostics.get("provider_latencies_ms", {})
    if isinstance(latencies, dict):
        for name, latency in latencies.items():
            if isinstance(latency, (int, float)) and not isinstance(latency, bool):
                context.record_provider_latency(str(name), float(latency))
    counts = diagnostics.get("provider_result_counts", {})
    if isinstance(counts, dict):
        for name, count in counts.items():
            if isinstance(count, int) and not isinstance(count, bool):
                context.record_provider_results(str(name), count)
    executions = diagnostics.get("plugin_executions", [])
    if isinstance(executions, list):
        for entry in executions:
            if not isinstance(entry, dict):
                continue
            plugin = entry.get("plugin")
            stage = entry.get("stage")
            status = entry.get("status", "unknown")
            if not (
                isinstance(plugin, str)
                and isinstance(stage, str)
                and isinstance(status, str)
            ):
                continue
            raw_latency = entry.get("latency_ms", 0.0)
            if isinstance(raw_latency, (int, float)) and not isinstance(
                raw_latency, bool
            ):
                latency_ms = float(raw_latency)
            else:
                latency_ms = 0.0
            error = entry.get("error")
            context.record_plugin_execution(
                plugin,
                stage,
                status,
                latency_ms,
                error if error is None or isinstance(error, str) else str(error),
            )
    errors = diagnostics.get("errors", [])
    if isinstance(errors, list):
        for item in errors:
            if isinstance(item, str):
                context.add_error(item)
    context.fallback_used = bool(diagnostics.get("fallback_used", False))
    return context


def _log_search(
    request: SearchRequest, response: SearchResponse, request_id: str
) -> None:
    """Emit the per-request records for one completed search."""
    logger = stdlib_logging.getLogger(obs_logging.LOGGER_NAME)
    context = _context_from_diagnostics(request, response.diagnostics, request_id)
    obs_logging.log_search_completed(
        logger,
        request=request,
        context=context,
        request_id=request_id,
        result_count=len(response.results),
    )
    obs_logging.log_search_query_debug(logger, request=request, request_id=request_id)


class _ObservedEngine(Engine):
    """Engine decorator owning the single observability feed point.

    Wraps ``engine.search``: after the inner engine returns, the resolved
    request id is stamped into ``response.diagnostics["request_id"]`` (so
    the wire schema echoes it), the diagnostics payload feeds the metrics
    registry, and the per-request log records are emitted. Metrics and
    logging read only the fixed-shape diagnostics dict — never the query.
    """

    def __init__(self, inner: Engine) -> None:
        self._inner = inner

    def configure(self, config: dict[str, Any]) -> None:
        self._inner.configure(config)

    def search(self, request: SearchRequest) -> SearchResponse:
        request_id = _REQUEST_ID_CTX.get("")
        response = self._inner.search(request)
        if request_id:
            response.diagnostics["request_id"] = request_id
        get_registry().observe(response.diagnostics)
        _log_search(request, response, request_id)
        return response

    def health_check(self) -> dict[str, bool]:
        return self._inner.health_check()


def create_app(engine: Optional[Engine] = None) -> FastAPI:
    app = FastAPI(title="OpenLens", version="0.1.0")
    # Browser frontends call the API cross-origin in dev (vite :5173 ->
    # backend :8000); answer preflights for local origins only.
    # X-Request-Id is allowed so browsers can send and read it.
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization", "X-Request-Id"],
    )
    app.state.config = load_app_config()
    obs_logging.configure_logging(app.state.config)
    if _metrics_enabled(app.state.config):
        get_registry().enable()
    else:
        get_registry().disable()
    wrapped: Optional[Engine] = _ObservedEngine(engine) if engine is not None else None
    app.state.engine = wrapped
    register_exception_handlers(app)
    app.include_router(search_router)

    @app.middleware("http")
    async def _request_id_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = resolve_request_id(request.headers.get(REQUEST_ID_HEADER))
        request.state.request_id = request_id
        token = _REQUEST_ID_CTX.set(request_id)
        try:
            response = await call_next(request)
        finally:
            _REQUEST_ID_CTX.reset(token)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response

    if _metrics_enabled(app.state.config):
        # Registered conditionally: when disabled there is no /metrics
        # route at all (clients see a 404), never a registered-then-500.
        @app.get("/metrics", tags=["observability"])
        def metrics() -> PlainTextResponse:
            return PlainTextResponse(
                get_registry().render(),
                media_type="text/plain; version=0.0.4; charset=utf-8",
                headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
            )

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
    app.state.engine = _ObservedEngine(_booted)
    try:
        app.state.config = load_app_config() or {}
    except (ConfigError, OSError):
        pass

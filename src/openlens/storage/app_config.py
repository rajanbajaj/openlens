"""DB-persisted config docs backing the admin config REST API.

One row per config doc (``models``, ``providers``, ``ranking``, ``query``,
``plugins``) in an ``app_config_docs`` table. Postgres-first with SQLite
parity, mirroring the ``storage/frontier.py`` idioms:

- ``SqliteAppConfigStore`` is the zero-dependency reference (thread-safe,
  ``check_same_thread=False`` + lock, ``CREATE TABLE IF NOT EXISTS`` DDL);
- ``PostgresAppConfigStore`` mirrors it over ``psycopg`` (lazy driver
  import, ``ProviderError(UNAVAILABLE)`` when the driver or server is
  missing) with a ``JSONB`` body column;
- :func:`open_app_config_store` picks the backend from the app
  ``storage:`` section (``postgres`` when configured, else SQLite) and
  never raises and never touches the network or disk itself — both
  stores connect lazily on first use so importing ``openlens.main``
  (which calls :func:`open_app_config_store` in ``create_app``) stays
  side-effect free.

Boot precedence (file fallback preserved): YAML files + ``${VAR}``
resolution stay the source of truth until a doc row is written; once a
row exists it wins over the file section (the same "DB row wins once
written" precedent as ``frontier_settings``). ``storage:``/``cache:``
backends are deliberately NOT overridable docs — reading the store
needs the storage config first (boot-order paradox) — so only the five
doc keys in :data:`CONFIG_DOC_KEYS` are served.

Validation reuses the existing startup validators (fail-closed
``ConfigError``), so a doc accepted by PUT boots identically: models
via ``ModelManager.from_config``, providers via
``ProviderManager.from_config``, ranking via ``profile_from_dict``,
query via ``QueryProcessor.from_config``, plugins via
``main._plugin_entries`` normalization plus the engine's list shape.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import sqlite3
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from openlens.core.interfaces import ConfigError, ProviderError

#: Config docs served by the admin REST API. Each doc body is the section
#: as it appears under the top-level key of the loaded app config.
CONFIG_DOC_KEYS: tuple[str, ...] = (
    "models",
    "providers",
    "ranking",
    "query",
    "plugins",
    "indexing",
)

#: Env-free provider name used on ProviderError surfaces from this module.
_PROVIDER_NAME = "app_config"

#: Postgres driver module (lazy import only).
_DRIVER_MODULE = "psycopg"

#: SQLite write-lock wait before surfacing UNAVAILABLE.
_BUSY_TIMEOUT_MS = 5000

#: Sentinel returned in place of live secret values on config reads, and
#: accepted on writes to mean "keep the existing value".
REDACTED_SECRET = "__REDACTED__"  # noqa: S105 (redaction sentinel, not a credential)

#: Case-insensitive field-name fragments treated as secrets. Kept narrow:
#: only credential-shaped names match, never tuning knobs or endpoints.
_SECRET_NAME_PARTS: tuple[str, ...] = (
    "api_key",
    "apikey",
    "password",
    "passwd",
    "secret",
    "token",
)


def _is_secret_name(name: object) -> bool:
    return isinstance(name, str) and any(
        part in name.lower() for part in _SECRET_NAME_PARTS
    )


def _is_live_secret(value: object) -> bool:
    return value is not None and value != "" and value != REDACTED_SECRET


def redact_secrets(body: Any) -> Any:
    """Deep-copy ``body`` with live secret values replaced by a sentinel.

    Only fields whose name looks like a credential are touched; everything
    else (including unset/empty secrets, which leak nothing) is preserved.
    The input is never mutated.
    """
    if isinstance(body, Mapping):
        return {
            key: (
                REDACTED_SECRET
                if _is_secret_name(key) and _is_live_secret(value)
                else redact_secrets(value)
            )
            for key, value in body.items()
        }
    if isinstance(body, list):
        return [redact_secrets(item) for item in body]
    return body


def restore_secrets(submitted: Any, existing: Any) -> Any:
    """Fill ``REDACTED_SECRET`` placeholders in ``submitted`` from ``existing``.

    Same-path lookup only (mappings by key, lists pairwise by index). A
    placeholder with no live prior value is left in place so
    :func:`missing_secrets` can fail the write closed instead of storing
    the sentinel as a credential.
    """
    if isinstance(submitted, Mapping):
        base = existing if isinstance(existing, Mapping) else {}
        restored: dict[Any, Any] = {}
        for key, value in submitted.items():
            if value == REDACTED_SECRET and _is_secret_name(key):
                prior = base.get(key)
                restored[key] = prior if _is_live_secret(prior) else REDACTED_SECRET
            else:
                restored[key] = restore_secrets(value, base.get(key))
        return restored
    if isinstance(submitted, list):
        base_list = existing if isinstance(existing, list) else []
        return [
            restore_secrets(value, base_list[index] if index < len(base_list) else None)
            for index, value in enumerate(submitted)
        ]
    return submitted


def missing_secrets(body: Any) -> list[str]:
    """Dotted paths of secret-named fields still holding the sentinel."""
    found: list[str] = []

    def _walk(node: Any, path: str) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                child = f"{path}.{key}" if path else str(key)
                if value == REDACTED_SECRET and _is_secret_name(key):
                    found.append(child)
                else:
                    _walk(value, child)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                _walk(value, f"{path}[{index}]")

    _walk(body, "")
    return found


def _utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _utcnow_precise() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass
class AppConfigDoc:
    """One persisted config doc: body plus audit metadata."""

    doc_key: str
    body: dict[str, Any] | list[Any]
    version: int = 1
    updated_at: str = ""


class AppConfigStore(ABC):
    """Persistence contract for config docs (DB row wins once written)."""

    @abstractmethod
    def get(self, doc_key: str) -> AppConfigDoc | None:
        """Return the stored doc, or None when no row was ever written."""

    @abstractmethod
    def put(self, doc_key: str, body: Mapping[str, Any] | list[Any]) -> AppConfigDoc:
        """Insert or replace a doc, bumping version and updated_at."""

    @abstractmethod
    def close(self) -> None:
        """Release held connections (idempotent)."""


#: SQLite DDL for the config-docs table (runs inside one executescript,
#: mirroring ``repository.py::_init_schema`` / frontier DDL).
SQLITE_APP_CONFIG_DDL = """
CREATE TABLE IF NOT EXISTS app_config_docs (
    doc_key TEXT PRIMARY KEY,
    body TEXT NOT NULL DEFAULT '{}',
    version INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
"""


def postgres_app_config_ddl(schema: str = "public") -> list[str]:
    """DDL statements for the config-docs table on Postgres.

    ``schema`` is the caller's already-validated identifier; every value
    below is static SQL (bound parameters carry the runtime values).
    """
    q = f'"{schema}"'
    return [
        f"CREATE TABLE IF NOT EXISTS {q}.app_config_docs ("
        " doc_key TEXT PRIMARY KEY,"
        " body JSONB NOT NULL DEFAULT '{}',"
        " version INTEGER NOT NULL DEFAULT 1,"
        " updated_at TIMESTAMPTZ NOT NULL DEFAULT now()"
        ")",
    ]


class SqliteAppConfigStore(AppConfigStore):
    """SQLite-backed config docs (zero-dependency reference)."""

    def __init__(self, db_path: str = ":memory:") -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._closed = False
        # ``:memory:`` databases vanish per connection, so connect eagerly
        # (no file side effect). File paths connect lazily on first use so
        # constructing the store never creates files at import time.
        self._conn: sqlite3.Connection | None = None
        if db_path == ":memory:":
            self._conn = self._open(db_path)

    @staticmethod
    def _open(db_path: str) -> sqlite3.Connection:
        try:
            conn = sqlite3.connect(db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            with conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")  # noqa: S608
                conn.executescript(SQLITE_APP_CONFIG_DDL)
            return conn
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to open app config store: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def _connection(self) -> sqlite3.Connection:
        with self._lock:
            if self._closed:
                raise ProviderError(
                    "app config store is closed",
                    provider_name=_PROVIDER_NAME,
                    code=ProviderError.UNAVAILABLE,
                )
            if self._conn is None:
                parent_dir = self._db_path.rpartition("/")[0]
                if parent_dir:
                    import os

                    os.makedirs(parent_dir, exist_ok=True)
                self._conn = self._open(self._db_path)
            return self._conn

    def get(self, doc_key: str) -> AppConfigDoc | None:
        try:
            conn = self._connection()
            with self._lock:
                row = conn.execute(
                    "SELECT body, version, updated_at FROM app_config_docs"
                    " WHERE doc_key = ?",
                    (doc_key,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to read app config doc: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc
        if row is None:
            return None
        try:
            body = json.loads(row["body"] or "{}")
        except (ValueError, TypeError):
            body = {}
        if isinstance(body, Mapping):
            parsed: dict[str, Any] | list[Any] = dict(body)
        elif isinstance(body, list):
            parsed = list(body)
        else:
            parsed = {}
        return AppConfigDoc(
            doc_key=doc_key,
            body=parsed,
            version=int(row["version"] or 1),
            updated_at=str(row["updated_at"] or ""),
        )

    def put(self, doc_key: str, body: Mapping[str, Any] | list[Any]) -> AppConfigDoc:
        if not isinstance(body, (Mapping, list)):
            raise ConfigError("Config doc body must be a mapping or a list.")
        payload = json.dumps(dict(body) if isinstance(body, Mapping) else list(body))
        now = _utcnow()
        try:
            conn = self._connection()
            with self._lock, conn:
                conn.execute(
                    "INSERT INTO app_config_docs (doc_key, body, version,"
                    " updated_at) VALUES (?, ?, 1, ?)"
                    " ON CONFLICT(doc_key) DO UPDATE SET"
                    " body = excluded.body,"
                    " version = app_config_docs.version + 1,"
                    " updated_at = excluded.updated_at",
                    (doc_key, payload, now),
                )
                row = conn.execute(
                    "SELECT version, updated_at FROM app_config_docs WHERE doc_key = ?",
                    (doc_key,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to save app config doc: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc
        version = int(row["version"]) if row is not None else 1
        updated_at = str(row["updated_at"]) if row is not None else now
        return AppConfigDoc(
            doc_key=doc_key,
            body=dict(body) if isinstance(body, Mapping) else list(body),
            version=version,
            updated_at=updated_at,
        )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            conn, self._conn = self._conn, None
        with contextlib.suppress(Exception):
            if conn is not None:
                conn.close()


def _load_driver() -> Any:
    try:
        return importlib.import_module(_DRIVER_MODULE)
    except ImportError as exc:
        raise ProviderError(
            "Postgres driver 'psycopg' is not installed; "
            "install the postgres extra to use the Postgres app config backend",
            provider_name=_PROVIDER_NAME,
            code=ProviderError.UNAVAILABLE,
        ) from exc


class PostgresAppConfigStore(AppConfigStore):
    """Postgres-backed config docs (same contract as SQLite).

    Connects lazily on first use so constructing the store never blocks
    boot or tests when the server is unreachable; every failure surfaces
    as ``ProviderError(UNAVAILABLE)`` and callers fall back to file
    config (reads) or report 503 (writes).
    """

    def __init__(
        self,
        *,
        host: str = "localhost",
        port: int = 5432,
        dbname: str = "openlens",
        user: str = "postgres",
        password: str = "",
        schema: str = "public",
        connect_timeout: int = 5,
    ) -> None:
        import re

        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema or ""):
            raise ValueError(f"invalid schema name: {schema!r}")
        self._schema = schema
        self._connect_kwargs: dict[str, Any] = {
            "host": host,
            "port": int(port),
            "dbname": dbname,
            "user": user,
            "password": password,
            "connect_timeout": connect_timeout,
        }
        self._lock = threading.Lock()
        self._closed = False
        self._conn: Any = None
        # Fail fast after the first unreachable-host attempt within this
        # process: without it every doc read pays a full connect timeout.
        self._unreachable = False

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "PostgresAppConfigStore":
        """Build a store from ``storage.postgres`` fields (dbname aliases).

        Pool sizes and ports may be strings (YAML/env resolution yields
        text); they are coerced here, mirroring
        ``PostgresFrontierStore.from_config``.
        """
        dbname = config.get(
            "dbname", config.get("database", config.get("db", "openlens"))
        )
        kwargs: dict[str, Any] = {
            "host": config.get("host", "localhost"),
            "port": int(config.get("port", 5432)),
            "dbname": dbname,
            "user": config.get("user", "postgres"),
            "password": config.get("password", ""),
        }
        if "schema" in config:
            kwargs["schema"] = config["schema"]
        if "connect_timeout" in config:
            kwargs["connect_timeout"] = int(config["connect_timeout"])
        return cls(**kwargs)

    def _connection(self) -> Any:
        with self._lock:
            if self._closed:
                raise ProviderError(
                    "Postgres app config store is closed",
                    provider_name=_PROVIDER_NAME,
                    code=ProviderError.UNAVAILABLE,
                )
            if self._unreachable:
                raise ProviderError(
                    "Postgres app config store is unreachable",
                    provider_name=_PROVIDER_NAME,
                    code=ProviderError.UNAVAILABLE,
                )
            if self._conn is None:
                driver = _load_driver()
                try:
                    conn = driver.connect(**self._connect_kwargs)
                    conn.autocommit = True
                    with conn.cursor() as cur:
                        for stmt in postgres_app_config_ddl(self._schema):
                            cur.execute(stmt)  # noqa: S608
                    self._conn = conn
                except ProviderError:
                    raise
                except Exception as exc:
                    self._unreachable = True
                    raise ProviderError(
                        "failed to connect to Postgres for app config store: "
                        f"{self._connect_kwargs['host']}:"
                        f"{self._connect_kwargs['port']}: {exc}",
                        provider_name=_PROVIDER_NAME,
                        code=ProviderError.UNAVAILABLE,
                    ) from exc
            return self._conn

    def _fetch(self, doc_key: str) -> AppConfigDoc | None:
        conn = self._connection()
        table = f'"{self._schema}".app_config_docs'
        try:
            with self._lock, conn.cursor() as cur:
                cur.execute(
                    f"SELECT body, version, updated_at FROM {table}"  # noqa: S608
                    " WHERE doc_key = %s",
                    (doc_key,),
                )
                row = cur.fetchone()
        except Exception as exc:
            raise ProviderError(
                f"failed to read app config doc: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc
        if row is None:
            return None
        raw_body = row[0]
        if isinstance(raw_body, str):
            try:
                decoded: Any = json.loads(raw_body or "{}")
            except (ValueError, TypeError):
                decoded = {}
        elif isinstance(raw_body, (dict, list)):
            decoded = raw_body
        else:
            decoded = {}
        if isinstance(decoded, Mapping):
            parsed_body: dict[str, Any] | list[Any] = dict(decoded)
        elif isinstance(decoded, list):
            parsed_body = list(decoded)
        else:
            parsed_body = {}
        updated_at = row[2]
        if hasattr(updated_at, "isoformat"):
            updated_at = updated_at.isoformat()
        return AppConfigDoc(
            doc_key=doc_key,
            body=parsed_body,
            version=int(row[1] or 1),
            updated_at=str(updated_at or ""),
        )

    def get(self, doc_key: str) -> AppConfigDoc | None:
        return self._fetch(doc_key)

    def put(self, doc_key: str, body: Mapping[str, Any] | list[Any]) -> AppConfigDoc:
        if not isinstance(body, (Mapping, list)):
            raise ConfigError("Config doc body must be a mapping or a list.")
        conn = self._connection()
        table = f'"{self._schema}".app_config_docs'
        payload = json.dumps(dict(body) if isinstance(body, Mapping) else list(body))
        try:
            with self._lock, conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {table} (doc_key, body, version, updated_at)"  # noqa: S608
                    " VALUES (%s, CAST(%s AS jsonb), 1, now())"
                    " ON CONFLICT (doc_key) DO UPDATE SET"
                    " body = CAST(excluded.body AS jsonb),"
                    " version = app_config_docs.version + 1,"
                    " updated_at = now()"
                    " RETURNING version, updated_at",
                    (doc_key, payload),
                )
                row = cur.fetchone()
        except Exception as exc:
            raise ProviderError(
                f"failed to save app config doc: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc
        updated_at = row[1] if row is not None else ""
        if hasattr(updated_at, "isoformat"):
            updated_at = updated_at.isoformat()
        return AppConfigDoc(
            doc_key=doc_key,
            body=dict(body) if isinstance(body, Mapping) else list(body),
            version=int(row[0]) if row is not None else 1,
            updated_at=str(updated_at or _utcnow_precise()),
        )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            conn, self._conn = self._conn, None
        with contextlib.suppress(Exception):
            if conn is not None:
                conn.close()


def open_app_config_store(config: Mapping[str, Any] | None) -> AppConfigStore:
    """Build the configured config-doc store (Postgres default).

    Reads the app ``storage:`` section: ``postgres`` backend opens
    :class:`PostgresAppConfigStore` (lazy-connect, so this call never
    blocks), anything else opens :class:`SqliteAppConfigStore` on the
    resolved SQLite path. Never raises and never performs I/O: any
    malformed section falls back to an in-memory SQLite store so boot
    and tests stay offline-safe.
    """
    try:
        storage: Any = {}
        if isinstance(config, Mapping):
            storage = config.get("storage", {}) or {}
        if not isinstance(storage, Mapping):
            storage = {}
        if storage.get("backend") == "postgres":
            section = storage.get("postgres", {}) or {}
            if isinstance(section, Mapping):
                return PostgresAppConfigStore.from_config(section)
        sqlite_cfg = storage.get("sqlite", {}) or {}
        if not isinstance(sqlite_cfg, Mapping):
            sqlite_cfg = {}
        db_path = sqlite_cfg.get("db_path", "data/openlens.db")
        if not isinstance(db_path, str) or not db_path:
            db_path = ":memory:"
        return SqliteAppConfigStore(db_path)
    except (ValueError, TypeError, ProviderError, ConfigError):
        return SqliteAppConfigStore(":memory:")


def validate_config_doc(doc_key: str, body: Any) -> None:
    """Validate a config doc body with the existing startup validators.

    Raises ``ConfigError`` on anything the boot path would reject, so a
    doc accepted by PUT boots identically. Unknown doc keys raise
    ``ConfigError`` too (routes map that to 404 before calling here).
    """
    if doc_key not in CONFIG_DOC_KEYS:
        raise ConfigError(
            f"Unknown config doc '{doc_key}'. Known: {sorted(CONFIG_DOC_KEYS)}."
        )
    if doc_key == "plugins":
        if not isinstance(body, (dict, list)):
            raise ConfigError("Config doc 'plugins' body must be a mapping or a list.")
    elif doc_key == "indexing":
        if not isinstance(body, dict):
            raise ConfigError("Config doc 'indexing' body must be a mapping.")
        from openlens.indexing.crawler import PoliteCrawler

        PoliteCrawler().configure(dict(body))
        return
    elif not isinstance(body, dict):
        raise ConfigError(f"Config doc '{doc_key}' body must be a mapping.")
    if doc_key == "models":
        from openlens.models.manager import ModelManager

        try:
            ModelManager.from_config({"models": body})
        except ConfigError as exc:
            # Zero enabled backends is a valid "no AI" state: boot maps
            # it to a None manager (see engine._build_model_manager), so
            # the PUT validator must accept it too. Any other validation
            # error (bad primary, malformed entry) still fails the save.
            if "No enabled model backends" not in str(exc):
                raise
    elif doc_key == "providers":
        from openlens.providers.manager import ProviderManager

        ProviderManager.from_config(body)
    elif doc_key == "ranking":
        _validate_ranking_section(body)
    elif doc_key == "query":
        from openlens.retrieval.query_processor import QueryProcessor

        QueryProcessor.from_config({"query": body})
    elif doc_key == "plugins":
        _validate_plugins_section(body)


def _validate_ranking_section(body: dict[str, Any]) -> None:
    """Validate a ``ranking:`` section (mirrors engine.configure rules)."""
    from openlens.ranking.profiles import BUILTIN_PROFILES, profile_from_dict

    default_profile = body.get("default_profile", body.get("default_mode", "default"))
    if not isinstance(default_profile, str) or not default_profile:
        raise ConfigError("Ranking 'default_profile' must be a non-empty string.")
    inline = body.get("profiles", None)
    if inline is not None:
        if not isinstance(inline, dict):
            raise ConfigError("Config section 'ranking.profiles' must be a mapping.")
        for profile_name, entry in inline.items():
            if not isinstance(entry, dict):
                raise ConfigError(
                    f"Ranking profile '{profile_name}' must be a mapping."
                )
            profile_from_dict(profile_name, entry)
    known = set(BUILTIN_PROFILES)
    if isinstance(inline, dict):
        known.update(inline)
    try:
        from openlens.main import load_ranking_profiles

        file_profiles = load_ranking_profiles()
    except (ConfigError, OSError):
        file_profiles = None
    if file_profiles:
        known.update(file_profiles)
    if default_profile not in known:
        raise ConfigError(
            f"Unknown ranking profile '{default_profile}'. Known: {sorted(known)}."
        )


def _validate_plugins_section(body: Any) -> None:
    """Validate a ``plugins:`` section (normalizer + engine list shape)."""
    from openlens.main import _plugin_entries

    entries = _plugin_entries({"plugins": body})
    if not isinstance(entries, list):
        raise ConfigError("Engine config 'plugins' must be a list.")
    for entry in entries:
        if not isinstance(entry, dict):
            raise ConfigError("Engine config 'plugins' entries must be mappings.")
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(
                "Engine config 'plugins' entries need a non-empty 'name'."
            )


def extract_file_doc(
    config: Mapping[str, Any] | None, doc_key: str
) -> dict[str, Any] | list[Any]:
    """Return the file-backed section for a doc key ({} when absent).

    ``plugins:`` may be a list section; every other doc is a mapping.
    """
    if not isinstance(config, Mapping):
        return {}
    section = config.get(doc_key, {})
    if isinstance(section, Mapping):
        return dict(section)
    if isinstance(section, list):
        return list(section)
    return {}


def try_get_doc(store: AppConfigStore | None, doc_key: str) -> AppConfigDoc | None:
    """Best-effort DB read: None when no row exists or the store is down."""
    if store is None:
        return None
    try:
        return store.get(doc_key)
    except (ProviderError, OSError):
        return None


def apply_db_config_overlay(
    config: dict[str, Any], store: AppConfigStore | None
) -> dict[str, Any]:
    """Overlay persisted docs onto a file-loaded config (DB wins).

    Reads every doc in :data:`CONFIG_DOC_KEYS`; each stored row replaces
    its file section wholesale. Unreachable stores degrade to the file
    config unchanged (offline-safe boot).
    """
    if not isinstance(config, dict) or store is None:
        return config
    merged = dict(config)
    for doc_key in CONFIG_DOC_KEYS:
        doc = try_get_doc(store, doc_key)
        if doc is not None:
            merged[doc_key] = (
                dict(doc.body) if isinstance(doc.body, Mapping) else list(doc.body)
            )
    return merged

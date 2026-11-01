"""Analytics event store: ClickHouse (prod) or SQLite (offline/tests).

The analytics database lives apart from the index/frontier database on
purpose: analytics is high-volume append traffic with time-based expiry,
while search needs low-latency reads and the frontier needs contended
writes. ClickHouse (MergeTree + TTL) is the prod backend — columnar
writes and native expiry fit append-only event streams far better than
row-store bloat plus scheduled DELETEs.

Postgres was deliberately not reused here: the v0.4.33-v0.4.35 incident
chain was write contention on the primary, and analytics would recreate
exactly that load pattern.

SQLite keeps the same method contract for offline dev and tests.
"""

from __future__ import annotations

import contextlib
import json
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any, Mapping

from openlens.core.interfaces import ProviderError

_PROVIDER_NAME = "analytics"

DEFAULT_DATABASE = "openlens_analytics"
DEFAULT_RETENTION_DAYS = 90


class AnalyticsStore:
    """SQLite analytics store: same contract, offline backend."""

    def __init__(self, db_path: str = ":memory:") -> None:
        self._lock = threading.Lock()
        self._closed = False
        try:
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            with self._lock:
                self._conn.executescript(_SQLITE_DDL)
                self._conn.commit()
        except Exception as exc:
            raise ProviderError(
                f"failed to open analytics store: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def insert_many(self, events: list[dict[str, Any]]) -> int:
        """Insert validated event payloads; returns the inserted count."""
        rows: list[tuple[str, str, str, str]] = []
        for event in events or []:
            if not isinstance(event, dict):
                continue
            name = event.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            try:
                at = float(event.get("at", 0.0) or 0.0)
            except (TypeError, ValueError):
                continue
            if at <= 0:
                continue
            props = event.get("props")
            rows.append(
                (
                    name.strip(),
                    str(event.get("source", "server") or "server"),
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at)),
                    json.dumps(props if isinstance(props, dict) else {}),
                )
            )
        if not rows:
            return 0
        try:
            with self._lock, self._conn:
                self._conn.executemany(
                    "INSERT INTO analytics_events (name, source, at, props)"
                    " VALUES (?, ?, ?, ?)",
                    rows,
                )
                return len(rows)
        except Exception as exc:
            raise ProviderError(
                f"failed to insert analytics events: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def counts_by_name(self, since_days: int = 30) -> dict[str, int]:
        """Event counts per name over the trailing window."""
        cutoff = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(time.time() - max(0, since_days) * 86400),
        )
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT name, COUNT(*) AS n FROM analytics_events"
                    " WHERE at >= ? GROUP BY name",
                    (cutoff,),
                ).fetchall()
                return {str(row["name"]): int(row["n"]) for row in rows}
        except Exception as exc:
            raise ProviderError(
                f"failed to count analytics events: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def purge_older_than(self, days: int) -> int:
        """Delete events older than ``days``; returns the removed count."""
        cutoff = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - max(0, days) * 86400)
        )
        try:
            with self._lock, self._conn:
                cursor = self._conn.execute(
                    "DELETE FROM analytics_events WHERE at < ?", (cutoff,)
                )
                return int(cursor.rowcount or 0)
        except Exception as exc:
            raise ProviderError(
                f"failed to purge analytics events: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        with contextlib.suppress(Exception):
            self._conn.close()


_SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS analytics_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'server',
    at TEXT NOT NULL,
    props TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_analytics_name_at
ON analytics_events(name, at);
"""


def _load_clickhouse() -> Any:
    try:
        import clickhouse_connect  # type: ignore[import-not-found, import-untyped]
    except ImportError as exc:
        raise ProviderError(
            "ClickHouse driver 'clickhouse-connect' is not installed; "
            "install the analytics extra to use the ClickHouse store",
            provider_name=_PROVIDER_NAME,
            code=ProviderError.UNAVAILABLE,
        ) from exc
    return clickhouse_connect


def _checked_identifier(value: str, what: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value or ""):
        raise ProviderError(
            f"invalid analytics {what}: {value!r}",
            provider_name=_PROVIDER_NAME,
            code=ProviderError.INVALID_REQUEST,
        )
    return value


class ClickHouseAnalyticsStore:
    """ClickHouse analytics store: MergeTree + native TTL expiry.

    Retention is enforced by the table TTL (``MODIFY TTL`` tracks
    ``retention_days`` changes), so expiry costs no DELETE traffic at all;
    :meth:`purge_older_than` additionally issues an explicit lightweight
    delete for same-day enforcement and returns the pre-delete count.
    """

    def __init__(
        self,
        *,
        host: str = "localhost",
        port: int = 8123,
        database: str = DEFAULT_DATABASE,
        user: str = "default",
        password: str = "",
        retention_days: int = DEFAULT_RETENTION_DAYS,
        connect_timeout: int = 5,
    ) -> None:
        clickhouse_connect = _load_clickhouse()
        self._database = _checked_identifier(database, "database")
        self._retention_days = max(1, int(retention_days))
        self._client = clickhouse_connect.get_client(
            host=host,
            port=int(port),
            username=user,
            password=password,
            connect_timeout=connect_timeout,
            send_receive_timeout=30,
        )
        self._closed = False
        try:
            self._client.command(f"CREATE DATABASE IF NOT EXISTS {self._database}")
            self._client.command(
                f"CREATE TABLE IF NOT EXISTS {self._database}.analytics_events ("
                "name String, "
                "source String DEFAULT 'server', "
                "at DateTime, "
                "props String"
                ") ENGINE = MergeTree ORDER BY (name, at)"
            )
            self._client.command(
                f"ALTER TABLE {self._database}.analytics_events "
                f"MODIFY TTL at + INTERVAL {self._retention_days} DAY"
            )
        except Exception as exc:
            with contextlib.suppress(Exception):
                self._client.close()
            raise ProviderError(
                f"failed to open analytics store: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    @classmethod
    def from_env(cls) -> "ClickHouseAnalyticsStore":
        """Build from the ``OPENLENS_CLICKHOUSE_*`` env."""
        import os

        return cls(
            host=os.environ.get("OPENLENS_CLICKHOUSE_HOST", "localhost"),
            port=int(os.environ.get("OPENLENS_CLICKHOUSE_PORT", "8123")),
            database=os.environ.get("OPENLENS_CLICKHOUSE_DB", DEFAULT_DATABASE),
            user=os.environ.get("OPENLENS_CLICKHOUSE_USER", "default"),
            password=os.environ.get("OPENLENS_CLICKHOUSE_PASSWORD", ""),
            retention_days=int(
                os.environ.get(
                    "OPENLENS_ANALYTICS_RETENTION_DAYS", str(DEFAULT_RETENTION_DAYS)
                )
            ),
        )

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "ClickHouseAnalyticsStore":
        """Build from an ``analytics.store.clickhouse`` mapping."""
        section = config.get("clickhouse", {}) if isinstance(config, Mapping) else {}
        if not isinstance(section, Mapping):
            section = {}
        parent = config if isinstance(config, Mapping) else {}
        try:
            retention = int(
                parent.get(
                    "retention_days",
                    section.get("retention_days", DEFAULT_RETENTION_DAYS),
                )
            )
        except (TypeError, ValueError):
            retention = DEFAULT_RETENTION_DAYS
        return cls(
            host=str(section.get("host", "localhost")),
            port=int(section.get("port", 8123)),
            database=str(section.get("database", DEFAULT_DATABASE)),
            user=str(section.get("user", "default")),
            password=str(section.get("password", "")),
            retention_days=retention,
        )

    def _table(self) -> str:
        return f"{self._database}.analytics_events"

    def insert_many(self, events: list[dict[str, Any]]) -> int:
        rows: list[list[Any]] = []
        for event in events or []:
            if not isinstance(event, dict):
                continue
            name = event.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            try:
                at = float(event.get("at", 0.0) or 0.0)
            except (TypeError, ValueError):
                continue
            if at <= 0:
                continue
            props = event.get("props")
            rows.append(
                [
                    name.strip(),
                    str(event.get("source", "server") or "server"),
                    datetime.fromtimestamp(at, tz=timezone.utc).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                    json.dumps(props if isinstance(props, dict) else {}),
                ]
            )
        if not rows:
            return 0
        try:
            self._client.insert(
                self._table(),
                rows,
                column_names=["name", "source", "at", "props"],
            )
            return len(rows)
        except Exception as exc:
            raise ProviderError(
                f"failed to insert analytics events: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def counts_by_name(self, since_days: int = 30) -> dict[str, int]:
        window = max(0, int(since_days))
        try:
            result = self._client.query(
                f"SELECT name, COUNT() FROM {self._table()} "  # noqa: S608
                f"WHERE at >= now() - INTERVAL {window} DAY GROUP BY name"
            )
            return {str(name): int(n) for name, n in result.result_rows}
        except Exception as exc:
            raise ProviderError(
                f"failed to count analytics events: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def purge_older_than(self, days: int) -> int:
        """Best-effort explicit delete (TTL owns steady-state expiry)."""
        window = max(0, int(days))
        try:
            count = self._client.query(
                f"SELECT COUNT() FROM {self._table()} "  # noqa: S608
                f"WHERE at < now() - INTERVAL {window} DAY"
            )
            removed = int(count.result_rows[0][0]) if count.result_rows else 0
            self._client.command(
                f"ALTER TABLE {self._table()} DELETE "  # noqa: S608
                f"WHERE at < now() - INTERVAL {window} DAY"
            )
            return removed
        except Exception as exc:
            raise ProviderError(
                f"failed to purge analytics events: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            self._client.close()


def open_analytics_store(config: Mapping[str, Any] | None = None) -> Any:
    """Open the configured analytics store (sqlite default, else ClickHouse).

    ``{"backend": "clickhouse", "clickhouse": {...}}`` selects ClickHouse;
    anything else opens SQLite (``sqlite.db_path``, default in-memory), so
    offline dev and tests boot with no services.
    """
    cfg = config if isinstance(config, Mapping) else {}
    backend = str(cfg.get("backend", "sqlite") or "sqlite").strip().lower()
    if backend == "clickhouse":
        section = cfg.get("clickhouse", {})
        if not isinstance(section, Mapping):
            section = {}
        merged = dict(section)
        if "retention_days" in cfg and "retention_days" not in merged:
            merged["retention_days"] = cfg["retention_days"]
        return ClickHouseAnalyticsStore.from_config({"clickhouse": merged})
    sqlite_cfg = cfg.get("sqlite", {})
    db_path = ":memory:"
    if isinstance(sqlite_cfg, Mapping):
        db_path = str(sqlite_cfg.get("db_path", ":memory:") or ":memory:")
    return AnalyticsStore(db_path)

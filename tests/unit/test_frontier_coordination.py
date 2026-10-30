"""Shared-queue coordination tests (IMPL-COORD).

Offline only: SQLite stores (``:memory:`` for single-connection cases, a
temporary file when two store instances must share state like two
workers). No real network, no live Postgres (the Postgres smoke test
skips without a driver/server).

Covers: atomic claim/lease under thread concurrency (no double-claim),
heartbeat + expiry-requeue (expired recovered, live never stolen),
per-domain politeness shared across workers, graceful-shutdown requeue,
worker registry, delta checkpoint counters, legacy-DB migration, and the
shipped ``configs/default.yaml`` coordination fields.
"""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from openlens.core.interfaces import ProviderError
from openlens.storage.frontier import (
    DEFAULT_LEASE_TTL_S,
    CrawlCheckpoint,
    FrontierStore,
    SqliteFrontierStore,
    WorkerLease,
    ordered_postgres_frontier_schema,
    postgres_frontier_coordination_migrations,
    postgres_frontier_ddl,
    resolve_worker_id,
    sqlite_frontier_coordination_migrations,
)

try:
    import psycopg  # type: ignore[import-not-found]

    _HAS_PSYCOPG = True
except ImportError:
    psycopg = None  # type: ignore[assignment]
    _HAS_PSYCOPG = False

needs_pg_driver = pytest.mark.skipif(not _HAS_PSYCOPG, reason="psycopg not installed")


def _store(urls: int = 0, **kwargs) -> SqliteFrontierStore:  # type: ignore[no-untyped-def]
    store = SqliteFrontierStore(":memory:", **kwargs)
    for index in range(urls):
        store.enqueue(f"https://crawl.test/page-{index}", 0)
    return store


# --- atomic claim ----------------------------------------------------------


def test_claim_records_owner_and_lease() -> None:
    store = _store(1)
    try:
        entry = store.claim("worker-1")
        assert entry is not None
        assert entry.status == "active"
        assert entry.owner == "worker-1"
        assert entry.leased_at
        assert entry.heartbeat_at
        assert entry.lease_ttl_s == DEFAULT_LEASE_TTL_S
        assert store.counts().get("active") == 1
        assert [row.url for row in store.owned("worker-1")] == [entry.url]
        assert store.owned("worker-2") == []
    finally:
        store.close()


def test_claim_rejects_empty_worker_id() -> None:
    store = _store(1)
    try:
        with pytest.raises(ProviderError):
            store.claim("  ")
    finally:
        store.close()


def test_no_double_claim_under_thread_concurrency() -> None:
    workers = 8
    urls = 20
    store = _store(urls)
    claimed: list[str] = []
    guard = threading.Lock()
    try:

        def _drain(worker_id: str) -> None:
            while True:
                entry = store.claim(worker_id)
                if entry is None:
                    return
                with guard:
                    claimed.append(entry.url)

        threads = [
            threading.Thread(target=_drain, args=(f"worker-{n}",))
            for n in range(workers)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert not any(thread.is_alive() for thread in threads)
        # Every URL claimed exactly once: no double-claim, no loss.
        assert sorted(claimed) == sorted(
            f"https://crawl.test/page-{index}" for index in range(urls)
        )
        assert store.counts().get("active") == urls
        assert store.claim("late-worker") is None
    finally:
        store.close()


# --- heartbeat + expiry-requeue --------------------------------------------


def test_heartbeat_refreshes_owned_rows_only() -> None:
    store = _store(2)
    try:
        assert store.claim("worker-a") is not None
        assert store.claim("worker-b") is not None
        assert store.heartbeat("worker-a") == 1
        assert store.heartbeat("unknown-worker") == 0
    finally:
        store.close()


def test_reap_recovers_expired_but_never_steals_live() -> None:
    store = _store(0)
    try:
        store.enqueue("https://crawl.test/live-a", 0)
        store.enqueue("https://crawl.test/live-b", 0)
        assert store.claim("worker-a") is not None
        assert store.claim("worker-b") is not None
        # Both heartbeats are fresh: startup recovery steals nothing.
        assert store.reap_expired_leases() == 0
        assert store.counts().get("active") == 2
        # Far-future clock: both leases expired, both recovered.
        assert store.reap_expired_leases(now=time.time() + 10_000) == 2
        assert store.counts().get("queued") == 2
    finally:
        store.close()


def test_reap_recovers_legacy_ownerless_rows() -> None:
    store = _store(1)
    try:
        # dequeue() stamps no owner/heartbeat (pre-coordination row shape).
        assert store.dequeue() is not None
        assert store.reap_expired_leases() == 1
        assert store.counts().get("queued") == 1
    finally:
        store.close()


# --- graceful shutdown ------------------------------------------------------


def test_requeue_owned_releases_only_that_worker() -> None:
    store = _store(3)
    try:
        assert store.claim("worker-a") is not None
        assert store.claim("worker-a") is not None
        assert store.claim("worker-b") is not None
        assert store.requeue_owned("worker-a") == 2
        assert store.requeue_owned("worker-a") == 0
        assert store.counts().get("queued") == 2
        assert len(store.owned("worker-b")) == 1
        assert store.reap_expired_leases() == 0
    finally:
        store.close()


def test_worker_lease_shutdown_releases_in_flight() -> None:
    store = _store(2)
    try:
        lease = WorkerLease(store, "worker-z", heartbeat_interval_s=0.02)
        lease.start()
        try:
            assert lease.claim() is not None
            assert lease.claimed_total == 1
            assert len(store.list_workers()) == 1
        finally:
            released = lease.shutdown()
        assert released == 1
        assert store.counts().get("queued") == 2
    finally:
        store.close()


def test_worker_lease_context_manager() -> None:
    store = _store(1)
    try:
        with WorkerLease(store, "worker-ctx") as lease:
            assert lease.claim() is not None
        assert store.counts().get("queued") == 1
    finally:
        store.close()


# --- shared politeness ------------------------------------------------------


def test_politeness_shared_across_worker_connections(tmp_path) -> None:  # type: ignore[no-untyped-def]
    db = str(tmp_path / "shared.db")
    first = SqliteFrontierStore(db)
    second = SqliteFrontierStore(db)
    try:
        moment = 1_700_000_000.0
        assert first.reserve_politeness("Example.COM", 10.0, now=moment) == 0.0
        # Second worker (separate connection) sees the reservation.
        assert second.reserve_politeness("example.com", 10.0, now=moment) == 10.0
        assert first.reserve_politeness("example.com", 10.0, now=moment + 5) == 15.0
        assert first.get_politeness_delay("EXAMPLE.com") == 10.0
        assert first.get_politeness_delay("other.test") == 0.0
        assert first.reserve_politeness("", 10.0, now=moment) == 0.0
    finally:
        first.close()
        second.close()


# --- worker registry --------------------------------------------------------


def test_worker_registry_round_trip_and_prune() -> None:
    store = _store(0)
    try:
        assert store.list_workers() == []
        store.register_worker("worker-a", "crawl-1")
        store.heartbeat_worker("worker-b", "crawl-1", claimed=3)
        store.heartbeat_worker("worker-c", "other-crawl")
        assert {row["worker_id"] for row in store.list_workers()} == {
            "worker-a",
            "worker-b",
            "worker-c",
        }
        assert {row["worker_id"] for row in store.list_workers("crawl-1")} == {
            "worker-a",
            "worker-b",
        }
        by_id = {row["worker_id"]: row for row in store.list_workers()}
        assert by_id["worker-b"]["claimed"] == 3
        assert by_id["worker-b"]["crawl_id"] == "crawl-1"
        # Fresh heartbeats: default prune removes nothing.
        assert store.prune_workers() == 0
        # Far-future clock: all rows stale, all pruned.
        assert store.prune_workers(now=time.time() + 10_000) == 3
        assert store.list_workers() == []
    finally:
        store.close()


# --- delta counters ----------------------------------------------------------


def test_increment_counters_adds_without_clobbering() -> None:
    store = _store(0)
    try:
        store.save_checkpoint(
            CrawlCheckpoint(
                crawl_id="crawl-1",
                seeds=["https://crawl.test/"],
                phase="paused",
                pages_attempted=10,
                pages_indexed=7,
                frontier_size=4,
                stats={"fetched": 7},
            )
        )
        merged = store.increment_counters("crawl-1", pages_attempted=5, pages_indexed=3)
        assert merged.pages_attempted == 15
        assert merged.pages_indexed == 10
        # Full-row fields survive the delta write (no last-writer-wins loss).
        assert merged.seeds == ["https://crawl.test/"]
        assert merged.phase == "paused"
        assert merged.stats == {"fetched": 7}
        second = store.increment_counters("crawl-1", pages_attempted=5, pages_indexed=3)
        assert (second.pages_attempted, second.pages_indexed) == (20, 13)
        fresh = store.increment_counters(
            "new-crawl", pages_attempted=2, pages_indexed=1
        )
        assert (fresh.pages_attempted, fresh.pages_indexed) == (2, 1)
    finally:
        store.close()


# --- legacy migration ---------------------------------------------------------


_LEGACY_DDL = """
CREATE TABLE frontier_queue (
    url TEXT PRIMARY KEY,
    depth INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'queued',
    parent_url TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    etag TEXT,
    last_modified TEXT,
    discovered_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    fetched_at TEXT
);
"""


def test_legacy_sqlite_db_migrates_to_coordination_schema(tmp_path) -> None:  # type: ignore[no-untyped-def]
    db = str(tmp_path / "legacy.db")
    raw = sqlite3.connect(db)
    try:
        raw.executescript(_LEGACY_DDL)
        raw.execute(
            "INSERT INTO frontier_queue (url, depth, status)"
            " VALUES ('https://crawl.test/old', 0, 'queued')"
        )
        raw.commit()
    finally:
        raw.close()
    store = SqliteFrontierStore(db)
    try:
        entry = store.claim("worker-1")
        assert entry is not None and entry.owner == "worker-1"
        assert store.reserve_politeness("crawl.test", 5.0) == 0.0
        store.register_worker("worker-1")
        assert len(store.list_workers()) == 1
        # Reopening re-runs the migration idempotently.
        store.close()
        reopened = SqliteFrontierStore(db)
        try:
            assert reopened.reap_expired_leases() == 0
        finally:
            reopened.close()
    finally:
        with __import__("contextlib").suppress(Exception):
            store.close()


def test_migration_helpers_cover_both_backends() -> None:
    sqlite_stmts = sqlite_frontier_coordination_migrations()
    assert any("ADD COLUMN owner" in stmt for stmt in sqlite_stmts)
    assert any("crawl_workers" in stmt for stmt in sqlite_stmts)
    assert any("domain_politeness" in stmt for stmt in sqlite_stmts)
    pg_stmts = postgres_frontier_coordination_migrations("public")
    assert any("ADD COLUMN IF NOT EXISTS owner" in stmt for stmt in pg_stmts)
    assert any("crawl_workers" in stmt for stmt in pg_stmts)
    assert any("domain_politeness" in stmt for stmt in pg_stmts)
    ddl = "\n".join(postgres_frontier_ddl("public"))
    assert "owner TEXT" in ddl
    assert "crawl_workers" in ddl
    assert "domain_politeness" in ddl
    assert isinstance(SqliteFrontierStore(":memory:").reap_expired_leases(), int)


def test_ordered_postgres_schema_migrates_before_indexing() -> None:
    # Regression: on pre-coordination databases the raw DDL list runs
    # CREATE INDEX idx_frontier_owner before the ADD COLUMN owner
    # migration, failing init with column "owner" does not exist.
    # DDL tables first, then migrations, then indexes last.
    stmts = ordered_postgres_frontier_schema("public")
    ddl_tables = [
        s
        for s in postgres_frontier_ddl("public")
        if s.lstrip().upper().startswith("CREATE TABLE")
    ]
    assert ddl_tables, "expected DDL tables in postgres_frontier_ddl"
    assert stmts[: len(ddl_tables)] == ddl_tables
    rest = stmts[len(ddl_tables) :]
    first_index = next(
        i for i, s in enumerate(rest) if s.lstrip().upper().startswith("CREATE INDEX")
    )
    assert first_index > 0, "expected migration statements before indexes"
    pre_index_non_tables = [
        s for s in rest[:first_index] if "CREATE TABLE" not in s.upper()
    ]
    assert pre_index_non_tables
    assert all("ADD COLUMN" in s.upper() for s in pre_index_non_tables)
    assert all(
        s.lstrip().upper().startswith("CREATE INDEX") for s in rest[first_index:]
    )
    # Nothing dropped: every DDL + migration statement is present exactly once.
    raw = postgres_frontier_ddl("public") + postgres_frontier_coordination_migrations(
        "public"
    )
    assert sorted(stmts) == sorted(raw)


def test_coordination_defaults_preserve_single_worker_shape() -> None:
    class _LegacyStore(FrontierStore):
        def enqueue(self, url: str, depth: int = 0, parent_url=None) -> bool:  # type: ignore[no-untyped-def]
            return False

        def dequeue(self):  # type: ignore[no-untyped-def]
            return None

        def mark_done(self, url: str, **kwargs) -> None:  # type: ignore[no-untyped-def]
            return None

        def mark_failed(self, url: str, error: str) -> None:
            return None

        def mark_skipped(self, url: str) -> int:
            return 0

        def requeue_active(self) -> int:
            return 7

        def requeue(self, url: str) -> bool:
            return False

        def is_known(self, url: str) -> bool:
            return False

        def pending(self, limit: int = 500, offset: int = 0):  # type: ignore[no-untyped-def]
            return []

        def counts(self):  # type: ignore[no-untyped-def]
            return {"total": 0}

        def save_checkpoint(self, checkpoint) -> None:  # type: ignore[no-untyped-def]
            return None

        def load_checkpoint(self, crawl_id: str = "default"):  # type: ignore[no-untyped-def]
            return None

        def get_settings(self, settings_id: str = "default"):  # type: ignore[no-untyped-def]
            return {}

        def save_settings(self, payload, settings_id: str = "default") -> None:  # type: ignore[no-untyped-def]
            return None

        def close(self) -> None:
            return None

        def clear_queued(self) -> int:
            return 0

    legacy = _LegacyStore()
    assert legacy.claim("worker-1") is None
    assert legacy.reap_expired_leases() == 7
    assert legacy.heartbeat("worker-1") == 0
    assert legacy.requeue_owned("worker-1") == 0
    assert legacy.reserve_politeness("example.com", 5.0) == 0.0
    assert legacy.list_workers() == []


# --- identity + shipped config --------------------------------------------------


def test_resolve_worker_id_precedence(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("OPENLENS_WORKER_ID", raising=False)
    assert resolve_worker_id("explicit") == "explicit"
    monkeypatch.setenv("OPENLENS_WORKER_ID", "env-worker")
    assert resolve_worker_id(None) == "env-worker"
    assert resolve_worker_id("  ") == "env-worker"
    monkeypatch.delenv("OPENLENS_WORKER_ID", raising=False)
    fallback = resolve_worker_id(None)
    assert fallback and "-" in fallback


def test_shipped_default_yaml_carries_coordination_fields() -> None:
    from pathlib import Path

    import yaml

    repo_root = Path(__file__).resolve().parents[2]
    with (repo_root / "configs" / "default.yaml").open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    assert set(config.get("storage", {}).get("postgres", {})) >= {
        "host",
        "port",
        "dbname",
        "user",
        "password",
        "table",
        "pool_size",
        "connect_timeout",
        "checkout_timeout",
    }
    assert set(config.get("frontier", {})) >= {
        "lease_ttl_s",
        "heartbeat_interval_s",
        "reaper_interval_s",
    }


@needs_pg_driver
def test_postgres_claim_smoke_when_server_reachable() -> None:
    from openlens.storage.frontier import PostgresFrontierStore

    try:
        store = PostgresFrontierStore(dbname="openlens")
    except ProviderError as exc:
        pytest.skip(f"no reachable Postgres for coordination smoke: {exc}")
    try:
        store.enqueue("https://crawl.test/pg-smoke", 0)
        entry = store.claim("pg-worker")
        assert entry is not None and entry.owner == "pg-worker"
        assert store.heartbeat("pg-worker") == 1
        assert store.reap_expired_leases() == 0
        assert store.requeue_owned("pg-worker") == 1
        assert store.reserve_politeness("crawl.test", 5.0) >= 0.0
        store.register_worker("pg-worker")
        assert any(row["worker_id"] == "pg-worker" for row in store.list_workers())
    finally:
        store.close()

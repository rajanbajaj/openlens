"""Tests for the frontier dashboard REST endpoints (offline, in-memory)."""

from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from openlens.api import frontier_state
from openlens.api.routes import ALLOW_OPEN_ADMIN_ENV
from openlens.main import create_app
from openlens.storage.frontier import CrawlCheckpoint, SqliteFrontierStore

from tests.unit.test_api import FakeEngine


@pytest.fixture(autouse=True)
def _allow_open_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Legacy local-dev contract under test: mutations open without a token."""
    monkeypatch.setenv(ALLOW_OPEN_ADMIN_ENV, "true")


@pytest.fixture(autouse=True)
def _isolate_frontier_snapshot() -> None:
    """Reset the shared in-memory snapshot after each test.

    Several tests resume with seeds or append stale seeds; without a
    teardown reset, file execution order leaks them into other tests'
    status views (queue_depth off by the leak).
    """
    yield
    frontier_state.reset_frontier_state()


@pytest.fixture()
def client() -> TestClient:
    frontier_state.reset_frontier_state()
    return TestClient(create_app(engine=FakeEngine()))


def test_status_empty_defaults(client: TestClient) -> None:
    resp = client.get("/api/v1/frontier/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["paused"] is False
    assert body["queue"] == []
    assert body["queue_depth"] == 0
    assert body["checkpoint"]["phase"] == "idle"
    assert body["new_urls"] == []


def test_status_reports_kafka_disabled_by_default(client: TestClient) -> None:
    """Without a kafka-enabled config the status says so explicitly.

    A bare null lag cannot distinguish "queue is store-backed" from
    "probe failed"; the flag lets the dashboard explain which.
    """
    body = client.get("/api/v1/frontier/status").json()
    assert body["kafka_enabled"] is False
    assert body["kafka_lag_total"] is None


def test_status_payload_flags_kafka_enabled() -> None:
    from openlens.api.routes import _frontier_status_payload

    enabled = _frontier_status_payload(
        {
            "kafka": {
                "enabled": True,
                "bootstrap_servers": ["kafka-1:19092"],
                "topic": "crawl.urls",
                "group_id": "crawlers",
            }
        }
    )
    assert enabled.kafka_enabled is True
    disabled = _frontier_status_payload({"kafka": {"enabled": False}})
    assert disabled.kafka_enabled is False


def test_pause_and_resume_cycle(client: TestClient) -> None:
    paused = client.post("/api/v1/frontier/pause")
    assert paused.status_code == 200
    assert paused.json()["paused"] is True
    assert paused.json()["checkpoint"]["phase"] == "paused"

    resumed = client.post("/api/v1/frontier/resume", json={})
    assert resumed.status_code == 200
    assert resumed.json()["paused"] is False
    assert resumed.json()["checkpoint"]["phase"] == "running"


def _file_store(monkeypatch: pytest.MonkeyPatch, tmp_path) -> str:
    """File-backed frontier store with a fresh handle per open.

    Mirrors production (each endpoint open is a new connection to the
    same DB) so endpoint-driven ``close()`` calls never break assertions.
    """
    from openlens.api import routes as api_routes

    db_path = str(tmp_path / "frontier.db")
    seed = SqliteFrontierStore(db_path)
    try:
        seed.save_settings({})
    finally:
        seed.close()
    monkeypatch.setattr(
        api_routes, "open_frontier_store", lambda _config: SqliteFrontierStore(db_path)
    )
    return db_path


def _row_statuses(db_path: str) -> dict[str, str]:
    probe = SqliteFrontierStore(db_path)
    try:
        return {
            row["url"]: row["status"]
            for row in probe._conn.execute(
                "SELECT url, status FROM frontier_queue"
            ).fetchall()
        }
    finally:
        probe.close()


def test_pause_reaps_expired_leases_but_keeps_live_ones(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Pause returns dead workers' rows to queued; live rows stay active."""
    db_path = _file_store(monkeypatch, tmp_path)
    setup = SqliteFrontierStore(db_path)
    try:
        assert setup.enqueue("https://dead.test/a")
        assert setup.enqueue("https://live.test/b")
        assert setup.claim("dead-worker") is not None
        assert setup.claim("live-worker") is not None
        # Dead worker never heartbeats again: backdate past the TTL.
        with setup._lock:
            setup._conn.execute(
                "UPDATE frontier_queue SET heartbeat_at = ?"
                " WHERE owner = 'dead-worker'",
                ("2000-01-01T00:00:00+00:00",),
            )
            setup._conn.commit()
        assert setup.heartbeat("live-worker") == 1
    finally:
        setup.close()

    paused = client.post("/api/v1/frontier/pause")
    assert paused.status_code == 200

    statuses = _row_statuses(db_path)
    assert statuses["https://dead.test/a"] == "queued"
    assert statuses["https://live.test/b"] == "active"


def test_quiesce_returns_when_no_live_work() -> None:
    from openlens.api.routes import _quiesce_in_flight

    class _Store:
        def __init__(self) -> None:
            self.reaps = 0

        def reap_expired_leases(self) -> int:
            self.reaps += 1
            return 0

        def counts(self) -> dict[str, int]:
            return {"active": 0, "queued": 3}

    store = _Store()
    assert _quiesce_in_flight(store) == 0.0
    assert store.reaps == 1


def test_quiesce_reaps_then_proceeds() -> None:
    from openlens.api import routes as api_routes

    class _Store:
        def __init__(self) -> None:
            self.reaps = 0

        def reap_expired_leases(self) -> int:
            self.reaps += 1
            return 1

        def counts(self) -> dict[str, int]:
            # First poll sees the orphan; the reap clears it.
            return {"active": 0 if self.reaps else 1, "queued": 0}

    store = _Store()
    assert api_routes._quiesce_in_flight(store) == 0.0
    assert store.reaps == 1


def test_quiesce_timeout_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    from openlens.api import routes as api_routes

    class _Store:
        def reap_expired_leases(self) -> int:
            return 0

        def counts(self) -> dict[str, int]:
            return {"active": 2, "queued": 0}

    monkeypatch.setattr(api_routes, "_CLEAR_QUIESCE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(api_routes, "_CLEAR_QUIESCE_POLL_S", 0.01)
    waited = api_routes._quiesce_in_flight(_Store())
    assert waited < 5.0


def test_clear_leaves_live_actives_alone(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Quiesce never steals a live fetch: bounded wait, queued still swept."""
    from openlens.api import routes as api_routes

    monkeypatch.setattr(api_routes, "_CLEAR_QUIESCE_TIMEOUT_S", 0.0)
    db_path = _file_store(monkeypatch, tmp_path)
    setup = SqliteFrontierStore(db_path)
    try:
        assert setup.enqueue("https://live.test/a")
        assert setup.enqueue("https://queued.test/b")
        assert setup.claim("live-worker") is not None
        assert setup.heartbeat("live-worker") == 1
    finally:
        setup.close()

    cleared = client.post("/api/v1/frontier/clear")
    assert cleared.status_code == 200
    assert cleared.json()["cleared_queued"] == 1
    assert _row_statuses(db_path) == {"https://live.test/a": "active"}


def test_clear_sweeps_reaped_orphans(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Pause-then-clear empties the queue even with a dead worker's rows."""
    db_path = _file_store(monkeypatch, tmp_path)
    setup = SqliteFrontierStore(db_path)
    try:
        assert setup.enqueue("https://dead.test/a")
        assert setup.enqueue("https://queued.test/b")
        assert setup.claim("dead-worker") is not None
        with setup._lock:
            setup._conn.execute(
                "UPDATE frontier_queue SET heartbeat_at = ?"
                " WHERE owner = 'dead-worker'",
                ("2000-01-01T00:00:00+00:00",),
            )
            setup._conn.commit()
    finally:
        setup.close()

    cleared = client.post("/api/v1/frontier/clear")
    assert cleared.status_code == 200
    assert cleared.json()["cleared_queued"] == 2
    assert _row_statuses(db_path) == {}


def test_resume_with_seeds_replaces_queue(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/frontier/resume", json={"seeds": ["https://example.com/a", "  "]}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["queue_depth"] == 1
    assert body["queue"][0]["url"] == "https://example.com/a"
    assert body["queue"][0]["depth"] == 0


def test_frontier_live_streams_status_snapshots(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live socket pushes the status view, then keeps pushing."""
    monkeypatch.setattr("openlens.api.routes.FRONTIER_LIVE_INTERVAL_S", 0.05)
    with client.websocket_connect("/api/v1/frontier/live") as ws:
        first = ws.receive_json()
        second = ws.receive_json()
    for message in (first, second):
        assert message["paused"] is False
        assert message["queue"] == []
        assert message["checkpoint"]["phase"] == "idle"
    # A resume between connects is visible on the next socket.
    resumed = client.post(
        "/api/v1/frontier/resume", json={"seeds": ["https://example.com/live"]}
    )
    assert resumed.status_code == 200
    with client.websocket_connect("/api/v1/frontier/live") as ws:
        latest = ws.receive_json()
    assert latest["queue"][0]["url"] == "https://example.com/live"


def test_settings_round_trip_and_strict_body(client: TestClient) -> None:
    got = client.get("/api/v1/frontier/settings")
    assert got.status_code == 200
    assert got.json()["settings"]["max_pages"] == 100

    updated = client.put(
        "/api/v1/frontier/settings",
        json={
            "max_pages": 10,
            "max_queued_urls": 50,
            "crawl_delay_s": 1.5,
            "default_max_depth": 2,
            "respect_robots": False,
        },
    )
    assert updated.status_code == 200
    assert updated.json()["settings"]["max_pages"] == 10
    assert updated.json()["settings"]["respect_robots"] is False

    rejected = client.put("/api/v1/frontier/settings", json={"bogus_key": 1})
    assert rejected.status_code == 422
    body = rejected.json()
    assert set(body.keys()) == {"error"}


def test_status_overlays_store_truth(tmp_path) -> None:
    """GET /status merges store rows/checkpoint with snapshot seeds."""
    client = TestClient(create_app(engine=FakeEngine()))
    db = str(tmp_path / "status.db")
    config = dict(client.app.state.config)
    config["storage"] = {"backend": "sqlite", "sqlite": {"db_path": db}}
    client.app.state.config = config

    resumed = client.post(
        "/api/v1/frontier/resume", json={"seeds": ["https://example.com/seed"]}
    )
    assert resumed.status_code == 200

    store = SqliteFrontierStore(db)
    try:
        assert store.enqueue("https://example.com/crawled", 1) is True
        store.save_checkpoint(
            CrawlCheckpoint(
                crawl_id="default",
                seeds=[],
                phase="running",
                pages_attempted=3,
                pages_indexed=2,
                frontier_size=5,
                stats={},
            )
        )
    finally:
        store.close()
    # Snapshot-only seed (e.g. still sitting in the Kafka topic).
    frontier_state.get_frontier_state().queue.append(
        {"url": "https://example.com/pending-seed", "depth": 0, "status": "queued"}
    )

    body = client.get("/api/v1/frontier/status").json()
    assert body["checkpoint"] == {
        "phase": "running",
        "pages_attempted": 3,
        "pages_indexed": 2,
    }
    urls = {entry["url"]: entry for entry in body["queue"]}
    assert urls["https://example.com/crawled"]["depth"] == 1
    assert urls["https://example.com/crawled"]["status"] == "queued"
    assert urls["https://example.com/seed"]["status"] == "queued"
    assert urls["https://example.com/pending-seed"]["status"] == "queued"
    assert body["queue_depth"] == 2


def test_statuses_of_returns_known_statuses_bulk(tmp_path) -> None:
    """Bulk terminal-row lookup: known URLs mapped, unknown omitted."""
    store = SqliteFrontierStore(str(tmp_path / "bulk.db"))
    try:
        assert store.enqueue("https://example.com/a") is True
        assert store.enqueue("https://example.com/b") is True
        assert store.enqueue("https://example.com/c") is True
        store.mark_failed("https://example.com/b", "boom")
        got = store.statuses_of(
            [
                "https://example.com/a",
                "https://example.com/b",
                "https://example.com/c",
                "https://example.com/nope",
                "not a url",
            ]
        )
        assert got == {
            "https://example.com/a": "queued",
            "https://example.com/b": "failed",
            "https://example.com/c": "queued",
        }
        assert store.statuses_of([]) == {}
    finally:
        store.close()


def test_status_overlay_uses_single_bulk_terminal_check(tmp_path) -> None:
    """N snapshot seeds cost one bulk lookup, not N round trips.

    Regression: every status poll and 2s WS push ran one status query
    per in-memory seed, so a dashboard holding thousands of seeds took
    seconds per refresh.
    """
    from openlens.api import routes as routes_mod
    from openlens.api.schemas import FrontierStatusSchema

    class _CountingStore(SqliteFrontierStore):
        def __init__(self, db_path: str) -> None:
            self.single_lookups = 0
            super().__init__(db_path)

        def status_of(self, url: str) -> str | None:
            self.single_lookups += 1
            return super().status_of(url)

    store = _CountingStore(str(tmp_path / "overlay.db"))
    try:
        assert store.enqueue("https://example.com/seed-one") is True
        assert store.enqueue("https://example.com/seed-two") is True
        store.mark_done("https://example.com/seed-two")
        payload = FrontierStatusSchema(
            queue_depth=0,
            queue=[
                {"url": "https://example.com/seed-one", "depth": 0},
                {"url": "https://example.com/seed-two", "depth": 0},
                {"url": "https://example.com/seed-unknown", "depth": 0},
            ],
        )
        merged = routes_mod._overlay_store_status(payload, store)
        urls = {entry.url: entry for entry in merged.queue}
        assert urls["https://example.com/seed-one"].status == "queued"
        assert urls["https://example.com/seed-unknown"].status == "queued"
        assert "https://example.com/seed-two" not in urls
        assert store.single_lookups == 0
    finally:
        store.close()


def test_pending_branch_sql_probes_single_status() -> None:
    """Each paging branch probes one status: equality + order + limit.

    Regression: ``WHERE status IN (...) ORDER BY ... LIMIT n`` seq-scans
    the whole backlog past ~25% queued selectivity (4.5 s measured on
    prod); single-status branches are pure index walks, merged in Python
    (SQLite rejects compound selects as derived tables).
    """
    from openlens.storage.frontier import _merge_pending, _pending_branch_sql

    pg = _pending_branch_sql('"public".frontier_queue', "%s", "queued")
    assert pg.count("%s") == 1
    assert "WHERE status = 'queued'" in pg
    assert "ORDER BY priority DESC, depth ASC, discovered_at ASC" in pg
    assert "UNION" not in pg
    lite = _pending_branch_sql("frontier_queue", "?", "active", tiebreak=", rowid ASC")
    assert lite.count("?") == 1
    assert "WHERE status = 'active'" in lite
    assert "rowid ASC" in lite
    # Merge favors higher priority, then shallower depth, then older row.
    assert _merge_pending([], 10, 0) == []
    pages: list[list[tuple[tuple[object, ...], str]]] = [
        [[(0, 0, "2026-01-02"), "queued-second"]],
        [[(-7, 0, "2026-01-01"), "active-first"]],
    ]
    assert _merge_pending(pages, 10, 0) == ["active-first", "queued-second"]
    assert _merge_pending(pages, 10, 1) == ["queued-second"]


def test_pending_union_matches_legacy_ordering(tmp_path) -> None:
    """Branch-merge paging returns the same global order, incl. offsets."""
    store = SqliteFrontierStore(str(tmp_path / "page.db"))
    try:
        assert store.enqueue("https://example.com/top", 0, priority=7) is True
        assert store.enqueue("https://example.com/mid-shallow", 0, priority=5) is True
        assert store.enqueue("https://example.com/mid-deep", 1, priority=5) is True
        assert store.enqueue("https://example.com/low", 0, priority=0) is True
        claimed = store.claim("worker-1")
        assert claimed is not None and claimed.url == "https://example.com/top"
        first = [e.url for e in store.pending(limit=2)]
        assert first == [
            "https://example.com/top",
            "https://example.com/mid-shallow",
        ]
        rest = [e.url for e in store.pending(limit=10, offset=1)]
        assert rest == [
            "https://example.com/mid-shallow",
            "https://example.com/mid-deep",
            "https://example.com/low",
        ]
        assert [e.url for e in store.pending(limit=10)] == [
            "https://example.com/top",
            *rest,
        ]
    finally:
        store.close()


def test_reseed_many_revives_only_terminal(tmp_path) -> None:
    """Bulk reseed revives terminal rows, skips queued/unknown/garbage."""
    store = SqliteFrontierStore(str(tmp_path / "reseedmany.db"))
    try:
        assert store.enqueue("https://example.com/a") is True
        assert store.enqueue("https://example.com/b") is True
        assert store.enqueue("https://example.com/c") is True
        store.mark_failed("https://example.com/a", "boom")
        store.mark_done("https://example.com/b")
        revived = store.reseed_many(
            [
                "https://example.com/a",
                "https://example.com/b",
                "https://example.com/c",
                "https://example.com/nope",
                "not a url",
            ]
        )
        assert revived == 2
        assert store.status_of("https://example.com/a") == "queued"
        assert store.status_of("https://example.com/b") == "queued"
        assert store.status_of("https://example.com/c") == "queued"
        assert store.reseed_many([]) == 0
    finally:
        store.close()


def test_counts_covers_every_status_plus_total(tmp_path) -> None:
    """counts() contract both backends share: per-status keys + total."""
    store = SqliteFrontierStore(str(tmp_path / "counts.db"))
    try:
        assert store.enqueue("https://example.com/a") is True
        assert store.enqueue("https://example.com/b") is True
        assert store.enqueue("https://example.com/c") is True
        store.mark_failed("https://example.com/a", "boom")
        store.mark_done("https://example.com/b")
        assert store.counts() == {
            "total": 3,
            "queued": 1,
            "failed": 1,
            "done": 1,
        }
    finally:
        store.close()


def test_reseed_revives_terminal_rows(tmp_path) -> None:
    """Store-level: failed/skipped/done rows go back to queued once."""
    store = SqliteFrontierStore(str(tmp_path / "reseed.db"))
    try:
        assert store.enqueue("https://example.com/a", 0) is True
        assert store.enqueue("https://example.com/b", 0) is True
        assert store.enqueue("https://example.com/c", 0) is True
        assert store.enqueue("https://example.com/d", 0) is True
        store.mark_failed("https://example.com/a", "boom")
        assert store.reseed("https://example.com/a") is True
        assert store.status_of("https://example.com/a") == "queued"
        assert store.reseed("https://example.com/b") is False
        assert store.reseed("https://example.com/nope") is False
    finally:
        store.close()


def test_resume_reseeds_failed_seed_for_refetch(tmp_path) -> None:
    """HTTP-level: Resume revives a failed seed instead of no-op'ing."""
    frontier_state.reset_frontier_state()
    client = TestClient(create_app(engine=FakeEngine()))
    db = str(tmp_path / "reresume.db")
    config = dict(client.app.state.config)
    config["storage"] = {"backend": "sqlite", "sqlite": {"db_path": db}}
    client.app.state.config = config

    store = SqliteFrontierStore(db)
    try:
        assert store.enqueue("https://example.com/retry", 0) is True
        store.mark_failed("https://example.com/retry", "disallowed by robots.txt")
    finally:
        store.close()
    resumed = client.post(
        "/api/v1/frontier/resume", json={"seeds": ["https://example.com/retry"]}
    )
    assert resumed.status_code == 200, resumed.text
    check = SqliteFrontierStore(db)
    try:
        assert check.status_of("https://example.com/retry") == "queued"
    finally:
        check.close()


def test_status_snapshot_entry_matching_terminal_store_row_not_shown_as_queued(
    tmp_path,
) -> None:
    """A raw snapshot URL must not shadow its terminal store row as queued.

    The store keys rows by normalized URL; the in-memory snapshot keeps
    the raw seed (trailing slash). Without normalization the dashboard
    shows a failed/done URL as still queued.
    """
    frontier_state.reset_frontier_state()
    client = TestClient(create_app(engine=FakeEngine()))
    db = str(tmp_path / "shadow.db")
    config = dict(client.app.state.config)
    config["storage"] = {"backend": "sqlite", "sqlite": {"db_path": db}}
    client.app.state.config = config

    store = SqliteFrontierStore(db)
    try:
        assert store.enqueue("https://example.com/article/", 0) is True
        store.mark_failed("https://example.com/article/", "max_bytes abort")
    finally:
        store.close()
    # Stale in-memory snapshot entry with the un-normalized seed URL.
    frontier_state.get_frontier_state().queue.append(
        {"url": "https://example.com/article/", "depth": 0, "status": "queued"}
    )

    body = client.get("/api/v1/frontier/status").json()
    urls = {entry["url"]: entry for entry in body["queue"]}
    # Terminal store row wins: the stale in-memory seed disappears
    # instead of reshowing as queued.
    assert urls == {}

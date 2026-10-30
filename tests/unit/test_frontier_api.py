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


def test_pause_and_resume_cycle(client: TestClient) -> None:
    paused = client.post("/api/v1/frontier/pause")
    assert paused.status_code == 200
    assert paused.json()["paused"] is True
    assert paused.json()["checkpoint"]["phase"] == "paused"

    resumed = client.post("/api/v1/frontier/resume", json={})
    assert resumed.status_code == 200
    assert resumed.json()["paused"] is False
    assert resumed.json()["checkpoint"]["phase"] == "running"


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

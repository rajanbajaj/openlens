"""Crawler worker visibility + multi-worker progress without collision.

Offline only: SQLite ``:memory:`` store, ``WorkerLease`` claim driver,
and the FastAPI TestClient over a fake engine (same offline rules as
``test_frontier.py`` / ``test_frontier_api.py``).
"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from openlens.api import frontier_state
from openlens.api.routes import FRONTIER_ADMIN_HEADER, FRONTIER_ADMIN_TOKEN_ENV
from openlens.main import create_app
from openlens.indexing.worker import heartbeat_once
from openlens.storage.frontier import (
    SqliteFrontierStore,
    WorkerLease,
)

from tests.unit.test_api import FakeEngine


@pytest.fixture()
def client() -> TestClient:
    frontier_state.reset_frontier_state()
    return TestClient(create_app(engine=FakeEngine()))


# --- worker visibility -----------------------------------------------------


def test_workers_empty_by_default(client: TestClient) -> None:
    resp = client.get("/api/v1/frontier/workers")
    assert resp.status_code == 200
    assert resp.json() == {"workers": [], "worker_count": 0}


def test_heartbeat_registers_and_status_includes_workers(
    client: TestClient,
) -> None:
    first = client.post(
        "/api/v1/frontier/workers/heartbeat",
        json={"worker_id": "worker-a", "crawl_id": "default", "claimed": 2},
    )
    assert first.status_code == 200
    assert first.json()["worker_count"] == 1

    second = client.post(
        "/api/v1/frontier/workers/heartbeat",
        json={"worker_id": "worker-b", "claimed": 0},
    )
    assert second.json()["worker_count"] == 2
    ids = {w["worker_id"] for w in second.json()["workers"]}
    assert ids == {"worker-a", "worker-b"}

    status = client.get("/api/v1/frontier/status")
    assert status.status_code == 200
    assert {w["worker_id"] for w in status.json()["workers"]} == {
        "worker-a",
        "worker-b",
    }


def test_stale_workers_pruned_on_read() -> None:
    frontier_state.reset_frontier_state()
    frontier_state.heartbeat_worker("old-worker", now=1000.0)
    frontier_state.heartbeat_worker("new-worker", now=1000.0)
    live = frontier_state.list_workers(now=1000.0 + 10.0)
    assert {entry["worker_id"] for entry in live} == {"old-worker", "new-worker"}
    live = frontier_state.list_workers(
        now=1000.0 + frontier_state.WORKER_STALE_AFTER_S + 1.0
    )
    assert live == []


def test_worker_heartbeat_refuses_non_http_urls() -> None:
    with pytest.raises(ValueError):
        heartbeat_once("file:///etc/passwd", "worker-a")
    with pytest.raises(ValueError):
        heartbeat_once("gopher://example.com/", "worker-a")


def test_heartbeat_rejects_blank_worker_id(client: TestClient) -> None:
    resp = client.post("/api/v1/frontier/workers/heartbeat", json={"worker_id": "   "})
    assert resp.status_code == 422
    assert set(resp.json().keys()) == {"error"}


def test_heartbeat_rejects_unknown_keys(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/frontier/workers/heartbeat",
        json={"worker_id": "worker-a", "bogus_key": 1},
    )
    assert resp.status_code == 422
    assert set(resp.json().keys()) == {"error"}


def test_heartbeat_admin_gate(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(FRONTIER_ADMIN_TOKEN_ENV, "s3cret")

    denied = client.post(
        "/api/v1/frontier/workers/heartbeat", json={"worker_id": "worker-a"}
    )
    assert denied.status_code == 403
    assert set(denied.json().keys()) == {"error"}

    wrong = client.post(
        "/api/v1/frontier/workers/heartbeat",
        json={"worker_id": "worker-a"},
        headers={FRONTIER_ADMIN_HEADER: "nope"},
    )
    assert wrong.status_code == 403

    # Reads stay open while mutations are gated.
    assert client.get("/api/v1/frontier/workers").status_code == 200
    assert client.get("/api/v1/frontier/status").status_code == 200

    allowed = client.post(
        "/api/v1/frontier/workers/heartbeat",
        json={"worker_id": "worker-a"},
        headers={FRONTIER_ADMIN_HEADER: "s3cret"},
    )
    assert allowed.status_code == 200
    assert allowed.json()["worker_count"] == 1


# --- multi-worker progress without collision -------------------------------


def test_two_workers_claim_distinct_urls_without_collision() -> None:
    """Two leases sharing one store never double-claim; all work finishes."""
    store = SqliteFrontierStore(":memory:")
    try:
        urls = [f"https://example.com/page-{i}" for i in range(20)]
        for url in urls:
            assert store.enqueue(url, 0) is True

        claimed: dict[str, str] = {}
        guard = threading.Lock()

        def run(worker_id: str) -> None:
            lease = WorkerLease(store, worker_id=worker_id)
            lease.start()
            try:
                while True:
                    entry = lease.claim()
                    if entry is None:
                        return
                    with guard:
                        assert entry.url not in claimed, f"{entry.url} claimed twice"
                        claimed[entry.url] = worker_id
                    store.mark_done(entry.url)
            finally:
                lease.shutdown()

        threads = [
            threading.Thread(target=run, args=(f"worker-{i}",)) for i in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        assert all(not thread.is_alive() for thread in threads)

        assert sorted(claimed.keys()) == sorted(urls)
        counts = store.counts()
        assert counts.get("done") == len(urls)
        assert counts.get("queued", 0) == 0
        assert counts.get("active", 0) == 0
    finally:
        store.close()


def test_two_api_heartbeats_coexist_without_collision(
    client: TestClient,
) -> None:
    """Concurrent heartbeats from N workers all land; none lost."""
    errors: list[BaseException] = []

    def beat(worker_id: str) -> None:
        try:
            resp = client.post(
                "/api/v1/frontier/workers/heartbeat",
                json={"worker_id": worker_id, "claimed": 1},
            )
            assert resp.status_code == 200
        except BaseException as exc:  # noqa: BLE001 — collected, then raised
            errors.append(exc)

    threads = [
        threading.Thread(target=beat, args=(f"api-worker-{i}",)) for i in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not errors
    body = client.get("/api/v1/frontier/workers").json()
    assert body["worker_count"] == 4

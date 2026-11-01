"""Discovery observability: heartbeat discovery summaries + pass logging.

Offline only: in-memory frontier state over the FastAPI TestClient with
a fake engine, ``:memory:`` SQLite frontier stores, and hermetic fake
feed fetches (no network).
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient

from openlens.api import frontier_state
from openlens.api.routes import ALLOW_OPEN_ADMIN_ENV
from openlens.indexing.worker import CrawlerWorker, heartbeat_once
from openlens.main import create_app
from openlens.storage.frontier import SqliteFrontierStore

from tests.unit.test_api import FakeEngine


@pytest.fixture(autouse=True)
def _allow_open_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Legacy local-dev contract under test: mutations open without a token."""
    monkeypatch.setenv(ALLOW_OPEN_ADMIN_ENV, "true")


@pytest.fixture()
def client() -> TestClient:
    frontier_state.reset_frontier_state()
    return TestClient(create_app(engine=FakeEngine()))


# --- heartbeat round-trip ----------------------------------------------------


def test_heartbeat_round_trips_discovery_summary(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/frontier/workers/heartbeat",
        json={
            "worker_id": "worker-a",
            "claimed": 1,
            "discovery": {
                "at": 1700000000.0,
                "feeds_checked": 2,
                "new_urls": 1,
                "refreshed": 3,
            },
        },
    )
    assert resp.status_code == 200
    workers = {w["worker_id"]: w for w in resp.json()["workers"]}
    assert workers["worker-a"]["discovery"] == {
        "worker_id": "",
        "at": 1700000000.0,
        "feeds_checked": 2,
        "new_urls": 1,
        "refreshed": 3,
    }

    listed = client.get("/api/v1/frontier/workers").json()
    assert listed["worker_count"] == 1
    assert listed["workers"][0]["discovery"]["new_urls"] == 1


def test_heartbeat_once_sends_discovery_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    class _Response:
        def read(self) -> bytes:
            return b"{}"

        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

    def fake_urlopen(request: Any, timeout: Any = None) -> _Response:
        import json as jsonlib

        seen.update(jsonlib.loads(request.data.decode()))
        return _Response()

    monkeypatch.setattr("openlens.indexing.worker.urllib.request.urlopen", fake_urlopen)
    heartbeat_once(
        "http://api.test",
        "worker-a",
        discovery={"feeds_checked": 2, "new_urls": 1, "refreshed": 0},
    )
    assert seen["worker_id"] == "worker-a"
    assert seen["discovery"]["feeds_checked"] == 2
    assert seen["discovery"]["new_urls"] == 1
    assert seen["discovery"]["refreshed"] == 0
    assert seen["discovery"]["at"] > 0


# --- status aggregation ------------------------------------------------------


def test_status_aggregates_latest_reporting_worker(client: TestClient) -> None:
    assert client.get("/api/v1/frontier/status").json()["discovery"] is None

    first = client.post(
        "/api/v1/frontier/workers/heartbeat",
        json={
            "worker_id": "worker-a",
            "discovery": {
                "at": 1700000000.0,
                "feeds_checked": 1,
                "new_urls": 1,
                "refreshed": 0,
            },
        },
    )
    assert first.status_code == 200
    second = client.post(
        "/api/v1/frontier/workers/heartbeat",
        json={
            "worker_id": "worker-b",
            "discovery": {
                "at": 1700000001.0,
                "feeds_checked": 4,
                "new_urls": 5,
                "refreshed": 6,
            },
        },
    )
    assert second.status_code == 200
    # A newer heartbeat without a summary must not clobber the aggregate.
    third = client.post(
        "/api/v1/frontier/workers/heartbeat", json={"worker_id": "worker-c"}
    )
    assert third.status_code == 200

    body = client.get("/api/v1/frontier/status").json()
    assert body["discovery"] == {
        "worker_id": "worker-b",
        "at": 1700000001.0,
        "feeds_checked": 4,
        "new_urls": 5,
        "refreshed": 6,
    }


# --- malformed discovery degrades cleanly ------------------------------------


@pytest.mark.parametrize("bad", ["bogus", [1, 2], 42, True])
def test_malformed_discovery_values_degrade_to_null(
    client: TestClient, bad: Any
) -> None:
    resp = client.post(
        "/api/v1/frontier/workers/heartbeat",
        json={"worker_id": "worker-a", "discovery": bad},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["workers"][0]["discovery"] is None
    assert client.get("/api/v1/frontier/status").json()["discovery"] is None


def test_malformed_discovery_counts_coerce_to_zero(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/frontier/workers/heartbeat",
        json={
            "worker_id": "worker-a",
            "discovery": {
                "at": "not-a-time",
                "feeds_checked": "many",
                "new_urls": -3,
                "refreshed": None,
            },
        },
    )
    assert resp.status_code == 200, resp.text
    summary = resp.json()["workers"][0]["discovery"]
    assert summary["feeds_checked"] == 0
    assert summary["new_urls"] == 0
    assert summary["refreshed"] == 0
    assert summary["at"] > 0


def test_unknown_heartbeat_keys_still_rejected(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/frontier/workers/heartbeat",
        json={"worker_id": "worker-a", "bogus_key": 1},
    )
    assert resp.status_code == 422


# --- discovery pass logging --------------------------------------------------


def _worker_with_feeds(store: SqliteFrontierStore, feeds: list[str]) -> CrawlerWorker:
    from openlens.indexing.crawler import PoliteCrawler

    crawler = PoliteCrawler()
    crawler.configure({"default_max_depth": 3})
    worker = CrawlerWorker(crawler=crawler, repository=None, frontier_store=store)
    worker.discovery_feeds = list(feeds)
    return worker


def test_warn_log_fires_on_feed_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = SqliteFrontierStore(":memory:")
    try:
        worker = _worker_with_feeds(store, ["https://news.example/feed.xml"])

        def boom(url: str, **kwargs: Any) -> str:
            raise ConnectionError("dns down")

        with caplog.at_level(logging.WARNING, logger="openlens.indexing.worker"):
            result = worker.run_discovery_pass(http_fetch=boom)
        assert result == {"feeds_checked": 1, "new_urls": 0, "refreshed": 0}
        warnings = [
            record
            for record in caplog.records
            if record.levelno >= logging.WARNING
            and record.name == "openlens.indexing.worker"
        ]
        assert warnings, "expected a warning log on feed fetch failure"
        assert any(
            "https://news.example/feed.xml" in record.getMessage()
            for record in warnings
        )
    finally:
        store.close()


def test_warn_log_fires_on_empty_feed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = SqliteFrontierStore(":memory:")
    try:
        worker = _worker_with_feeds(store, ["https://news.example/empty.xml"])
        with caplog.at_level(logging.WARNING, logger="openlens.indexing.worker"):
            result = worker.run_discovery_pass(
                http_fetch=lambda url, **kwargs: "",
            )
        assert result["feeds_checked"] == 1
        assert result["new_urls"] == 0
        assert any(
            "https://news.example/empty.xml" in record.getMessage()
            for record in caplog.records
            if record.name == "openlens.indexing.worker"
        )
    finally:
        store.close()


def test_info_log_reports_pass_counts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from tests.unit.test_discovery_worker import FEED_XML

    store = SqliteFrontierStore(":memory:")
    try:
        worker = _worker_with_feeds(store, ["https://news.example/feed.xml"])
        with caplog.at_level(logging.INFO, logger="openlens.indexing.worker"):
            result = worker.run_discovery_pass(
                http_fetch=lambda url, **kwargs: FEED_XML,
            )
        assert result["new_urls"] == 2
        assert any(
            "feeds_checked=1" in record.getMessage()
            and "new_urls=2" in record.getMessage()
            for record in caplog.records
            if record.levelno == logging.INFO
            and record.name == "openlens.indexing.worker"
        )
    finally:
        store.close()

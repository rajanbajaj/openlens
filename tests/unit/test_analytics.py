"""Analytics pipeline tests (offline, no broker, no Postgres server)."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient
import pytest

from openlens.analytics.events import (
    feedback_event,
    hash_query,
    normalize_event,
    served_event,
)
from openlens.analytics.store import AnalyticsStore
from openlens.analytics.worker import run_analytics_worker
from openlens.api import frontier_state
from openlens.api.routes import ALLOW_OPEN_ADMIN_ENV
from openlens.main import create_app

from tests.unit.test_api import FakeEngine


@pytest.fixture(autouse=True)
def _allow_open_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ALLOW_OPEN_ADMIN_ENV, "true")


@pytest.fixture()
def client() -> TestClient:
    frontier_state.reset_frontier_state()
    return TestClient(create_app(engine=FakeEngine()))


def test_normalize_event_rejects_unknown() -> None:
    assert normalize_event(None) is None
    assert normalize_event({"name": "nope", "props": {}}) is None
    assert (
        normalize_event({"name": "search.served", "source": "client", "props": {}})
        is None
    )


def test_normalize_event_allowlists_props() -> None:
    event = normalize_event(
        {
            "name": "search.feedback",
            "props": {
                "doc_id": "https://example.com/a",
                "action": "helpful",
                "evil": "drop me",
                "reason": "x" * 500,
            },
        }
    )
    assert event is not None
    assert event.name == "search.feedback"
    assert event.props["doc_id"] == "https://example.com/a"
    assert "evil" not in event.props
    assert len(event.props["reason"]) <= 256


def test_query_hash_never_stores_raw_text() -> None:
    digest = hash_query("  Array Sorting  ")
    assert digest == hash_query("array sorting")
    assert "array" not in digest
    assert len(digest) == 64


def test_feedback_and_served_event_shapes() -> None:
    assert feedback_event("d", "helpful", None).props == {
        "doc_id": "d",
        "action": "helpful",
    }
    served = served_event("Python", "news", 7)
    assert served.name == "search.served"
    assert served.props["mode"] == "news"
    assert served.props["result_count"] == 7
    assert served.props["query_hash"] == hash_query("Python")


def test_store_batch_insert_counts_and_purge(tmp_path) -> None:
    store = AnalyticsStore(str(tmp_path / "a.db"))
    try:
        now = time.time()
        inserted = store.insert_many(
            [
                feedback_event("d1", "helpful", None).to_payload(),
                served_event("q", "default", 3).to_payload(),
                {"name": "search.served", "at": now - 100 * 86400, "props": {}},
                {"bogus": True},
                {"name": "", "at": now, "props": {}},
            ]
        )
        assert inserted == 3
        counts = store.counts_by_name(since_days=30)
        assert counts == {"search.feedback": 1, "search.served": 1}
        assert store.counts_by_name(since_days=200).get("search.served") == 2
        assert store.purge_older_than(90) == 1
        assert store.counts_by_name(since_days=200).get("search.served") == 1
    finally:
        store.close()


class _FakeAnalyticsQueue:
    def __init__(self, script: list[list[dict]]) -> None:
        self._script = [list(batch) for batch in script]
        self.commits = 0
        self.published: list[dict] = []

    def poll_events(self, timeout_ms: int = 0, max_records: int = 500):
        if not self._script:
            return []
        return self._script.pop(0)[:max_records]

    def commit(self) -> None:
        self.commits += 1

    def close(self) -> None:
        pass

    def publish_event(self, event) -> bool:
        self.published.append(event.to_payload())
        return True


def test_worker_consumes_batches_and_purges(tmp_path) -> None:
    store = AnalyticsStore(str(tmp_path / "w.db"))
    old = dict(served_event("old", "default", 1).to_payload())
    old["at"] = time.time() - 100 * 86400
    assert store.insert_many([old]) == 1
    queue = _FakeAnalyticsQueue(
        [
            [
                feedback_event("d1", "helpful", None).to_payload(),
                {"not": "an event"},
                {"name": "unknown.name", "props": {}},
            ]
        ]
    )
    counts = run_analytics_worker(
        # Sub-resolution cadence: the retention branch fires on the first
        # pass deterministically, without sleeping.
        {"analytics": {"retention_days": 90, "retention_check_s": 1e-9}},
        queue=queue,
        store=store,
        max_iterations=3,
    )
    assert counts["received"] == 3
    assert counts["stored"] == 1
    assert counts["dropped"] == 2
    assert counts["purged"] == 1
    assert queue.commits >= 1
    # The worker owns and closes the store; read back through a fresh
    # handle to prove the batch is durable.
    check = AnalyticsStore(str(tmp_path / "w.db"))
    try:
        assert check.counts_by_name(since_days=200) == {"search.feedback": 1}
    finally:
        check.close()


def test_feedback_publishes_event_but_survives_no_broker(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[dict] = []

    class _Queue:
        @classmethod
        def from_app_config(cls, config, **kwargs):
            return cls()

        def publish_event(self, event) -> bool:
            captured.append(event.to_payload())
            return True

    monkeypatch.setattr("openlens.analytics.events.AnalyticsQueue", _Queue)
    resp = client.post(
        "/api/v1/feedback",
        json={"doc_id": "https://example.com/a", "action": "helpful"},
    )
    assert resp.status_code == 200
    assert len(captured) == 1
    assert captured[0]["name"] == "search.feedback"
    assert captured[0]["props"]["action"] == "helpful"


def test_feedback_still_200_when_broker_down(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Broken:
        @classmethod
        def from_app_config(cls, config, **kwargs):
            raise RuntimeError("no broker")

    monkeypatch.setattr("openlens.analytics.events.AnalyticsQueue", _Broken)
    resp = client.post(
        "/api/v1/feedback",
        json={"doc_id": "https://example.com/a", "action": "helpful"},
    )
    assert resp.status_code == 200


def test_search_publishes_served_event(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[dict] = []

    class _Queue:
        @classmethod
        def from_app_config(cls, config, **kwargs):
            return cls()

        def publish_event(self, event) -> bool:
            captured.append(event.to_payload())
            return True

    monkeypatch.setattr("openlens.analytics.events.AnalyticsQueue", _Queue)
    resp = client.post("/api/v1/search", json={"query": "test", "limit": 1})
    assert resp.status_code == 200
    assert captured, "expected a search.served event"
    assert captured[0]["name"] == "search.served"
    assert "query_hash" in captured[0]["props"]


def test_admin_counts_reads_store(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    store = AnalyticsStore(str(tmp_path / "c.db"))
    try:
        assert (
            store.insert_many([feedback_event("d1", "helpful", None).to_payload()]) == 1
        )
        monkeypatch.setattr(
            "openlens.analytics.store.open_analytics_store", lambda section: store
        )
        resp = client.get("/api/v1/admin/analytics/counts?days=30")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["by_name"] == {"search.feedback": 1}
        assert body["total"] == 1
    finally:
        store.close()


def test_admin_counts_503_when_store_down(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(section):
        raise RuntimeError("no db")

    monkeypatch.setattr("openlens.analytics.store.open_analytics_store", _boom)
    resp = client.get("/api/v1/admin/analytics/counts")
    assert resp.status_code == 503


def _clickhouse_store():
    """Live ClickHouse store, or skip (no driver / no server)."""
    try:
        import clickhouse_connect  # noqa: F401
    except ImportError:
        pytest.skip("clickhouse-connect not installed")
    from openlens.analytics.store import ClickHouseAnalyticsStore
    from openlens.core.interfaces import ProviderError

    try:
        return ClickHouseAnalyticsStore(
            host="localhost",
            port=8123,
            database="openlens_analytics_test",
            user="default",
            password="",
            retention_days=36500,
        )
    except ProviderError:
        pytest.skip("no ClickHouse server on localhost:8123")


def test_clickhouse_roundtrip() -> None:
    store = _clickhouse_store()
    try:
        now = time.time()
        assert (
            store.insert_many(
                [
                    feedback_event("ch1", "helpful", None).to_payload(),
                    dict(
                        served_event("q", "default", 2).to_payload(),
                        at=now - 100 * 86400,
                    ),
                ]
            )
            == 2
        )
        counts = store.counts_by_name(since_days=30)
        assert counts.get("search.feedback", 0) >= 1
        assert store.purge_older_than(90) >= 1
    finally:
        store.close()


def test_cli_rejects_nonpositive_batch_size() -> None:
    import argparse

    from openlens.cli import FAILURE_EXIT, _cmd_analytics_worker

    args = argparse.Namespace(batch_size=0, max_iterations=None)
    assert _cmd_analytics_worker(args) == FAILURE_EXIT

"""Analytics pipeline tests (offline, no broker, no Postgres server)."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient
import pytest

from openlens.analytics.events import (
    crawl_page_event,
    feedback_event,
    hash_query,
    normalize_event,
    served_event,
)
from openlens.analytics.store import AnalyticsStore
from openlens.analytics.worker import run_analytics_worker
from openlens.core.interfaces import ProviderError
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


def test_crawl_page_event_shape_and_normalize() -> None:
    event = crawl_page_event(
        "https://example.com/a", "skipped", "denied by deny_domains"
    )
    assert event.name == "crawl.page"
    assert event.props == {
        "url": "https://example.com/a",
        "outcome": "skipped",
        "reason": "denied by deny_domains",
    }
    normalized = normalize_event(event.to_payload())
    assert normalized is not None
    assert normalized.name == "crawl.page"
    assert normalized.props["outcome"] == "skipped"
    assert normalized.props["reason"] == "denied by deny_domains"
    # Reason is optional; unknown props still dropped.
    bare = normalize_event(
        crawl_page_event("https://example.com/b", "fetched").to_payload()
    )
    assert bare is not None
    assert "reason" not in bare.props
    dropped = normalize_event(
        {"name": "crawl.page", "props": {"url": "u", "outcome": "x", "evil": 1}}
    )
    assert dropped is not None
    assert "evil" not in dropped.props


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


def test_top_props_groups_reasons_and_rejects_junk(tmp_path) -> None:
    store = AnalyticsStore(str(tmp_path / "top.db"))
    try:
        assert (
            store.insert_many(
                [
                    crawl_page_event(
                        "https://a.example/1", "skipped", "denied by deny_domains"
                    ).to_payload(),
                    crawl_page_event(
                        "https://a.example/2", "skipped", "denied by deny_domains"
                    ).to_payload(),
                    crawl_page_event(
                        "https://b.example/1", "failed", "fetch timeout"
                    ).to_payload(),
                    crawl_page_event("https://c.example/1", "fetched").to_payload(),
                ]
            )
            == 4
        )
        assert store.top_props("crawl.page", "reason") == [
            ("denied by deny_domains", 2),
            ("fetch timeout", 1),
        ]
        assert store.top_props("crawl.page", "outcome") == [
            ("skipped", 2),
            ("failed", 1),
            ("fetched", 1),
        ]
        assert store.top_props("crawl.page", "reason", limit=1) == [
            ("denied by deny_domains", 2)
        ]
        assert store.top_props("search.served", "mode") == []
        with pytest.raises(ProviderError):
            store.top_props("nope.unknown", "reason")
        with pytest.raises(ProviderError):
            store.top_props("crawl.page", "reason; DROP TABLE x")
    finally:
        store.close()


def test_window_cutoff_matches_stored_format(tmp_path) -> None:
    """Events written today must count (space, not T, cutoff separator)."""
    store = AnalyticsStore(str(tmp_path / "cutoff.db"))
    try:
        assert store.insert_many([served_event("q", "default", 1).to_payload()]) == 1
        # With the old "T"-separated cutoff, same-day rows compared less
        # than the bound and a 1-day window came back empty.
        assert sum(store.counts_by_name(since_days=1).values()) == 1
    finally:
        store.close()


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


def test_admin_top_groups_and_rejects(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    store = AnalyticsStore(str(tmp_path / "t.db"))
    try:
        assert (
            store.insert_many(
                [
                    crawl_page_event(
                        "https://a.example/1", "skipped", "denied by deny_domains"
                    ).to_payload(),
                    crawl_page_event(
                        "https://b.example/1", "failed", "fetch timeout"
                    ).to_payload(),
                ]
            )
            == 2
        )
        monkeypatch.setattr(
            "openlens.analytics.store.open_analytics_store", lambda section: store
        )
        resp = client.get(
            "/api/v1/admin/analytics/top?name=crawl.page&prop=reason&days=30"
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["name"] == "crawl.page"
        assert body["prop"] == "reason"
        assert body["total"] == 2
        assert {entry["value"] for entry in body["top"]} == {
            "denied by deny_domains",
            "fetch timeout",
        }
        assert client.get("/api/v1/admin/analytics/top?name=bogus").status_code == 422
        assert client.get("/api/v1/admin/analytics/top?prop=a-b").status_code == 422
    finally:
        store.close()


def _dated_crawl_page(url, outcome, reason, at_epoch):
    from urllib.parse import urlsplit

    domain = urlsplit(url).hostname or ""
    payload = crawl_page_event(url, outcome, reason, domain=domain).to_payload()
    payload["at"] = at_epoch
    return payload


def _range_dates():
    """Two UTC day labels: 20 and 10 days ago (both inside 30d, ordered)."""
    import datetime

    today = datetime.datetime.now(datetime.timezone.utc).date()
    old = (today - datetime.timedelta(days=20)).isoformat()
    new = (today - datetime.timedelta(days=10)).isoformat()
    start = (today - datetime.timedelta(days=21)).isoformat()
    mid = (today - datetime.timedelta(days=15)).isoformat()
    end = (today - datetime.timedelta(days=5)).isoformat()
    return old, new, start, mid, end


def _range_day_ts(day_label):
    import datetime

    return (
        datetime.datetime.fromisoformat(day_label)
        .replace(tzinfo=datetime.timezone.utc)
        .timestamp()
    )


def _seed_range_store(tmp_path):
    """Two domains on two days: 20 days ago (alpha x2) and 10 days ago (beta)."""
    from openlens.analytics.store import AnalyticsStore

    old, new, _, _, _ = _range_dates()
    store = AnalyticsStore(str(tmp_path / "range.db"))
    assert (
        store.insert_many(
            [
                _dated_crawl_page(
                    "https://alpha.example/1", "fetched", "ok", _range_day_ts(old)
                ),
                _dated_crawl_page(
                    "https://alpha.example/2", "fetched", "ok", _range_day_ts(old)
                ),
                _dated_crawl_page(
                    "https://beta.example/1", "failed", "http 404", _range_day_ts(new)
                ),
            ]
        )
        == 3
    )
    return store


def test_top_props_range_match_and_offset(tmp_path) -> None:
    old, new, start, mid, end = _range_dates()
    store = _seed_range_store(tmp_path)
    try:
        assert store.top_props("crawl.page", "domain") == [
            ("alpha.example", 2),
            ("beta.example", 1),
        ]
        assert store.top_props("crawl.page", "domain", start=mid, end=end) == [
            ("beta.example", 1)
        ]
        assert store.top_props("crawl.page", "domain", match="ALPHA") == [
            ("alpha.example", 2)
        ]
        assert store.top_props("crawl.page", "domain", limit=1, offset=1) == [
            ("beta.example", 1)
        ]
        assert store.top_props("crawl.page", "domain", limit=1, offset=9) == []
        with pytest.raises(ProviderError):
            store.top_props("crawl.page", "domain", start=new)
        with pytest.raises(ProviderError):
            store.top_props("crawl.page", "domain", start="2026-13-01", end=end)
        with pytest.raises(ProviderError):
            store.top_props("crawl.page", "domain", start=new, end=old)
        with pytest.raises(ProviderError):
            store.top_props("crawl.page", "domain", match="a'b")
        with pytest.raises(ProviderError):
            store.top_props("crawl.page", "domain", match="a%")
    finally:
        store.close()


def test_series_filter_splits_by_outcome(tmp_path) -> None:
    _, _, start, _, end = _range_dates()
    store = _seed_range_store(tmp_path)
    try:
        fetched = store.series(
            "crawl.page",
            start=start,
            end=end,
            filter_prop="outcome",
            filter_value="fetched",
        )
        assert sum(n for _, n in fetched) == 2
        failed = store.series(
            "crawl.page",
            start=start,
            end=end,
            filter_prop="outcome",
            filter_value="failed",
        )
        assert sum(n for _, n in failed) == 1
        with pytest.raises(ProviderError):
            store.series("crawl.page", start=start, end=end, filter_prop="outcome")
        with pytest.raises(ProviderError):
            store.series("crawl.page", filter_prop="outcome", filter_value="a'b")
    finally:
        store.close()


def test_counts_and_series_range(tmp_path) -> None:
    old, new, start, mid, end = _range_dates()
    store = _seed_range_store(tmp_path)
    try:
        assert store.counts_by_name(start=start, end=old) == {"crawl.page": 2}
        assert store.counts_by_name(start=mid, end=end) == {"crawl.page": 1}
        assert sum(n for _, n in store.series("crawl.page", start=start, end=end)) == 3
        assert sum(n for _, n in store.series("crawl.page", start=mid, end=end)) == 1
        with pytest.raises(ProviderError):
            store.series("crawl.page", start="not-a-date", end=end)
    finally:
        store.close()


def test_admin_top_range_and_match_params(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from openlens.analytics.store import AnalyticsStore

    old, _new, start, mid, end = _range_dates()
    seed = _seed_range_store(tmp_path)
    seed.close()
    monkeypatch.setattr(
        "openlens.analytics.store.open_analytics_store",
        lambda section: AnalyticsStore(str(tmp_path / "range.db")),
    )
    resp = client.get(
        f"/api/v1/admin/analytics/top?name=crawl.page&prop=domain&start={mid}&end={end}"
    )
    assert resp.status_code == 200, resp.text
    assert [entry["value"] for entry in resp.json()["top"]] == ["beta.example"]
    resp = client.get(
        "/api/v1/admin/analytics/top?name=crawl.page&prop=domain&match=alpha&limit=1&offset=1"
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["top"] == []
    assert (
        client.get(
            f"/api/v1/admin/analytics/top?name=crawl.page&prop=domain&start={mid}"
        ).status_code
        == 422
    )
    assert (
        client.get(
            "/api/v1/admin/analytics/top?name=crawl.page&prop=domain&match=a%25"
        ).status_code
        == 422
    )
    resp = client.get(f"/api/v1/admin/analytics/counts?start={start}&end={old}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["by_name"] == {"crawl.page": 2}
    assert resp.json()["days"] == 2
    assert (
        client.get(
            f"/api/v1/admin/analytics/counts?start=2026-13-01&end={old}"
        ).status_code
        == 422
    )
    resp = client.get(
        f"/api/v1/admin/analytics/series?name=crawl.page&start={mid}&end={end}"
    )
    assert resp.status_code == 200, resp.text
    assert sum(point["count"] for point in resp.json()["points"]) == 1
    resp = client.get(
        "/api/v1/admin/analytics/series?name=crawl.page"
        f"&start={start}&end={end}&filter_prop=outcome&filter_value=failed"
    )
    assert resp.status_code == 200, resp.text
    assert sum(point["count"] for point in resp.json()["points"]) == 1
    assert (
        client.get(
            "/api/v1/admin/analytics/series?name=crawl.page&filter_prop=outcome"
        ).status_code
        == 422
    )


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


def test_open_analytics_store_defaults_to_shared_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty config opens a shared file store, never a private :memory: db.

    A ``:memory:`` default gives the API and the analytics-worker separate
    empty databases, so admin counts would always read zero.
    """
    from openlens.analytics import store as store_module

    seen: list[str] = []

    class _Fake:
        def __init__(self, db_path: str = ":memory:") -> None:
            seen.append(db_path)

    monkeypatch.setattr(store_module, "AnalyticsStore", _Fake)
    store_module.open_analytics_store({})
    store_module.open_analytics_store(None)
    assert seen == ["data/openlens-analytics.db", "data/openlens-analytics.db"]


def test_clickhouse_insert_serializes_datetimes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """insert_many must hand datetime objects (not strings) to the driver.

    Regression: pre-formatted timestamp strings crashed
    clickhouse-connect with AttributeError, so the analytics-worker never
    stored anything and admin counts read permanently zero.
    """
    import datetime as datetime_mod

    from openlens.analytics import store as store_module

    calls: dict[str, object] = {}

    class _FakeClient:
        def command(self, *args: object, **kwargs: object) -> None:
            return None

        def insert(
            self,
            table: object,
            rows: object,
            column_names: object = None,
        ) -> None:
            calls["rows"] = rows
            calls["table"] = table
            calls["columns"] = column_names

        def close(self) -> None:
            return None

    class _FakeDriver:
        @staticmethod
        def get_client(**kwargs: object) -> _FakeClient:
            return _FakeClient()

    monkeypatch.setattr(store_module, "_load_clickhouse", lambda: _FakeDriver)
    store = store_module.ClickHouseAnalyticsStore(database="test_db")
    try:
        assert store.insert_many([served_event("q", "default", 3).to_payload()]) == 1
        rows = calls["rows"]
        assert isinstance(rows, list) and len(rows) == 1
        at = rows[0][2]
        assert isinstance(at, datetime_mod.datetime), (
            f"expected datetime for the DateTime column, got {type(at)}"
        )
    finally:
        store.close()


def test_analytics_store_creates_parent_dirs(tmp_path) -> None:
    from openlens.analytics.store import AnalyticsStore

    db = tmp_path / "nested" / "analytics.db"
    store = AnalyticsStore(str(db))
    try:
        assert db.exists()
        assert store.counts_by_name(since_days=30) == {}
    finally:
        store.close()


def test_backend_image_installs_analytics_extra() -> None:
    """The release image must ship the ClickHouse driver.

    Compose runs this image as the API and the analytics-worker with
    ``OPENLENS_ANALYTICS_BACKEND=clickhouse``; without the ``analytics``
    extra both sides fail and the admin endpoint 503s with "analytics
    store unavailable".
    """
    from pathlib import Path

    dockerfile = (
        Path(__file__).resolve().parent.parent.parent
        / "deploy"
        / "local"
        / "Dockerfile.backend"
    )
    text = dockerfile.read_text()
    assert "analytics" in text, "Dockerfile.backend must install .[kafka,analytics]"

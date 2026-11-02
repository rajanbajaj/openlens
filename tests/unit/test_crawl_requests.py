"""User-requested crawl URLs: store round-trip, rate scope, route wiring.

Public ``POST /api/v1/crawl-requests`` (rate-limited) records
``pending`` rows deduped by normalized URL; admin review
(``GET /frontier/crawl-requests`` + approve/decline) decides rows,
with approve additionally enqueueing the URL. Legacy ``FrontierStore``
doubles keep instantiating via the concrete defaults.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from openlens.api import frontier_state
from openlens.api import routes as api_routes
from openlens.api.rate_limit import limit_for, scope_for_request
from openlens.api.routes import ALLOW_OPEN_ADMIN_ENV
from openlens.main import create_app
from openlens.storage.frontier import FrontierStore, SqliteFrontierStore

from tests.unit.test_api import FakeEngine


@pytest.fixture(autouse=True)
def _allow_open_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ALLOW_OPEN_ADMIN_ENV, "true")


@pytest.fixture()
def client() -> TestClient:
    frontier_state.reset_frontier_state()
    return TestClient(create_app(engine=FakeEngine()))


@pytest.fixture()
def file_store(monkeypatch: pytest.MonkeyPatch, tmp_path) -> str:
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


def test_submit_dedupes_pending_by_normalized_url() -> None:
    store = SqliteFrontierStore(":memory:")
    try:
        first = store.submit_crawl_request("https://example.com/a?x=1")
        assert first["status"] == "pending"
        assert isinstance(first["id"], int)
        second = store.submit_crawl_request("https://example.com/a?x=1")
        assert second["id"] == first["id"]
        assert store.list_crawl_requests(status="pending") != []
    finally:
        store.close()


def test_submit_blank_url_rejected() -> None:
    store = SqliteFrontierStore(":memory:")
    try:
        with pytest.raises(Exception):
            store.submit_crawl_request("   ")
    finally:
        store.close()


def test_list_and_decide_round_trip() -> None:
    store = SqliteFrontierStore(":memory:")
    try:
        one = store.submit_crawl_request("https://example.com/1")
        two = store.submit_crawl_request("https://example.com/2")
        assert [r["id"] for r in store.list_crawl_requests()] == [two["id"], one["id"]]
        assert store.set_crawl_request_status(one["id"], "approved") is True
        assert store.set_crawl_request_status(one["id"], "declined") is False
        assert store.set_crawl_request_status(999999, "approved") is False
        assert store.set_crawl_request_status(two["id"], "bogus") is False
        approved = store.list_crawl_requests(status="approved")
        assert [r["id"] for r in approved] == [one["id"]]
        assert approved[0]["decided_at"]
    finally:
        store.close()


def test_legacy_double_keeps_working() -> None:
    class _Legacy(FrontierStore):
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
            return 0

        def requeue(self, url: str) -> bool:
            return False

        def is_known(self, url: str) -> bool:
            return False

        def pending(self, limit: int = 500, offset: int = 0):  # type: ignore[no-untyped-def]
            return []

        def failed(self, limit: int = 100, offset: int = 0):  # type: ignore[no-untyped-def]
            return []

        def failed_domains(self, limit: int = 100, offset: int = 0):  # type: ignore[no-untyped-def]
            return []

        def failed_domain_count(self) -> int:
            return 0

        def requeue_failed_domains(self, domains) -> int:  # type: ignore[no-untyped-def]
            return 0

        def purge_failed_domains(self, domains) -> int:  # type: ignore[no-untyped-def]
            return 0

        def queued_domains(self, limit: int = 100, offset: int = 0):  # type: ignore[no-untyped-def]
            return []

        def queued_domain_count(self) -> int:
            return 0

        def bump_queued_priority(self, domains, delta: int = 1) -> int:  # type: ignore[no-untyped-def]
            return 0

        def counts(self):  # type: ignore[no-untyped-def]
            return {"total": 0}

        def save_checkpoint(self, checkpoint) -> None:  # type: ignore[no-untyped-def]
            return None

        def load_checkpoint(self, crawl_id: str = "default"):  # type: ignore[no-untyped-def]
            return None

        def get_settings(self, settings_id: str = "default"):  # type: ignore[no-untyped-def]
            return {}

        def save_settings(self, settings) -> None:  # type: ignore[no-untyped-def]
            return None

        def clear_queued(self) -> int:
            return 0

        def close(self) -> None:
            return None

    double = _Legacy()
    assert double.submit_crawl_request("https://example.com/x")["id"] is None
    assert double.list_crawl_requests() == []
    assert double.set_crawl_request_status(1, "approved") is False


def test_rate_limit_scope_mapping() -> None:
    assert scope_for_request("POST", "/api/v1/crawl-requests") == "crawl_request"
    assert limit_for("crawl_request") == (10, 3600.0)


def test_submit_and_duplicate_flow(client: TestClient, file_store: str) -> None:
    first = client.post(
        "/api/v1/crawl-requests", json={"url": "https://example.com/new-page"}
    )
    assert first.status_code == 201
    assert first.json()["status"] == "pending"
    request_id = first.json()["id"]

    repeat = client.post(
        "/api/v1/crawl-requests", json={"url": "https://example.com/new-page"}
    )
    assert repeat.status_code == 200
    assert repeat.json()["id"] == request_id
    assert repeat.json()["status"] == "pending"


def test_submit_rejects_non_http_url(client: TestClient, file_store: str) -> None:
    resp = client.post("/api/v1/crawl-requests", json={"url": "ftp://example.com/x"})
    assert resp.status_code == 422


def test_submit_known_url_returns_duplicate(
    client: TestClient, file_store: str
) -> None:
    probe = SqliteFrontierStore(file_store)
    try:
        assert probe.enqueue("https://example.com/known") is True
    finally:
        probe.close()
    resp = client.post(
        "/api/v1/crawl-requests", json={"url": "https://example.com/known"}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "duplicate"


def test_admin_review_and_approve_enqueues(client: TestClient, file_store: str) -> None:
    created = client.post(
        "/api/v1/crawl-requests", json={"url": "https://example.com/review-me"}
    )
    request_id = created.json()["id"]

    listed = client.get("/api/v1/frontier/crawl-requests?status=pending")
    assert listed.status_code == 200
    assert listed.json()["total"] == 1

    approved = client.post(f"/api/v1/frontier/crawl-requests/{request_id}/approve")
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"

    status = client.get("/api/v1/frontier/status")
    assert status.status_code == 200
    assert any(
        entry["url"] == "https://example.com/review-me"
        for entry in status.json()["queue"]
    )

    again = client.post(f"/api/v1/frontier/crawl-requests/{request_id}/approve")
    assert again.status_code == 404


def test_admin_decline_and_unknown_id(client: TestClient, file_store: str) -> None:
    created = client.post(
        "/api/v1/crawl-requests", json={"url": "https://example.com/nope"}
    )
    request_id = created.json()["id"]

    declined = client.post(f"/api/v1/frontier/crawl-requests/{request_id}/decline")
    assert declined.status_code == 200
    assert declined.json()["status"] == "declined"

    missing = client.post("/api/v1/frontier/crawl-requests/999999/decline")
    assert missing.status_code == 404

    bad_filter = client.get("/api/v1/frontier/crawl-requests?status=bogus")
    assert bad_filter.status_code == 422

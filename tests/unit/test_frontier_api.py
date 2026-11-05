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


def test_failed_lists_reasons_with_pagination(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The failed-URL view shows the short terminal reason per page."""
    from openlens.api import routes as routes_mod
    from openlens.storage.frontier import FrontierEntry

    entries = [
        FrontierEntry(
            url=f"https://example.com/{name}",
            status="failed",
            attempts=attempts,
            last_error=reason,
        )
        for name, attempts, reason in [
            ("a", 3, "fetch timeout"),
            ("b", 2, "denied by deny_domains"),
            ("c", 1, None),
        ]
    ]

    class _Store:
        def failed(self, limit: int = 100, offset: int = 0):
            return entries[offset : offset + limit]

        def counts(self):
            return {"failed": len(entries), "total": len(entries)}

    monkeypatch.setattr(routes_mod, "_frontier_read_store", lambda request: _Store())
    first = client.get("/api/v1/frontier/failed?limit=2")
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["total"] == 3
    assert [entry["url"] for entry in body["urls"]] == [
        "https://example.com/a",
        "https://example.com/b",
    ]
    assert body["urls"][0]["reason"] == "fetch timeout"
    assert body["urls"][0]["attempts"] == 3
    second = client.get("/api/v1/frontier/failed?limit=2&offset=2")
    assert second.status_code == 200, second.text
    assert [entry["url"] for entry in second.json()["urls"]] == [
        "https://example.com/c"
    ]
    assert second.json()["urls"][0]["reason"] == ""


def _failed_domain_store(tmp_path) -> SqliteFrontierStore:
    """Seeded store: two failed hosts, one invalid URL, one queued row."""
    from openlens.storage.frontier import SqliteFrontierStore as _Store

    store = _Store(str(tmp_path / "domains.db"))
    for url in (
        "https://example.com/a",
        "https://example.com/b",
        "https://other.example/c",
        "mailto:info@example.com",
        "https://example.com/queued",
    ):
        assert store.enqueue(url, 0) is True
    for _ in range(3):
        store.mark_failed("https://example.com/a", "fetch timeout")
    for _ in range(2):
        store.mark_failed("https://example.com/b", "http 404")
    store.mark_failed("https://other.example/c", "http 401")
    store.mark_failed("mailto:info@example.com", "invalid url")
    return store


def test_failed_domain_postgres_expr_has_no_literal_percent() -> None:
    """psycopg scans %X client-side: a literal %: in SQL is a fatal
    ProgrammingError, so the rollup expression must only carry %s."""
    import re

    from openlens.storage.frontier import FAILED_DOMAIN_HOST_EXPR_PG

    assert re.findall(r"%(?!s)", FAILED_DOMAIN_HOST_EXPR_PG) == []


def test_failed_domains_groups_hosts_with_counts(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The domains view rolls failed rows up by host, most-failed first."""
    from openlens.api import routes as routes_mod

    store = _failed_domain_store(tmp_path)
    try:
        monkeypatch.setattr(routes_mod, "_frontier_read_store", lambda request: store)
        body = client.get("/api/v1/frontier/failed/domains").json()
        assert body["total"] == 3
        assert [
            (d["domain"], d["failed"], d["top_reason"]) for d in body["domains"]
        ] == [
            ("example.com", 2, "fetch timeout"),
            ("(invalid)", 1, "invalid url"),
            ("other.example", 1, "http 401"),
        ]
        page = client.get("/api/v1/frontier/failed/domains?limit=1&offset=1").json()
        assert [(d["domain"], d["failed"]) for d in page["domains"]] == [
            ("(invalid)", 1)
        ]
    finally:
        store.close()


def test_domains_requeue_revives_with_reset_attempts(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Requeue resets the retry budget so rows actually refetch."""
    from openlens.api import routes as routes_mod

    store = _failed_domain_store(tmp_path)
    try:
        monkeypatch.setattr(routes_mod, "_frontier_read_store", lambda request: store)
        resp = client.post(
            "/api/v1/frontier/domains/requeue", json={"domains": ["example.com"]}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"domains": ["example.com"], "affected": 2}
        assert {e.url for e in store.failed(limit=100)} == {
            "https://other.example/c",
            "mailto:info@example.com",
        }
        pending = {e.url: e for e in store.pending(limit=100)}
        assert pending["https://example.com/a"].attempts == 0
    finally:
        store.close()


def test_domains_purge_deletes_only_failed_rows(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Purge removes failed history, never queued work."""
    from openlens.api import routes as routes_mod

    store = _failed_domain_store(tmp_path)
    try:
        monkeypatch.setattr(routes_mod, "_frontier_read_store", lambda request: store)
        resp = client.post(
            "/api/v1/frontier/domains/purge", json={"domains": ["example.com"]}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["affected"] == 2
        assert {e.url for e in store.pending(limit=100)} == {
            "https://example.com/queued"
        }
    finally:
        store.close()


def test_domains_skip_moves_queued_rows(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Skip marks queued rows without touching failed history."""
    from openlens.api import routes as routes_mod

    _file_store(monkeypatch, tmp_path)
    store = _failed_domain_store(tmp_path)
    try:
        monkeypatch.setattr(routes_mod, "_frontier_read_store", lambda request: store)
        resp = client.post(
            "/api/v1/frontier/domains/skip", json={"domains": ["example.com"]}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"domains": ["example.com"], "affected": 1}
        assert {e.url for e in store.failed(limit=100)} == {
            "https://example.com/a",
            "https://example.com/b",
            "https://other.example/c",
            "mailto:info@example.com",
        }
    finally:
        store.close()


def test_domains_actions_reject_bad_bodies(client: TestClient) -> None:
    """Domain actions are fail-closed on malformed host lists."""
    for path in (
        "/api/v1/frontier/domains/requeue",
        "/api/v1/frontier/domains/purge",
        "/api/v1/frontier/domains/skip",
        "/api/v1/frontier/domains/priority",
    ):
        assert client.post(path, json={}).status_code == 422
        assert client.post(path, json={"domains": []}).status_code == 422
        assert client.post(path, json={"domains": "example.com"}).status_code == 422
        assert client.post(path, json={"domains": [":::"]}).status_code == 422
    assert (
        client.post(
            "/api/v1/frontier/domains/priority",
            json={"domains": ["example.com"], "delta": 101},
        ).status_code
        == 422
    )


def _queued_domain_store(tmp_path) -> SqliteFrontierStore:
    """Seeded store: two queued hosts, one failed row, one skipped row."""
    from openlens.storage.frontier import SqliteFrontierStore as _Store

    store = _Store(str(tmp_path / "queued.db"))
    for url in (
        "https://example.com/a",
        "https://example.com/b",
        "https://other.example/c",
        "https://example.com/failed",
        "https://example.com/skipped",
    ):
        assert store.enqueue(url, 0) is True
    store.mark_failed("https://example.com/failed", "fetch timeout")
    assert store.mark_skipped("https://example.com/skipped") == 1
    return store


def test_queued_domains_groups_hosts_with_counts(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The queued view rolls up only queued rows, priority first."""
    from openlens.api import routes as routes_mod

    store = _queued_domain_store(tmp_path)
    assert store.bump_queued_priority(["other.example"], 3) == 1
    try:
        monkeypatch.setattr(routes_mod, "_frontier_read_store", lambda request: store)
        body = client.get("/api/v1/frontier/queued/domains").json()
        assert body["total"] == 2
        assert [
            (d["domain"], d["queued"], d["top_priority"]) for d in body["domains"]
        ] == [
            ("other.example", 1, 3),
            ("example.com", 2, 0),
        ]
        page = client.get("/api/v1/frontier/queued/domains?limit=1&offset=1").json()
        assert [(d["domain"], d["queued"]) for d in page["domains"]] == [
            ("example.com", 2)
        ]
    finally:
        store.close()


def test_failed_domain_urls_lists_only_that_host(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The domain drill-down lists that host's failed URLs with reasons."""
    from openlens.api import routes as routes_mod

    store = _failed_domain_store(tmp_path)
    try:
        monkeypatch.setattr(routes_mod, "_frontier_read_store", lambda request: store)
        body = client.get("/api/v1/frontier/failed/domains/example.com/urls").json()
        assert body["total"] == 2
        assert [(u["url"], u["reason"]) for u in body["urls"]] == [
            ("https://example.com/a", "fetch timeout"),
            ("https://example.com/b", "http 404"),
        ]
        other = client.get("/api/v1/frontier/failed/domains/other.example/urls").json()
        assert other["total"] == 1
        assert other["urls"][0]["url"] == "https://other.example/c"
        missing = client.get("/api/v1/frontier/failed/domains/missing.test/urls").json()
        assert missing == {"urls": [], "total": 0, "limit": 100, "offset": 0}
        malformed = client.get("/api/v1/frontier/failed/domains/:::/urls").json()
        assert malformed["urls"] == [] and malformed["total"] == 0
    finally:
        store.close()


def test_queued_domain_urls_lists_only_that_host(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The queued drill-down lists that host's queued URLs in take order."""
    from openlens.api import routes as routes_mod

    store = _queued_domain_store(tmp_path)
    assert store.bump_queued_priority(["example.com"], 4) == 2
    try:
        monkeypatch.setattr(routes_mod, "_frontier_read_store", lambda request: store)
        body = client.get("/api/v1/frontier/queued/domains/example.com/urls").json()
        assert body["total"] == 2
        assert [u["url"] for u in body["urls"]] == [
            "https://example.com/b",
            "https://example.com/a",
        ]
        other = client.get("/api/v1/frontier/queued/domains/other.example/urls").json()
        assert other["total"] == 1
        assert other["urls"][0]["url"] == "https://other.example/c"
        missing = client.get("/api/v1/frontier/queued/domains/missing.test/urls").json()
        assert missing == {"urls": [], "total": 0, "limit": 100, "offset": 0}
        malformed = client.get("/api/v1/frontier/queued/domains/:::/urls").json()
        assert malformed["urls"] == [] and malformed["total"] == 0
    finally:
        store.close()


def test_domains_priority_bumps_only_queued(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Priority deltas move queued rows; failed/skipped rows keep priority."""
    from openlens.api import routes as routes_mod

    store = _queued_domain_store(tmp_path)
    try:
        monkeypatch.setattr(routes_mod, "_frontier_read_store", lambda request: store)
        resp = client.post(
            "/api/v1/frontier/domains/priority",
            json={"domains": ["example.com"], "delta": 2},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"domains": ["example.com"], "affected": 2}
        prio = {e.url: e.priority for e in store.pending(limit=100)}
        assert prio["https://example.com/a"] == 2
        assert prio["https://example.com/b"] == 2
        assert prio["https://other.example/c"] == 0
        resp = client.post(
            "/api/v1/frontier/domains/priority",
            json={"domains": ["example.com"], "delta": -5},
        )
        assert resp.json()["affected"] == 2
        prio = {e.url: e.priority for e in store.pending(limit=100)}
        assert prio["https://example.com/a"] == -3
        resp = client.post(
            "/api/v1/frontier/domains/priority",
            json={"domains": ["missing.test"], "delta": 1},
        )
        assert resp.status_code == 200
        assert resp.json()["affected"] == 0
    finally:
        store.close()


def test_domains_skip_merges_settings_skip_list(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Skipping from the domains view also lands in settings.skip_domains."""
    from openlens.api import routes as routes_mod
    from openlens.api import frontier_state

    db_path = _file_store(monkeypatch, tmp_path)
    store = _failed_domain_store(tmp_path)
    try:
        monkeypatch.setattr(routes_mod, "_frontier_read_store", lambda request: store)
        resp = client.post(
            "/api/v1/frontier/domains/skip", json={"domains": ["example.com"]}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["affected"] == 1
        assert (
            "example.com"
            in frontier_state.get_frontier_state().settings["skip_domains"]
        )
        probe = SqliteFrontierStore(db_path)
        try:
            assert "example.com" in probe.get_settings().get("skip_domains", [])
        finally:
            probe.close()
    finally:
        store.close()


def _triage_store(tmp_path) -> str:
    """Seeded store with one failed host per triage reason class."""
    db_path = str(tmp_path / "triage.db")
    store = SqliteFrontierStore(db_path)
    try:
        seeds = [
            ("https://robots.example/a", "disallowed by robots.txt"),
            ("https://robots.example/still-queued", None),
            ("https://type.example/a", "content-type skipped"),
            ("https://type.example/still-queued", None),
            ("https://gone.example/a", "http 404"),
            ("https://gone.example/still-queued", None),
            ("https://denied.example/a", "http 401"),
            ("https://slow.example/a", "http 429"),
            ("https://slow.example/b", "http 429"),
            ("https://timeout.example/a", "fetch timeout"),
            ("https://timeout.example/b", "fetch timeout"),
            ("https://thin.example/a", "thin content"),
            ("https://thin.example/still-queued", None),
            ("https://weird.example/a", "some new reason"),
            ("mailto:info@invalid.example", "invalid url"),
        ]
        for url, reason in seeds:
            assert store.enqueue(url, 0) is True
            if reason is not None:
                store.mark_failed(url, reason)
    finally:
        store.close()
    return db_path


def _triage_post(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    payload: dict,
) -> dict:
    """Run the triage endpoint against a seeded store; return the body."""
    from openlens.api import routes as routes_mod
    from openlens.storage.app_config import SqliteAppConfigStore

    _file_store(monkeypatch, tmp_path)
    db_path = _triage_store(tmp_path)
    store = SqliteFrontierStore(db_path)
    # Offline config-doc store: the test app otherwise resolves a
    # (lazy, unreachable) Postgres config store for the block step.
    config_store = SqliteAppConfigStore(":memory:")
    monkeypatch.setattr(routes_mod, "_admin_config_store", lambda request: config_store)
    try:
        monkeypatch.setattr(routes_mod, "_frontier_read_store", lambda request: store)
        resp = client.post("/api/v1/frontier/failed/domains/auto-triage", json=payload)
        assert resp.status_code == 200, resp.text
        return {"body": resp.json(), "db_path": db_path}
    finally:
        store.close()


def _triage_by_domain(body: dict) -> dict[str, dict]:
    return {entry["domain"]: entry for entry in body["triaged"]}


def test_auto_triage_dispositions_by_reason(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Every reason class gets its documented action; unknowns are untouched."""
    from openlens.api import frontier_state

    result = _triage_post(client, monkeypatch, tmp_path, {})
    assert result["body"]["dry_run"] is False
    by_domain = _triage_by_domain(result["body"])

    assert by_domain["robots.example"]["action"] == "skip+purge"
    assert by_domain["type.example"]["action"] == "skip+purge"
    assert by_domain["gone.example"]["action"] == "skip+purge"
    assert by_domain["denied.example"]["action"] == "skip+purge"
    assert by_domain["slow.example"]["action"] == "requeue+wait"
    assert by_domain["slow.example"]["wait_s"] == pytest.approx(120.0)
    assert by_domain["timeout.example"]["action"] == "requeue+backoff"
    assert by_domain["timeout.example"]["wait_s"] == pytest.approx(60.0)
    assert by_domain["thin.example"]["action"] == "skip+block+purge"
    assert by_domain["weird.example"]["action"] == "unhandled"
    assert by_domain["(invalid)"]["action"] == "unhandled"

    statuses = _row_statuses(result["db_path"])
    # Permanent failures: queued rows skipped, failed history purged.
    assert statuses["https://robots.example/still-queued"] == "skipped"
    assert statuses["https://type.example/still-queued"] == "skipped"
    assert statuses["https://gone.example/still-queued"] == "skipped"
    assert "https://robots.example/a" not in statuses
    assert "https://gone.example/a" not in statuses
    # Transient failures: revived to queued.
    assert statuses["https://slow.example/a"] == "queued"
    assert statuses["https://timeout.example/a"] == "queued"
    assert statuses["https://thin.example/still-queued"] == "skipped"
    assert "https://thin.example/a" not in statuses
    # Unknowns: never mutated.
    assert statuses["https://weird.example/a"] == "failed"
    assert statuses["mailto:info@invalid.example"] == "failed"

    settings = frontier_state.get_frontier_state().settings
    for domain in ("robots.example", "type.example", "gone.example", "thin.example"):
        assert domain in settings["skip_domains"]
    indexing = client.get("/api/v1/admin/config/indexing").json()
    assert "thin.example" in indexing["body"].get("deny_domains", [])
    assert "robots.example" not in indexing["body"].get("deny_domains", [])

    probe = SqliteFrontierStore(result["db_path"])
    try:
        assert probe.get_politeness_delay("slow.example") > 0
        pending = {entry.url: entry for entry in probe.pending(limit=100)}
        assert pending["https://timeout.example/a"].priority == -1
        assert pending["https://slow.example/a"].priority == 0
    finally:
        probe.close()


def test_auto_triage_dry_run_mutates_nothing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Dry run classifies (with planned waits) but changes no rows or policy."""
    from openlens.api import frontier_state

    result = _triage_post(client, monkeypatch, tmp_path, {"dry_run": True})
    assert result["body"]["dry_run"] is True
    by_domain = _triage_by_domain(result["body"])
    assert by_domain["slow.example"]["action"] == "requeue+wait"
    assert by_domain["slow.example"]["wait_s"] == pytest.approx(120.0)
    assert by_domain["timeout.example"]["action"] == "requeue+backoff"
    assert by_domain["gone.example"]["action"] == "skip+purge"
    assert by_domain["thin.example"]["action"] == "skip+block+purge"
    assert all(entry["affected"] == 0 for entry in result["body"]["triaged"])

    statuses = _row_statuses(result["db_path"])
    assert statuses["https://slow.example/a"] == "failed"
    assert statuses["https://gone.example/still-queued"] == "queued"
    assert "thin.example" not in (
        frontier_state.get_frontier_state().settings.get("skip_domains", [])
    )
    indexing = client.get("/api/v1/admin/config/indexing").json()
    assert "thin.example" not in indexing["body"].get("deny_domains", [])


def test_auto_triage_persists_rate_limit_for_transient_hosts(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """429/timeout triage persists a per-host rate_limit_s for future crawls."""
    dry = _triage_post(client, monkeypatch, tmp_path, {"dry_run": True})
    assert dry["body"]["dry_run"] is True
    (tmp_path / "triage.db").unlink(missing_ok=True)
    dry_policies = (
        client.get("/api/v1/admin/config/indexing")
        .json()
        .get("body", {})
        .get("domain_policies", {})
    )
    assert "slow.example" not in dry_policies
    assert "timeout.example" not in dry_policies

    result = _triage_post(client, monkeypatch, tmp_path, {})
    by_domain = _triage_by_domain(result["body"])
    assert by_domain["slow.example"]["action"] == "requeue+wait"
    assert by_domain["timeout.example"]["action"] == "requeue+backoff"
    policies = (
        client.get("/api/v1/admin/config/indexing")
        .json()
        .get("body", {})
        .get("domain_policies", {})
    )
    assert policies["slow.example"]["rate_limit_s"] == pytest.approx(10.0)
    assert policies["timeout.example"]["rate_limit_s"] == pytest.approx(10.0)
    assert "gone.example" not in policies
    assert "weird.example" not in policies


def test_store_overlay_preserves_kafka_enabled() -> None:
    """The store overlay rebuilds the payload; it must not drop the flag.

    On a server with a live store the overlay always rebuilds, so a
    dropped flag reads as "disabled" no matter the real config.
    """
    from openlens.api.routes import _frontier_status_payload, _overlay_store_status

    class _StubStore:
        def load_checkpoint(self):
            return None

        def pending(self, limit: int = 500):
            return []

        def counts(self):
            return {}

    payload = _frontier_status_payload(
        {
            "kafka": {
                "enabled": True,
                "bootstrap_servers": ["kafka-1:19092"],
                "topic": "crawl.urls",
                "group_id": "crawlers",
            }
        }
    )
    assert _overlay_store_status(payload, _StubStore()).kafka_enabled is True


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


def test_enqueue_seeds_accepts_without_touching_phase(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    db_path = _file_store(monkeypatch, tmp_path)
    paused = client.post("/api/v1/frontier/pause")
    assert paused.status_code == 200
    assert paused.json()["paused"] is True
    resp = client.post(
        "/api/v1/frontier/seeds",
        json={"seeds": ["https://example.com/a", "  "], "priority": 3},
    )
    assert resp.status_code == 200
    assert resp.json() == {"enqueued": 1, "skipped_over_cap": 0}
    # The count is real: the row lands queued with the given priority.
    probe = SqliteFrontierStore(db_path)
    try:
        rows = {
            row["url"]: (row["status"], row["priority"])
            for row in probe._conn.execute(
                "SELECT url, status, priority FROM frontier_queue"
            ).fetchall()
        }
    finally:
        probe.close()
    assert rows == {"https://example.com/a": ("queued", 3)}
    # Still paused: seeds never resume, never replace the queue.
    status = client.get("/api/v1/frontier/status").json()
    assert status["paused"] is True
    assert status["checkpoint"]["phase"] == "paused"


def test_enqueue_seeds_repeat_paste_counts_known_url(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _file_store(monkeypatch, tmp_path)
    first = client.post(
        "/api/v1/frontier/seeds", json={"seeds": ["https://example.com/a"]}
    )
    assert first.status_code == 200
    assert first.json() == {"enqueued": 1, "skipped_over_cap": 0}
    # A re-paste of an already-queued seed stays a 200: the queue is
    # healthy, there is just nothing new to do.
    second = client.post(
        "/api/v1/frontier/seeds", json={"seeds": ["https://example.com/a"]}
    )
    assert second.status_code == 200
    assert second.json() == {"enqueued": 1, "skipped_over_cap": 0}


def test_enqueue_seeds_honors_max_queued_urls(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Pastes larger than the dashboard cap queue only up to the cap.

    Regression: ``POST /frontier/seeds`` ignored ``max_queued_urls`` and
    queued every pasted seed, so the setting only ever bit later as
    crawl-time eviction.
    """
    _file_store(monkeypatch, tmp_path)
    settings = client.get("/api/v1/frontier/settings").json()["settings"]
    settings["max_queued_urls"] = 2
    put = client.put("/api/v1/frontier/settings", json=settings)
    assert put.status_code == 200
    first = client.post(
        "/api/v1/frontier/seeds",
        json={
            "seeds": [
                "https://example.com/a",
                "https://example.com/b",
                "https://example.com/c",
            ]
        },
    )
    assert first.status_code == 200
    assert first.json() == {"enqueued": 2, "skipped_over_cap": 1}
    # At the cap, a fresh seed is refused (200, nothing new) — not queued.
    second = client.post(
        "/api/v1/frontier/seeds", json={"seeds": ["https://example.com/d"]}
    )
    assert second.status_code == 200
    assert second.json() == {"enqueued": 0, "skipped_over_cap": 1}
    # Re-pasting a known URL stays idempotent without growing the queue.
    third = client.post(
        "/api/v1/frontier/seeds", json={"seeds": ["https://example.com/a"]}
    )
    assert third.status_code == 200
    assert third.json() == {"enqueued": 1, "skipped_over_cap": 0}


def test_enqueue_seeds_503_when_nothing_accepted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openlens.api import routes as routes_mod

    monkeypatch.setattr(routes_mod, "_publish_seeds", lambda *args, **kwargs: 0)
    monkeypatch.setattr(routes_mod, "_frontier_read_store", lambda request: None)
    resp = client.post(
        "/api/v1/frontier/seeds", json={"seeds": ["https://example.com/a"]}
    )
    assert resp.status_code == 503
    body = resp.json()
    assert set(body.keys()) == {"error"}
    assert body["error"]["code"] == "provider_unavailable"


def _doc_store(monkeypatch: pytest.MonkeyPatch, tmp_path) -> str:
    """File-backed document store wired into the endpoint's opener."""
    from openlens.providers import custom_index as custom_index_mod
    from openlens.storage.repository import DocumentRepository

    db_path = str(tmp_path / "docs.db")
    monkeypatch.setattr(
        custom_index_mod, "open_store", lambda config: DocumentRepository(db_path)
    )
    return db_path


def test_url_info_reports_full_story(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from openlens.core.interfaces import Document
    from openlens.storage.repository import DocumentRepository

    frontier_db = _file_store(monkeypatch, tmp_path)
    frontier = SqliteFrontierStore(frontier_db)
    try:
        assert frontier.enqueue("https://example.com/a", 0, priority=9) is True
    finally:
        frontier.close()
    doc_db = _doc_store(monkeypatch, tmp_path)
    docs = DocumentRepository(doc_db)
    try:
        docs.add_documents(
            [
                Document(
                    id="doc-a",
                    title="Example A",
                    content="hello world",
                    url="https://example.com/a",
                    metadata={
                        "outlinks": ["https://example.com/b", "https://example.com/c"]
                    },
                )
            ]
        )
    finally:
        docs.close()
    resp = client.get(
        "/api/v1/frontier/url-info", params={"url": "https://example.com/a"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["url"] == "https://example.com/a"
    assert body["normalized_url"] == "https://example.com/a"
    assert body["frontier"] == {
        "status": "queued",
        "priority": 9,
        "attempts": 0,
        "last_error": None,
    }
    assert body["document"] == {"title": "Example A", "chars": 11}
    assert body["extracted_links"] == 2
    assert body["link_sample"] == ["https://example.com/b", "https://example.com/c"]


def test_url_info_unknown_url_resolves_to_nulls(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _file_store(monkeypatch, tmp_path)
    _doc_store(monkeypatch, tmp_path)
    resp = client.get(
        "/api/v1/frontier/url-info", params={"url": "https://example.com/never-seen"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["frontier"] is None
    assert body["document"] is None
    assert body["extracted_links"] == 0
    assert body["link_sample"] == []


def test_url_info_rejects_blank(client: TestClient) -> None:
    resp = client.get("/api/v1/frontier/url-info", params={"url": "   "})
    assert resp.status_code == 422


def test_enqueue_seeds_validates_priority(client: TestClient) -> None:
    resp = client.post("/api/v1/frontier/seeds", json={"seeds": [], "priority": "high"})
    assert resp.status_code == 422
    empty = client.post("/api/v1/frontier/seeds", json={"seeds": []})
    assert empty.status_code == 200
    assert empty.json() == {"enqueued": 0, "skipped_over_cap": 0}


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


def test_enqueue_seeds_rejects_bad_depth(client: TestClient) -> None:
    # Note: `True` is not here — pydantic coerces bool to int before
    # field validators run, so depth=true means 1 (same as priority).
    for bad in (-1, "deep", 2.5):
        resp = client.post(
            "/api/v1/frontier/seeds",
            json={"seeds": ["https://example.com/a"], "depth": bad},
        )
        assert resp.status_code == 422, bad


def test_enqueue_seeds_stores_depth(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    db_path = _file_store(monkeypatch, tmp_path)
    resp = client.post(
        "/api/v1/frontier/seeds",
        json={"seeds": ["https://example.com/a"], "depth": 2},
    )
    assert resp.status_code == 200
    assert resp.json() == {"enqueued": 1, "skipped_over_cap": 0}
    probe = SqliteFrontierStore(db_path)
    try:
        rows = {
            row["url"]: (row["status"], row["depth"])
            for row in probe._conn.execute(
                "SELECT url, status, depth FROM frontier_queue"
            ).fetchall()
        }
    finally:
        probe.close()
    assert rows == {"https://example.com/a": ("queued", 2)}


def test_publish_seeds_threads_depth_to_kafka(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Kafka-first path must carry depth, not hardcode 0."""
    from openlens.api import routes as routes_mod

    seen: list[tuple] = []

    class _Queue:
        def publish_many(self, items):  # type: ignore[no-untyped-def]
            seen.extend(items)
            return len(items)

        def close(self) -> None:
            pass

    monkeypatch.setattr(routes_mod, "kafka_enabled", lambda config: True)
    monkeypatch.setattr(routes_mod, "open_kafka_queue", lambda config: _Queue())
    monkeypatch.setattr(
        routes_mod, "_enqueue_seeds_to_store", lambda *args, **kwargs: 0
    )
    assert (
        routes_mod._publish_seeds({}, ["https://example.com/a"], priority=1, depth=2)
        == 1
    )
    assert seen == [("https://example.com/a", 2, 1)]

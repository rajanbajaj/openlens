"""Rate-limiter regression tests: unit window behavior + HTTP/WS 429s."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from openlens.api import frontier_state
from openlens.api.rate_limit import (
    RateLimiter,
    limit_for,
    rate_limit_enabled,
    scope_for_request,
)
from openlens.main import create_app

from tests.unit.test_api import FakeEngine


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """App with the default (generous) limits; per-test env overrides."""
    for var in (
        "OPENLENS_RATE_LIMIT_ENABLED",
        "OPENLENS_RATE_LIMIT_SEARCH",
        "OPENLENS_RATE_LIMIT_SUGGEST",
        "OPENLENS_RATE_LIMIT_FEEDBACK",
        "OPENLENS_RATE_LIMIT_ADMIN",
        "OPENLENS_RATE_LIMIT_WS",
        "OPENLENS_ALLOW_OPEN_ADMIN",
        "OPENLENS_FRONTIER_ADMIN_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
    frontier_state.reset_frontier_state()
    return TestClient(create_app(engine=FakeEngine()))


def test_scope_mapping() -> None:
    assert scope_for_request("POST", "/api/v1/search") == "search"
    assert scope_for_request("GET", "/api/v1/suggest") == "suggest"
    assert scope_for_request("POST", "/api/v1/feedback") == "feedback"
    assert scope_for_request("POST", "/api/v1/frontier/pause") == "admin"
    assert scope_for_request("PUT", "/api/v1/admin/config/query") == "admin"
    assert scope_for_request("GET", "/api/v1/frontier/status") is None
    assert scope_for_request("GET", "/api/v1/search") is None


def test_fixed_window_allows_then_denies_then_recovers() -> None:
    clock = _Clock()
    limiter = RateLimiter(clock=clock)
    assert limiter.check("search", "ip") is None
    assert limiter.check("search", "ip") is None
    # Exhaust a tiny window via env override semantics: use feedback (20/60).
    for _ in range(20):
        assert limiter.check("feedback", "ip") is None
    retry_after = limiter.check("feedback", "ip")
    assert retry_after is not None and retry_after > 0
    clock.now += 61.0
    assert limiter.check("feedback", "ip") is None


def test_limit_parsing_and_master_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENLENS_RATE_LIMIT_SEARCH", "5/30")
    assert limit_for("search") == (5, 30.0)
    monkeypatch.setenv("OPENLENS_RATE_LIMIT_SEARCH", "garbage")
    assert limit_for("search") == (120, 60.0)
    monkeypatch.setenv("OPENLENS_RATE_LIMIT_ENABLED", "false")
    assert rate_limit_enabled() is False
    assert RateLimiter().check("search", "ip") is None


def test_search_limited_per_ip_with_retry_after(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENLENS_RATE_LIMIT_SEARCH", "2/60")
    assert client.post("/api/v1/search", json={"query": "a"}).status_code == 200
    assert client.post("/api/v1/search", json={"query": "b"}).status_code == 200
    limited = client.post("/api/v1/search", json={"query": "c"})
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "rate_limited"
    assert "Retry-After" in limited.headers


def test_scopes_are_independent(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENLENS_RATE_LIMIT_SEARCH", "1/60")
    assert client.post("/api/v1/search", json={"query": "a"}).status_code == 200
    assert client.post("/api/v1/search", json={"query": "b"}).status_code == 429
    # Suggest draws from its own bucket.
    assert client.get("/api/v1/suggest", params={"q": "a"}).status_code == 200


def test_disabled_switch_turns_off_limiting(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENLENS_RATE_LIMIT_ENABLED", "false")
    monkeypatch.setenv("OPENLENS_RATE_LIMIT_SEARCH", "1/60")
    for _ in range(3):
        assert client.post("/api/v1/search", json={"query": "a"}).status_code == 200


def test_admin_mutations_limited(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENLENS_ALLOW_OPEN_ADMIN", "true")
    monkeypatch.setenv("OPENLENS_RATE_LIMIT_ADMIN", "1/60")
    assert client.post("/api/v1/frontier/pause").status_code == 200
    assert client.post("/api/v1/frontier/resume", json={}).status_code == 429


def test_live_socket_handshakes_limited(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENLENS_ALLOW_OPEN_ADMIN", "true")
    monkeypatch.setenv("OPENLENS_RATE_LIMIT_WS", "1/60")
    with client.websocket_connect("/api/v1/frontier/live"):
        pass
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/v1/frontier/live"):
            pass  # pragma: no cover - handshake must not complete

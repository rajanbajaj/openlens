"""Fail-closed admin gate regression tests (offline, in-memory).

Covers the security contract: with no token configured and no local-dev
opt-in, every admin mutation (HTTP + WebSocket) is denied; the opt-in
re-opens local dev; a configured token gates via `X-Admin-Token`
(header for HTTP, header or subprotocol for the live socket).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from openlens.api import frontier_state
from openlens.api.routes import (
    ALLOW_OPEN_ADMIN_ENV,
    FRONTIER_ADMIN_HEADER,
    FRONTIER_ADMIN_TOKEN_ENV,
)
from openlens.main import create_app

from tests.unit.test_api import FakeEngine

_TOKEN = "s3cret-gate-token"  # noqa: S105 (fake test credential, not a secret)


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """App with neither token nor opt-in: the locked-down default."""
    monkeypatch.delenv(FRONTIER_ADMIN_TOKEN_ENV, raising=False)
    monkeypatch.delenv(ALLOW_OPEN_ADMIN_ENV, raising=False)
    frontier_state.reset_frontier_state()
    return TestClient(create_app(engine=FakeEngine()))


def _mutation_calls(client: TestClient) -> list[tuple[str, str, dict]]:
    return [
        ("POST", "/api/v1/frontier/pause", {}),
        ("POST", "/api/v1/frontier/resume", {"seeds": ["https://example.com/a"]}),
        ("POST", "/api/v1/frontier/clear", {}),
        ("PUT", "/api/v1/frontier/settings", {"max_pages": 5}),
        ("PUT", "/api/v1/admin/config/query", {"body": {"providers": {}}}),
        ("heartbeat", "/api/v1/frontier/workers/heartbeat", {"worker_id": "w-1"}),
    ]


def _call(
    client: TestClient,
    method: str,
    path: str,
    payload: dict,
    headers: dict | None = None,
):
    if method == "POST":
        return client.post(path, json=payload, headers=headers or {})
    if method == "PUT":
        return client.put(path, json=payload, headers=headers or {})
    assert method == "heartbeat"
    return client.post(path, json=payload, headers=headers or {})


def test_mutations_denied_by_default(client: TestClient) -> None:
    for method, path, payload in _mutation_calls(client):
        resp = _call(client, method, path, payload)
        assert resp.status_code == 403, (method, path)
        assert resp.json()["error"]["code"] == "forbidden"


def test_reads_stay_open_by_default(client: TestClient) -> None:
    assert client.get("/api/v1/frontier/status").status_code == 200
    assert client.get("/api/v1/frontier/settings").status_code == 200
    assert client.get("/api/v1/frontier/workers").status_code == 200


def test_live_socket_denied_by_default(client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/api/v1/frontier/live"):
            pass  # pragma: no cover - accept must not happen
    assert exc_info.value.code == 1008


def test_opt_in_reopens_local_dev(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_OPEN_ADMIN_ENV, "true")
    assert client.post("/api/v1/frontier/pause").status_code == 200
    with client.websocket_connect("/api/v1/frontier/live"):
        pass


def test_token_gate_requires_matching_header(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(FRONTIER_ADMIN_TOKEN_ENV, _TOKEN)
    assert client.post("/api/v1/frontier/pause").status_code == 403
    wrong = client.post(
        "/api/v1/frontier/pause", headers={FRONTIER_ADMIN_HEADER: "nope"}
    )
    assert wrong.status_code == 403
    allowed = client.post(
        "/api/v1/frontier/pause", headers={FRONTIER_ADMIN_HEADER: _TOKEN}
    )
    assert allowed.status_code == 200


def test_live_socket_accepts_matching_subprotocol(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(FRONTIER_ADMIN_TOKEN_ENV, _TOKEN)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/v1/frontier/live"):
            pass  # pragma: no cover - no token offered
    with client.websocket_connect(
        "/api/v1/frontier/live", subprotocols=["x-admin-token", _TOKEN]
    ):
        pass

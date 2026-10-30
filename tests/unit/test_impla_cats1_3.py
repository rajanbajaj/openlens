"""IMPL-A (cats 1-3) tests: feedback reporting taxonomy + frontier admin gate.

Kids/privacy/fairness reporting rides on POST /api/v1/feedback with a
closed action taxonomy (`report_incorrect`, `report_suspicious`); the URL
frontier mutations are admin-only (fail-closed without a token unless the
local-dev `OPENLENS_ALLOW_OPEN_ADMIN` opt-in is set). Offline: fake
engine, in-memory frontier, no keys.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from openlens.api import frontier_state
from openlens.api.routes import FRONTIER_ADMIN_HEADER, FRONTIER_ADMIN_TOKEN_ENV
from openlens.main import create_app

from tests.unit.test_api import FakeEngine


@pytest.fixture()
def client() -> TestClient:
    frontier_state.reset_frontier_state()
    return TestClient(create_app(engine=FakeEngine()))


def _valid_settings() -> dict[str, object]:
    return {
        "max_pages": 10,
        "max_queued_urls": 50,
        "crawl_delay_s": 1.5,
        "default_max_depth": 2,
        "respect_robots": False,
    }


# --- feedback reporting taxonomy --------------------------------------------


def test_feedback_report_actions_count(client: TestClient) -> None:
    for action in ("report_incorrect", "report_suspicious"):
        resp = client.post(
            "/api/v1/feedback", json={"doc_id": "doc-r", "action": action}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"doc_id": "doc-r", "action": action, "count": 1}


def test_feedback_action_aliases_normalize(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/feedback", json={"doc_id": "doc-n", "action": "Report-Incorrect"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["action"] == "report_incorrect"
    other = client.post(
        "/api/v1/feedback", json={"doc_id": "doc-n", "action": "not helpful"}
    )
    assert other.status_code == 200, other.text
    assert other.json()["action"] == "not_helpful"


def test_feedback_rejects_unknown_action(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/feedback", json={"doc_id": "doc-x", "action": "doxx them please"}
    )
    assert resp.status_code == 422, resp.text
    assert set(resp.json().keys()) == {"error"}


# --- frontier admin gate -----------------------------------------------------


def test_frontier_mutations_denied_without_token_or_opt_in(
    client: TestClient,
) -> None:
    """Fail-closed: unconfigured gate denies mutations (no open default)."""
    assert client.post("/api/v1/frontier/pause").status_code == 403
    assert client.post("/api/v1/frontier/resume", json={}).status_code == 403
    assert (
        client.put("/api/v1/frontier/settings", json=_valid_settings()).status_code
        == 403
    )


def test_frontier_mutations_require_token_when_configured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(FRONTIER_ADMIN_TOKEN_ENV, "s3cret")
    for denied in (
        client.post("/api/v1/frontier/pause"),
        client.post("/api/v1/frontier/resume", json={}),
        client.put("/api/v1/frontier/settings", json=_valid_settings()),
    ):
        assert denied.status_code == 403, denied.text
        assert denied.json() == {
            "error": {
                "code": "forbidden",
                "message": "frontier mutation requires admin rights",
            }
        }
    # Reads stay open.
    assert client.get("/api/v1/frontier/status").status_code == 200
    assert client.get("/api/v1/frontier/settings").status_code == 200


def test_frontier_mutations_allow_token_holder(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(FRONTIER_ADMIN_TOKEN_ENV, "s3cret")
    headers = {FRONTIER_ADMIN_HEADER: "s3cret"}
    assert client.post("/api/v1/frontier/pause", headers=headers).status_code == 200
    resumed = client.post("/api/v1/frontier/resume", json={}, headers=headers)
    assert resumed.status_code == 200
    saved = client.put(
        "/api/v1/frontier/settings", json=_valid_settings(), headers=headers
    )
    assert saved.status_code == 200
    assert saved.json()["settings"]["max_pages"] == 10


def test_frontier_wrong_token_rejected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(FRONTIER_ADMIN_TOKEN_ENV, "s3cret")
    resp = client.post(
        "/api/v1/frontier/pause", headers={FRONTIER_ADMIN_HEADER: "wrong"}
    )
    assert resp.status_code == 403, resp.text

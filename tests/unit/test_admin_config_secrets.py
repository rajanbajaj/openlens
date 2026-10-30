"""Tests for secret redaction on the admin config API.

The models doc carries live credentials (``api_key``). GET endpoints are
open by design, and the admin UI renders the doc body verbatim, so secret
values must never leave the backend in plaintext:

- GET returns a ``REDACTED_SECRET`` sentinel wherever a secret-named field
  holds a value (both ``db`` and ``file`` sources).
- PUT accepts the sentinel back to mean "keep the existing value", so other
  fields stay editable without retyping keys.
- PUT with a sentinel where no value exists yet fails closed (422) instead
  of storing the sentinel as a credential.
- PUT responses are redacted too; the running app keeps the real values.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from openlens.storage.app_config import (
    REDACTED_SECRET,
    SqliteAppConfigStore,
    redact_secrets,
    restore_secrets,
)
from openlens.main import create_app

from tests.unit.test_api import FakeEngine


def _client() -> TestClient:
    app = create_app(engine=FakeEngine())
    app.state.app_config_store = SqliteAppConfigStore(":memory:")
    return TestClient(app)


def _models_body(key: str) -> dict[str, object]:
    return {
        "primary": "fake",
        "fake": {"enabled": True, "type": "fake"},
        "comp": {
            "enabled": False,
            "type": "openai_compat",
            "api_key": key,
            "model": "x",
        },
    }


def test_redact_secrets_unit() -> None:
    body = {
        "primary": "fake",
        "comp": {"api_key": "sk-live-123", "model": "x", "nested": {"password": "pw"}},
        "items": [{"token": "t"}],
        "timeout_s": 5,
    }
    redacted = redact_secrets(body)
    assert redacted["comp"]["api_key"] == REDACTED_SECRET
    assert redacted["comp"]["nested"]["password"] == REDACTED_SECRET
    assert redacted["items"][0]["token"] == REDACTED_SECRET
    assert redacted["comp"]["model"] == "x"
    assert redacted["timeout_s"] == 5
    # Input untouched.
    assert body["comp"]["api_key"] == "sk-live-123"


def test_restore_secrets_unit() -> None:
    submitted = {"comp": {"api_key": REDACTED_SECRET, "model": "y"}}
    existing = {"comp": {"api_key": "sk-live-123", "model": "x"}}
    assert restore_secrets(submitted, existing) == {
        "comp": {"api_key": "sk-live-123", "model": "y"}
    }


def test_get_redacts_db_secret() -> None:
    client = _client()
    put = client.put(
        "/api/v1/admin/config/models", json={"body": _models_body("sk-live-123")}
    )
    assert put.status_code == 200, put.text
    # PUT response itself must not echo the key.
    assert "sk-live-123" not in put.text
    assert put.json()["body"]["comp"]["api_key"] == REDACTED_SECRET

    got = client.get("/api/v1/admin/config/models")
    assert got.status_code == 200
    payload = got.json()
    assert payload["source"] == "db"
    assert payload["body"]["comp"]["api_key"] == REDACTED_SECRET
    assert "sk-live-123" not in got.text


def test_get_redacts_file_secret() -> None:
    client = _client()
    client.app.state.config["models"] = _models_body("sk-file-456")
    got = client.get("/api/v1/admin/config/models")
    assert got.status_code == 200
    assert got.json()["source"] == "file"
    assert got.json()["body"]["comp"]["api_key"] == REDACTED_SECRET
    assert "sk-file-456" not in got.text


def test_put_sentinel_preserves_secret() -> None:
    client = _client()
    first = client.put(
        "/api/v1/admin/config/models", json={"body": _models_body("sk-live-123")}
    )
    assert first.status_code == 200

    edited = _models_body(REDACTED_SECRET)
    edited["comp"]["model"] = "y"
    second = client.put("/api/v1/admin/config/models", json={"body": edited})
    assert second.status_code == 200, second.text
    assert second.json()["body"]["comp"]["api_key"] == REDACTED_SECRET

    # Running app kept the real key and picked up the edited field.
    live = client.app.state.config["models"]
    assert live["comp"]["api_key"] == "sk-live-123"
    assert live["comp"]["model"] == "y"

    # A later GET still redacts.
    assert "sk-live-123" not in client.get("/api/v1/admin/config/models").text


def test_put_new_secret_value_replaces() -> None:
    client = _client()
    assert (
        client.put(
            "/api/v1/admin/config/models", json={"body": _models_body("sk-old")}
        ).status_code
        == 200
    )
    resp = client.put(
        "/api/v1/admin/config/models", json={"body": _models_body("sk-new")}
    )
    assert resp.status_code == 200, resp.text
    assert client.app.state.config["models"]["comp"]["api_key"] == "sk-new"
    assert "sk-new" not in resp.text


def test_put_sentinel_without_existing_value_fails_closed() -> None:
    client = _client()
    body = _models_body(REDACTED_SECRET)
    body["comp"]["enabled"] = True
    resp = client.put("/api/v1/admin/config/models", json={"body": body})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_request"
    # Nothing persisted: still the file seed.
    assert client.get("/api/v1/admin/config/models").json()["source"] == "file"

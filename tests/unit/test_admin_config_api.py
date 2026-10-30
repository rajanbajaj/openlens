"""Tests for the DB-persisted admin config API (offline, in-memory store).

Covers GET/PUT /api/v1/admin/config/{doc} for the five config docs
(models/providers/ranking/query/plugins): file-fallback reads, strict-body
and validator 422s, the reused X-Admin-Token gate (403 on mutations, open
reads), DB-wins-over-file precedence, version audit, and offline-safe boot
(Postgres default store constructs lazily; unreachable DB degrades to file
config instead of failing).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from openlens.api.routes import FRONTIER_ADMIN_HEADER, FRONTIER_ADMIN_TOKEN_ENV
from openlens.core.interfaces import SearchRequest, SearchResult
from openlens.main import create_app, load_app_config, load_effective_config
from openlens.providers import registry
from openlens.providers.base import BaseSearchProvider
from openlens.storage.app_config import (
    CONFIG_DOC_KEYS,
    PostgresAppConfigStore,
    SqliteAppConfigStore,
    apply_db_config_overlay,
    open_app_config_store,
    postgres_app_config_ddl,
)

from tests.unit.test_api import FakeEngine


class _ToggleFakeProvider(BaseSearchProvider):
    """Thread-safe in-memory provider for manager-level validation."""

    @property
    def name(self) -> str:
        return "toggle_fake"

    def search(self, request: SearchRequest) -> list[SearchResult]:
        return [
            SearchResult(
                id="t1",
                title="toggle hit",
                snippet="s",
                original_score=1.0,
                final_score=1.0,
                provider="toggle_fake",
                reasons=["toggle"],
            )
        ]

    def health_check(self) -> bool:
        return True


@pytest.fixture()
def _toggle_provider() -> object:
    registry.register_builtin_providers()
    registry.register("toggle_fake", _ToggleFakeProvider)
    yield None
    registry.unregister("toggle_fake")


@pytest.fixture()
def client() -> TestClient:
    app = create_app(engine=FakeEngine())
    app.state.app_config_store = SqliteAppConfigStore(":memory:")
    return TestClient(app)


def _valid_bodies() -> dict[str, object]:
    return {
        "models": {
            "primary": "fake",
            "fake": {"enabled": True, "type": "fake"},
        },
        "providers": {
            "strategy": "single",
            "primary": "toggle_fake",
            "toggle_fake": {"enabled": True, "type": "toggle_fake"},
        },
        "ranking": {
            "default_profile": "custom",
            "profiles": {"custom": {"relevance_weight": 2.0}},
        },
        "query": {"expansion": {"synonym_sets": {"apparel": ["dress"]}}},
        "plugins": [{"name": "attribute_enricher"}],
        "indexing": {"max_pages": 10, "default_max_depth": 2},
    }


# -- reads ---------------------------------------------------------------


def test_get_unknown_doc_404_envelope(client: TestClient) -> None:
    resp = client.get("/api/v1/admin/config/nope")
    assert resp.status_code == 404
    assert resp.json() == {
        "error": {
            "code": "not_found",
            "message": "unknown config doc 'nope'. "
            f"Known: {', '.join(CONFIG_DOC_KEYS)}.",
        }
    }


def test_put_unknown_doc_404_envelope(client: TestClient) -> None:
    resp = client.put("/api/v1/admin/config/nope", json={"body": {}})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


def test_get_file_fallback_before_any_put(client: TestClient) -> None:
    for doc in CONFIG_DOC_KEYS:
        resp = client.get(f"/api/v1/admin/config/{doc}")
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload["doc"] == doc
        assert payload["source"] == "file"
        assert payload["version"] == 0
        assert payload["updated_at"] is None
        assert isinstance(payload["body"], (dict, list))


# -- round trips + versions ------------------------------------------------


def test_put_get_round_trip_all_docs(
    client: TestClient, _toggle_provider: object
) -> None:
    bodies = _valid_bodies()
    for doc in CONFIG_DOC_KEYS:
        put = client.put(f"/api/v1/admin/config/{doc}", json={"body": bodies[doc]})
        assert put.status_code == 200, put.text
        first = put.json()
        assert first["doc"] == doc
        assert first["source"] == "db"
        assert first["version"] == 1
        assert first["updated_at"]
        assert first["body"] == bodies[doc]

        got = client.get(f"/api/v1/admin/config/{doc}")
        assert got.status_code == 200
        assert got.json()["body"] == bodies[doc]
        assert got.json()["source"] == "db"

        again = client.put(f"/api/v1/admin/config/{doc}", json={"body": bodies[doc]})
        assert again.status_code == 200
        assert again.json()["version"] == 2


def test_put_models_all_disabled_is_accepted(client: TestClient) -> None:
    """Zero enabled model backends must save (AI features just go quiet).

    Boot already tolerates this via ``_build_model_manager`` (returns
    ``None``, AI plugins omitted); the PUT validator must match so the
    admin UI can turn every backend off without "Save failed".
    """
    body = {"fake": {"enabled": False, "type": "fake"}}
    resp = client.put("/api/v1/admin/config/models", json={"body": body})
    assert resp.status_code == 200, resp.text
    assert resp.json()["body"] == body
    # A stale primary pointing at the now-disabled backend saves too:
    # that is the exact doc the admin UI resubmits when switching the
    # last backend off.
    stale = {"primary": "fake", "fake": {"enabled": False, "type": "fake"}}
    resp = client.put("/api/v1/admin/config/models", json={"body": stale})
    assert resp.status_code == 200, resp.text
    assert resp.json()["body"] == stale


def test_put_overlays_running_app_config(
    client: TestClient, _toggle_provider: object
) -> None:
    body = {"expansion": {"synonym_sets": {"apparel": ["gown"]}}}
    resp = client.put("/api/v1/admin/config/query", json={"body": body})
    assert resp.status_code == 200
    assert client.app.state.config["query"] == body
    # Other docs still fall back to file sections.
    assert client.get("/api/v1/admin/config/plugins").json()["source"] == "file"


# -- strict bodies + validator 422s ----------------------------------------


def test_put_strict_body_rejects_extra_keys(client: TestClient) -> None:
    resp = client.put(
        "/api/v1/admin/config/query",
        json={"body": {}, "bogus": 1},
    )
    assert resp.status_code == 422
    assert set(resp.json().keys()) == {"error"}


def test_put_strict_body_requires_body_mapping(client: TestClient) -> None:
    resp = client.put("/api/v1/admin/config/query", json={"wrong": {}})
    assert resp.status_code == 422
    assert set(resp.json().keys()) == {"error"}


@pytest.mark.parametrize(
    ("doc", "body", "code"),
    [
        (
            "models",
            {"primary": "nope", "fake": {"enabled": True, "type": "fake"}},
            "invalid_request",
        ),
        # NOTE: {"primary": "fake", "fake": {"enabled": False, ...}} is
        # intentionally absent here: zero enabled backends is the valid
        # "no AI" state (see test_put_models_all_disabled_is_accepted),
        # and the admin UI resubmits a stale primary when switching the
        # last backend off. A primary dangling with backends enabled
        # (the "nope" case above) still 422s.
        ("providers", {"strategy": "bogus"}, "invalid_request"),
        (
            "providers",
            {"strategy": "single", "primary": "toggle_fake"},
            "invalid_request",
        ),
        ("ranking", {"default_profile": "nope"}, "invalid_request"),
        (
            "ranking",
            {"default_profile": "default", "profiles": {"bad": {"bogus_key": 1}}},
            "invalid_request",
        ),
        (
            "ranking",
            {"default_profile": "default", "profiles": {"bad": "nope"}},
            "invalid_request",
        ),
        (
            "query",
            {"normalizer": {"max_length": -5, "on_overflow": "truncate"}},
            "invalid_request",
        ),
        ("query", {"context": {"enabled": "yes"}}, "invalid_request"),
        ("plugins", [{"enabled": True}], "invalid_request"),
        ("plugins", [{"name": 123}], "invalid_request"),
        ("indexing", {"bogus_key": 1}, "invalid_request"),
        ("indexing", {"max_pages": 0}, "invalid_request"),
        ("indexing", "nope", "validation_error"),
        # Shape-level rejection (body must be a mapping or a list).
        ("plugins", "nope", "validation_error"),
    ],
)
def test_put_invalid_bodies_422_and_persist_nothing(
    client: TestClient, _toggle_provider: object, doc: str, body: object, code: str
) -> None:
    resp = client.put(f"/api/v1/admin/config/{doc}", json={"body": body})
    assert resp.status_code == 422, resp.text
    assert set(resp.json().keys()) == {"error"}
    assert resp.json()["error"]["code"] == code
    # Failed validation persists nothing: the read still serves the file.
    assert client.get(f"/api/v1/admin/config/{doc}").json()["source"] == "file"


# -- admin gate -------------------------------------------------------------


def test_put_requires_token_when_configured(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    _toggle_provider: object,
) -> None:
    monkeypatch.setenv(FRONTIER_ADMIN_TOKEN_ENV, "s3cret")
    body = _valid_bodies()["query"]
    denied = client.put("/api/v1/admin/config/query", json={"body": body})
    assert denied.status_code == 403
    assert denied.json() == {
        "error": {
            "code": "forbidden",
            "message": "frontier mutation requires admin rights",
        }
    }
    wrong = client.put(
        "/api/v1/admin/config/query",
        json={"body": body},
        headers={FRONTIER_ADMIN_HEADER: "wrong"},
    )
    assert wrong.status_code == 403
    # Reads stay open even with the gate set.
    assert client.get("/api/v1/admin/config/query").status_code == 200
    # The 403 persisted nothing.
    assert client.get("/api/v1/admin/config/query").json()["source"] == "file"
    allowed = client.put(
        "/api/v1/admin/config/query",
        json={"body": body},
        headers={FRONTIER_ADMIN_HEADER: "s3cret"},
    )
    assert allowed.status_code == 200
    assert allowed.json()["source"] == "db"


# -- precedence -------------------------------------------------------------


def test_db_row_wins_over_file_section(_toggle_provider: object) -> None:
    file_config = load_app_config()
    assert file_config, "shipped default.yaml must load for precedence test"
    store = SqliteAppConfigStore(":memory:")
    try:
        assert apply_db_config_overlay(dict(file_config), store) == file_config
        body = {"expansion": {"synonym_sets": {"apparel": ["gown"]}}}
        store.put("query", body)
        merged = apply_db_config_overlay(dict(file_config), store)
        assert merged["query"] == body
        for doc in CONFIG_DOC_KEYS:
            if doc != "query":
                assert merged[doc] == file_config.get(doc, {})
    finally:
        store.close()


# -- offline-safe boot -------------------------------------------------------


def test_create_app_store_is_lazy_and_side_effect_free() -> None:
    """create_app attaches the configured store without any I/O.

    The shipped default selects Postgres; construction must not connect
    (no server in tests/offline) and must not create files.
    """
    app = create_app(engine=FakeEngine())
    store = app.state.app_config_store
    assert isinstance(store, PostgresAppConfigStore)
    assert store._conn is None


def test_effective_config_falls_back_to_file_when_db_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unreachable Postgres degrades to the file config (offline-safe)."""
    monkeypatch.setenv("OPENLENS_PG_PORT", "1")
    file_config = load_app_config()
    assert file_config
    effective = load_effective_config()
    assert effective == file_config


def test_open_app_config_store_never_raises_or_connects() -> None:
    store = open_app_config_store(None)
    assert isinstance(store, SqliteAppConfigStore)
    store.close()
    store = open_app_config_store({"storage": "bogus"})
    assert isinstance(store, SqliteAppConfigStore)
    store.close()
    store = open_app_config_store(
        {"storage": {"backend": "postgres", "postgres": {"port": "not-a-port"}}}
    )
    assert isinstance(store, SqliteAppConfigStore)
    store.close()


# -- store unit behavior -----------------------------------------------------


def test_sqlite_store_round_trip_versions(tmp_path: Path) -> None:
    db_path = str(tmp_path / "app_config.db")
    store = SqliteAppConfigStore(db_path)
    try:
        assert store.get("query") is None
        first = store.put("query", {"a": 1})
        assert first.version == 1
        assert first.updated_at
        assert store.get("query") is not None
        assert store.get("query").body == {"a": 1}
        second = store.put("query", {"a": 2})
        assert second.version == 2
        assert store.get("query").body == {"a": 2}
    finally:
        store.close()
    # File-backed rows survive reopen (persistence, not just memory).
    reopened = SqliteAppConfigStore(db_path)
    try:
        doc = reopened.get("query")
        assert doc is not None
        assert doc.body == {"a": 2}
        assert doc.version == 2
    finally:
        reopened.close()


def test_postgres_ddl_and_from_config_shape() -> None:
    stmts = postgres_app_config_ddl("public")
    assert len(stmts) == 1
    assert "app_config_docs" in stmts[0]
    assert "JSONB" in stmts[0]
    store = PostgresAppConfigStore.from_config(
        {"host": "localhost", "port": "5433", "connect_timeout": "2"}
    )
    assert store._connect_kwargs["port"] == 5433
    assert store._connect_kwargs["connect_timeout"] == 2
    assert store._conn is None  # lazy: from_config never connects

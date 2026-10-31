"""Smoke tests for the API surface (step 7 minus observability wiring).

All tests run against a fake Wave-0 `Engine`: no network, no index, no
keys. They pin validation, pagination, applied-filter reporting, the
consistent error envelope, and diagnostics passthrough.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi.testclient import TestClient
import pytest

from openlens.core.interfaces import (
    ConfigError,
    Engine,
    PluginError,
    ProviderError,
    SearchRequest,
    SearchResponse,
    SearchResult,
)
from openlens.main import create_app


class FakeEngine(Engine):
    """Deterministic Engine stub: 25 ranked hits, filter echo, failures."""

    def __init__(self, total: int = 25, fail: Optional[str] = None) -> None:
        self._total = total
        self._fail = fail
        self._config: dict[str, Any] = {}
        self.last_request: Optional[SearchRequest] = None

    def configure(self, config: dict[str, Any]) -> None:
        self._config = config

    def search(self, request: SearchRequest) -> SearchResponse:
        self.last_request = request
        if self._fail == "provider":
            raise ProviderError.timeout("fake-provider")
        if self._fail == "plugin":
            raise PluginError(
                "filter exploded", plugin_name="fake-filter", mandatory=True
            )
        if self._fail == "config":
            raise ConfigError("bad config")
        if self._fail == "boom":
            raise RuntimeError("secret-token-abc123 must not leak")
        full = [
            SearchResult(
                id=f"doc-{i}",
                title=f"Title {i}",
                snippet=f"Snippet {i} for {request.query}",
                original_score=float(self._total - i),
                final_score=float(self._total - i),
                url=f"https://example.com/{i}",
                provider="fake-provider",
                reasons=[f"ranked #{i}"],
            )
            for i in range(self._total)
        ]
        page = full[request.offset : request.offset + request.limit]
        return SearchResponse(
            query=request.query,
            mode=request.mode,
            results=page,
            total=self._total,
            applied_filters=[f"{k}={v}" for k, v in sorted(request.filters.items())],
            diagnostics={
                "provider_latencies_ms": {"fake-provider": 1.5},
                "plugin_executions": [
                    {
                        "plugin": "fake-filter",
                        "stage": "filtering",
                        "status": "ok",
                        "latency_ms": 0.5,
                        "error": None,
                    }
                ],
                "errors": [],
            },
        )

    def health_check(self) -> dict[str, bool]:
        return {"fake-provider": True}


@pytest.fixture(autouse=True)
def _allow_open_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Legacy local-dev contract under test: mutations open without a token."""
    from openlens.api.routes import ALLOW_OPEN_ADMIN_ENV

    monkeypatch.setenv(ALLOW_OPEN_ADMIN_ENV, "true")


def make_client(**kwargs: Any) -> TestClient:
    return TestClient(create_app(engine=FakeEngine(**kwargs)))


def assert_envelope(resp: Any, code: str) -> None:
    body = resp.json()
    assert set(body.keys()) == {"error"}, body
    assert set(body["error"].keys()) == {"code", "message"}, body
    assert body["error"]["code"] == code, body


# --- happy path -----------------------------------------------------------


def test_search_ok_shape() -> None:
    resp = make_client().post("/api/v1/search", json={"query": "cats"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["query"] == "cats"
    assert body["mode"] == "default"
    assert body["total"] == 25
    assert len(body["results"]) == 10  # default limit
    first = body["results"][0]
    assert first["id"] == "doc-0"
    assert first["provider"] == "fake-provider"
    assert first["original_score"] == 25.0
    assert first["reasons"] == ["ranked #0"]
    assert body["applied_filters"] == []
    assert body["diagnostics"]["provider_latencies_ms"] == {"fake-provider": 1.5}


def test_search_defaults_and_modes() -> None:
    client = make_client()
    resp = client.post("/api/v1/search", json={"query": "dogs", "mode": "news"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["mode"] == "news"


def test_engine_receives_core_request() -> None:
    engine = FakeEngine()
    client = TestClient(create_app(engine=engine))
    resp = client.post(
        "/api/v1/search",
        json={"query": "  padded  ", "limit": 3, "filters": {"lang": "en"}},
    )
    assert resp.status_code == 200, resp.text
    assert engine.last_request is not None
    assert engine.last_request.query == "padded"
    assert engine.last_request.limit == 3
    assert engine.last_request.filters == {"lang": "en"}


# --- pagination ------------------------------------------------------------


def test_pagination_slices_and_keeps_total() -> None:
    client = make_client()
    resp = client.post("/api/v1/search", json={"query": "q", "limit": 5, "offset": 5})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [r["id"] for r in body["results"]] == [f"doc-{i}" for i in range(5, 10)]
    assert body["total"] == 25  # pre-pagination total, not page size


def test_pagination_tail_page() -> None:
    resp = make_client().post(
        "/api/v1/search", json={"query": "q", "limit": 10, "offset": 22}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [r["id"] for r in body["results"]] == ["doc-22", "doc-23", "doc-24"]
    assert body["total"] == 25


# --- applied-filter reporting -------------------------------------------------


def test_applied_filters_reported() -> None:
    resp = make_client().post(
        "/api/v1/search", json={"query": "q", "filters": {"lang": "en", "safe": "true"}}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["applied_filters"] == ["lang=en", "safe=true"]


# --- validation ------------------------------------------------------------


def test_validation_missing_query() -> None:
    resp = make_client().post("/api/v1/search", json={})
    assert resp.status_code == 422, resp.text
    assert_envelope(resp, "validation_error")


def test_validation_empty_query() -> None:
    resp = make_client().post("/api/v1/search", json={"query": ""})
    assert resp.status_code == 422, resp.text
    assert_envelope(resp, "validation_error")


def test_validation_blank_query() -> None:
    resp = make_client().post("/api/v1/search", json={"query": "   "})
    assert resp.status_code == 422, resp.text
    assert_envelope(resp, "validation_error")


def test_validation_limit_bounds() -> None:
    client = make_client()
    for bad in (0, -1, 101, 1000):
        resp = client.post("/api/v1/search", json={"query": "q", "limit": bad})
        assert resp.status_code == 422, (bad, resp.text)
        assert_envelope(resp, "validation_error")


def test_validation_negative_offset() -> None:
    resp = make_client().post("/api/v1/search", json={"query": "q", "offset": -1})
    assert resp.status_code == 422, resp.text
    assert_envelope(resp, "validation_error")


def test_validation_limit_ok_at_max() -> None:
    resp = make_client().post("/api/v1/search", json={"query": "q", "limit": 100})
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["results"]) == 25


# --- error shape -----------------------------------------------------------


def test_provider_error_maps_to_502_envelope() -> None:
    resp = make_client(fail="provider").post("/api/v1/search", json={"query": "q"})
    assert resp.status_code == 502, resp.text
    assert_envelope(resp, "provider_timeout")


def test_plugin_error_maps_to_500_envelope() -> None:
    resp = make_client(fail="plugin").post("/api/v1/search", json={"query": "q"})
    assert resp.status_code == 500, resp.text
    assert_envelope(resp, "plugin_error")


def test_config_error_maps_to_500_envelope() -> None:
    resp = make_client(fail="config").post("/api/v1/search", json={"query": "q"})
    assert resp.status_code == 500, resp.text
    assert_envelope(resp, "config_error")


def test_unexpected_error_is_generic_500() -> None:
    client = TestClient(
        create_app(engine=FakeEngine(fail="boom")), raise_server_exceptions=False
    )
    resp = client.post("/api/v1/search", json={"query": "q"})
    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert_envelope(resp, "internal_error")
    assert "secret-token-abc123" not in body["error"]["message"]


def test_missing_engine_is_503_envelope() -> None:
    resp = TestClient(create_app()).post("/api/v1/search", json={"query": "q"})
    assert resp.status_code == 503, resp.text
    assert_envelope(resp, "engine_unavailable")


def test_unknown_route_uses_envelope() -> None:
    resp = make_client().get("/api/v1/nope")
    assert resp.status_code == 404, resp.text
    assert_envelope(resp, "http_error")


# --- misc ------------------------------------------------------------------


def test_healthz() -> None:
    resp = make_client().get("/healthz")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "ok"}


def test_cors_preflight_allows_localhost() -> None:
    # Browser dev servers (vite :5173) call the API cross-origin; the
    # preflight must succeed instead of surfacing as a 405 http_error.
    resp = make_client().options(
        "/api/v1/search",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_cors_preflight_rejects_non_localhost() -> None:
    resp = make_client().options(
        "/api/v1/search",
        headers={
            "Origin": "https://example.com",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert "access-control-allow-origin" not in resp.headers


# --- suggest ---------------------------------------------------------------


def test_suggest_happy_path() -> None:
    resp = make_client().get("/api/v1/suggest", params={"q": "cof", "limit": 5})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["q"] == "cof"
    assert body["suggestions"] == ["coffee", "coffee maker"]


def test_suggest_limit_caps_results() -> None:
    resp = make_client().get("/api/v1/suggest", params={"q": "cof", "limit": 1})
    assert resp.status_code == 200, resp.text
    assert resp.json()["suggestions"] == ["coffee"]


def test_suggest_empty_q_returns_empty_list() -> None:
    client = make_client()
    for params in ({"q": ""}, {"q": "   "}, {}):
        resp = client.get("/api/v1/suggest", params=params)
        assert resp.status_code == 200, resp.text
        assert resp.json()["suggestions"] == [], params


def test_suggest_no_match_returns_empty_list() -> None:
    resp = make_client().get("/api/v1/suggest", params={"q": "zxqv"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["suggestions"] == []


def test_suggest_uses_app_state_vocabulary() -> None:
    app = create_app(engine=FakeEngine())
    app.state.suggest_terms = ["zebra crossing", "zebra finch"]
    resp = TestClient(app).get("/api/v1/suggest", params={"q": "zeb"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["suggestions"] == ["zebra crossing", "zebra finch"]


# --- search assistance -------------------------------------------------------


def test_search_assistance_populated() -> None:
    resp = make_client().post("/api/v1/search", json={"query": "running"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["suggestions"] == ["running shoes", "running watch"]
    assert body["related"] == ["running shoes", "running watch"]
    assert body["spelling"] is None  # exact term needs no correction


def test_search_spelling_correction_offered() -> None:
    resp = make_client(total=0).post("/api/v1/search", json={"query": "runing shoes"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["spelling"] == "running shoes"


def test_search_spelling_suppressed_when_results_exist() -> None:
    resp = make_client().post("/api/v1/search", json={"query": "runing shoes"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["spelling"] is None


def test_search_assistance_absent_for_unknown_query() -> None:
    resp = make_client().post("/api/v1/search", json={"query": "zxqv wobble"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["suggestions"] == []
    assert body["spelling"] is None
    assert body["related"] == []


# --- pagination hint ---------------------------------------------------------


def test_search_next_offset_math() -> None:
    client = make_client()
    first = client.post(
        "/api/v1/search", json={"query": "q", "limit": 10, "offset": 0}
    ).json()
    assert (first["limit"], first["offset"], first["total"]) == (10, 0, 25)
    assert first["next_offset"] == 10
    mid = client.post(
        "/api/v1/search", json={"query": "q", "limit": 10, "offset": 10}
    ).json()
    assert mid["next_offset"] == 20
    tail = client.post(
        "/api/v1/search", json={"query": "q", "limit": 10, "offset": 22}
    ).json()
    assert tail["next_offset"] is None
    exact = client.post(
        "/api/v1/search", json={"query": "q", "limit": 25, "offset": 0}
    ).json()
    assert exact["next_offset"] is None  # page reaching total has no next


# --- feedback ----------------------------------------------------------------


def test_feedback_counts_only() -> None:
    client = make_client()
    payload = {"doc_id": "feedback-doc-a", "action": "click"}
    first = client.post("/api/v1/feedback", json=payload)
    assert first.status_code == 200, first.text
    assert first.json() == {**payload, "count": 1}
    second = client.post("/api/v1/feedback", json=payload)
    assert second.status_code == 200, second.text
    assert second.json()["count"] == 2
    other_action = client.post(
        "/api/v1/feedback",
        json={"doc_id": "feedback-doc-a", "action": "impression"},
    )
    assert other_action.json()["count"] == 1  # counted per (doc, action)


def test_feedback_rejects_extra_fields_and_blanks() -> None:
    client = make_client()
    smuggled = client.post(
        "/api/v1/feedback",
        json={"doc_id": "d", "action": "click", "query": "cats"},
    )
    assert smuggled.status_code == 422, smuggled.text  # no query text kept
    blank = client.post("/api/v1/feedback", json={"doc_id": "   ", "action": "click"})
    assert blank.status_code == 422, blank.text
    missing = client.post("/api/v1/feedback", json={"doc_id": "d"})
    assert missing.status_code == 422, missing.text


def test_feedback_body_carries_no_identifiers() -> None:
    resp = make_client().post(
        "/api/v1/feedback",
        json={"doc_id": "feedback-doc-b", "action": "click"},
    )
    assert resp.status_code == 200, resp.text
    assert set(resp.json().keys()) == {"doc_id", "action", "count"}


def test_feedback_reason_taxonomy_and_summary() -> None:
    from openlens.api import routes as routes_mod

    client = make_client()
    saved = dict(routes_mod._FEEDBACK_COUNTS)
    routes_mod._FEEDBACK_COUNTS.clear()
    try:
        ok = client.post(
            "/api/v1/feedback",
            json={"doc_id": "reason-doc", "action": "not_helpful", "reason": "Privacy"},
        )
        assert ok.status_code == 200, ok.text
        assert ok.json() == {
            "doc_id": "reason-doc",
            "action": "not_helpful",
            "count": 1,
            "reason": "privacy",
        }
        bad_reason = client.post(
            "/api/v1/feedback",
            json={"doc_id": "reason-doc", "action": "not_helpful", "reason": "nope"},
        )
        assert bad_reason.status_code == 422
        reason_on_positive = client.post(
            "/api/v1/feedback",
            json={"doc_id": "reason-doc", "action": "click", "reason": "privacy"},
        )
        assert reason_on_positive.status_code == 422
        client.post(
            "/api/v1/feedback", json={"doc_id": "reason-doc", "action": "click"}
        )
        summary = client.get("/api/v1/admin/feedback/summary")
        assert summary.status_code == 200, summary.text
        body = summary.json()
        assert body["total"] == 2
        assert body["by_action"] == {"not_helpful": 1, "click": 1}
        assert body["by_reason"] == {"privacy": 1, "unspecified": 1}
        assert body["top_docs"] == [
            {
                "doc_id": "reason-doc",
                "total": 2,
                "helpful": 0,
                "not_helpful": 1,
                "reports": 0,
                "top_reason": "privacy",
            }
        ]
    finally:
        routes_mod._FEEDBACK_COUNTS.clear()
        routes_mod._FEEDBACK_COUNTS.update(saved)


def test_feedback_summary_gated_when_token_set(monkeypatch: pytest.MonkeyPatch) -> None:
    from openlens.api.routes import FRONTIER_ADMIN_HEADER, FRONTIER_ADMIN_TOKEN_ENV

    client = make_client()
    monkeypatch.setenv(FRONTIER_ADMIN_TOKEN_ENV, "s3cret")
    denied = client.get("/api/v1/admin/feedback/summary")
    assert denied.status_code == 403
    allowed = client.get(
        "/api/v1/admin/feedback/summary", headers={FRONTIER_ADMIN_HEADER: "s3cret"}
    )
    assert allowed.status_code == 200

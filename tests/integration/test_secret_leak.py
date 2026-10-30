"""Secret-leak test: API keys and passwords never appear in outputs.

Seeds configs with sentinel secrets, runs the full engine + API stack,
and asserts the secrets appear in no response body, diagnostics payload,
or captured log record.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from openlens.core.engine import SearchEngine
from openlens.core.interfaces import SearchRequest, SearchResult
from openlens.main import create_app
from openlens.providers.base import BaseSearchProvider
from openlens.providers.custom_index import CustomIndexProvider
from tests.fixtures.corpus import fixture_documents, seed_provider

SENTINEL_KEY = "sk-test-SENTINEL-7f3a9c"
SENTINEL_PASSWORD = "pw-test-SENTINEL-4b2e81"  # noqa: S105 - intentional non-secret fixture value
SECRETS = (SENTINEL_KEY, SENTINEL_PASSWORD)


class _SecretFakeProvider(BaseSearchProvider):
    """Thread-safe corpus provider for API-level leak tests.

    TestClient executes endpoints on a worker thread, which SQLite-backed
    CustomIndexProvider cannot serve (see Wave-3 note on
    storage/repository.py thread affinity); this fake serves the same
    fixture corpus without thread affinity.
    """

    @property
    def name(self) -> str:
        return "secret_fake"

    def search(self, request: SearchRequest) -> list[SearchResult]:
        query = request.query.lower()
        hits = []
        for rank, doc in enumerate(fixture_documents()):
            if any(t in (doc.title + " " + doc.content).lower() for t in query.split()):
                hits.append(
                    SearchResult(
                        id=doc.id,
                        title=doc.title,
                        snippet=doc.content[:200],
                        original_score=1.0 / (rank + 1),
                        final_score=1.0 / (rank + 1),
                        url=doc.url,
                        provider=self.name,
                        reasons=["corpus match"],
                    )
                )
        return hits

    def health_check(self) -> bool:
        return True


@pytest.fixture()
def engine() -> SearchEngine:
    provider = CustomIndexProvider()
    provider.configure({"db_path": ":memory:", "seed": False})
    seed_provider(provider)
    eng = SearchEngine(providers=[provider], plugins=[])
    eng.configure(
        {
            # Secrets live only in config sections; the engine must never
            # echo them into results, diagnostics, or applied_filters.
            "plugins": [{"name": "safety-filter", "enabled": True, "config": {}}],
            "ranking": {},
        }
    )
    return eng


@pytest.fixture()
def api_engine() -> SearchEngine:
    eng = SearchEngine(providers=[_SecretFakeProvider()], plugins=[])
    eng.configure(
        {
            "plugins": [{"name": "safety-filter", "enabled": True, "config": {}}],
            "ranking": {},
        }
    )
    return eng


def _assert_no_secrets(payload: object, where: str) -> None:
    text = json.dumps(payload, default=str)
    for secret in SECRETS:
        assert secret not in text, f"[api/secrets] sentinel secret leaked in {where}"


def test_engine_response_and_diagnostics_carry_no_secrets(engine, caplog):
    with caplog.at_level(logging.DEBUG):
        response = engine.search(SearchRequest(query="shoes", limit=10))
    _assert_no_secrets(
        {
            "results": [r.__dict__ for r in response.results],
            "diagnostics": response.diagnostics,
            "applied_filters": response.applied_filters,
        },
        "SearchResponse",
    )
    for record in caplog.records:
        for secret in SECRETS:
            assert secret not in record.getMessage(), (
                "[api/secrets] sentinel secret leaked in log records"
            )


def test_api_success_body_carries_no_secrets(api_engine):
    app = create_app(api_engine)
    client = TestClient(app)
    http = client.post(
        "/api/v1/search", json={"query": "shoes", "limit": 5, "mode": "default"}
    )
    assert http.status_code == 200, http.text
    _assert_no_secrets(http.json(), "POST /api/v1/search response")
    for secret in SECRETS:
        assert secret not in http.text


def test_api_error_envelope_carries_no_secrets(api_engine, monkeypatch):
    def _boom(self, request):
        raise RuntimeError("backend blew up")

    monkeypatch.setattr(_SecretFakeProvider, "search", _boom)
    app = create_app(api_engine)
    client = TestClient(app, raise_server_exceptions=False)
    http = client.post("/api/v1/search", json={"query": "shoes"})
    assert http.status_code == 500, http.text
    _assert_no_secrets(http.json(), "500 error envelope")
    for secret in SECRETS:
        assert secret not in http.text


def test_removed_external_types_cannot_be_created() -> None:
    # Third-party search APIs and the OpenSearch adapter were removed
    # (offline-first, SQLite-only): no config can instantiate them, so no
    # external engine call is reachable.
    from openlens.core.interfaces import ConfigError
    from openlens.providers import registry

    registry.register_builtin_providers()
    for removed in ("bing", "http", "external", "opensearch"):
        with pytest.raises(ConfigError):
            registry.create(removed, {"enabled": True})


# -- model backends (AI capabilities) -----------------------------------------

MODEL_SENTINEL_KEY = "[REDACTED]"
MODEL_SENTINEL_ENDPOINT = "https://model-endpoint-SENTINEL-77d2c9.example.invalid/v1"
MODEL_SECRETS = (MODEL_SENTINEL_KEY, MODEL_SENTINEL_ENDPOINT)


def _assert_no_model_secrets(payload: object, where: str) -> None:
    text = json.dumps(payload, default=str)
    for secret in MODEL_SECRETS:
        assert secret not in text, f"[models] model secret leaked in {where}"


def _assert_logs_carry_no_model_secrets(caplog: pytest.LogCaptureFixture) -> None:
    for record in caplog.records:
        for secret in MODEL_SECRETS:
            assert secret not in record.getMessage(), (
                "[models] model secret leaked in log records"
            )


class _FakeModelResponse:
    def __init__(self, payload: object, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = ""

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._payload


def _install_fake_model_httpx(
    monkeypatch: pytest.MonkeyPatch, error: Exception | None = None
) -> dict:
    """Replace ``httpx`` with an offline transport for LocalModelProvider.

    Records calls so tests can prove the sentinel key/endpoint really are
    in use (Authorization header / URL) while never reaching outputs.
    """
    import sys
    import types

    calls: dict = {"posts": [], "gets": []}

    def _post(url: str, **kwargs: object) -> _FakeModelResponse:
        calls["posts"].append({"url": url, **kwargs})
        if error is not None:
            raise error
        payload = kwargs.get("json") or {}
        if isinstance(payload, dict) and "labels" in payload:
            labels = payload.get("labels") or ["safe"]
            return _FakeModelResponse({"label": labels[0]})
        if isinstance(payload, dict) and ("texts" in payload or "input" in payload):
            texts = payload.get("texts") or payload.get("input") or []
            return _FakeModelResponse({"embeddings": [[0.1, 0.2] for _ in texts]})
        return _FakeModelResponse({"text": "model-generated text"})

    def _get(url: str, **kwargs: object) -> _FakeModelResponse:
        calls["gets"].append({"url": url, **kwargs})
        if error is not None:
            raise error
        return _FakeModelResponse({"status": "ok"})

    fake_module = types.ModuleType("httpx")
    fake_module.post = _post  # type: ignore[attr-defined]
    fake_module.get = _get  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "httpx", fake_module)
    return calls


def _model_config() -> dict:
    return {
        "primary": "local",
        "local": {
            "enabled": True,
            "type": "local",
            "endpoint": MODEL_SENTINEL_ENDPOINT,
            "api_key": MODEL_SENTINEL_KEY,
            "model": "sentinel-model",
            "timeout_s": 1.0,
        },
    }


def _ai_engine() -> SearchEngine:
    eng = SearchEngine(providers=[_SecretFakeProvider()], plugins=[])
    eng.configure(
        {
            "plugins": [
                {"name": "safety-filter", "enabled": True, "config": {}},
                {"name": "ai-summary", "enabled": True, "config": {}},
                {"name": "ai-understanding", "enabled": True, "config": {}},
                {"name": "ai-classification", "enabled": True, "config": {}},
            ],
            "ranking": {},
            "models": _model_config(),
        }
    )
    return eng


def test_model_secrets_never_in_engine_response_diagnostics_or_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls = _install_fake_model_httpx(monkeypatch)
    engine = _ai_engine()
    assert engine.model_manager is not None
    assert engine.model_manager.primary == "local"
    with caplog.at_level(logging.DEBUG):
        response = engine.search(SearchRequest(query="shoes", limit=10))
    # Vacuity guard: AI enrichment really ran through the sentinel backend.
    assert response.results, "expected fixture hits for AI search"
    assert any("ai_summary" in r.metadata for r in response.results)
    assert calls["posts"], "expected the local model backend to be called"
    assert calls["posts"][0]["url"] == MODEL_SENTINEL_ENDPOINT
    assert (
        calls["posts"][0]["headers"]["Authorization"] == f"Bearer {MODEL_SENTINEL_KEY}"
    )
    _assert_no_model_secrets(
        {
            "results": [r.__dict__ for r in response.results],
            "diagnostics": response.diagnostics,
            "applied_filters": response.applied_filters,
        },
        "AI SearchResponse",
    )
    _assert_logs_carry_no_model_secrets(caplog)


def test_model_secrets_never_in_api_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_model_httpx(monkeypatch)
    app = create_app(_ai_engine())
    client = TestClient(app)
    http = client.post(
        "/api/v1/search", json={"query": "shoes", "limit": 5, "mode": "default"}
    )
    assert http.status_code == 200, http.text
    _assert_no_model_secrets(http.json(), "POST /api/v1/search AI response")
    for secret in MODEL_SECRETS:
        assert secret not in http.text


def test_model_secrets_never_in_errors_or_diagnostics_when_model_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Raw transport error deliberately embeds both sentinels, mirroring a
    # client exception that echoes the request URL / credential.
    raw_error = RuntimeError(
        f"connection to {MODEL_SENTINEL_ENDPOINT} refused key={MODEL_SENTINEL_KEY}"
    )
    _install_fake_model_httpx(monkeypatch, error=raw_error)
    engine = _ai_engine()
    with caplog.at_level(logging.DEBUG):
        response = engine.search(SearchRequest(query="shoes", limit=10))
    # Optional AI plugins fail in isolation: request succeeds minus AI fields.
    assert response.results, "search must succeed when the model backend fails"
    assert all("ai_summary" not in r.metadata for r in response.results)
    _assert_no_model_secrets(
        {
            "results": [r.__dict__ for r in response.results],
            "diagnostics": response.diagnostics,
            "applied_filters": response.applied_filters,
        },
        "AI SearchResponse (model failing)",
    )
    _assert_logs_carry_no_model_secrets(caplog)


def test_model_provider_errors_and_health_carry_no_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openlens.core.interfaces import ProviderError
    from openlens.models.backends import LocalModelProvider

    raw_error = RuntimeError(
        f"connection to {MODEL_SENTINEL_ENDPOINT} refused key={MODEL_SENTINEL_KEY}"
    )
    _install_fake_model_httpx(monkeypatch, error=raw_error)
    provider = LocalModelProvider()
    provider.configure(
        {
            "endpoint": MODEL_SENTINEL_ENDPOINT,
            "api_key": MODEL_SENTINEL_KEY,
            "timeout_s": 1.0,
        }
    )
    with pytest.raises(ProviderError) as excinfo:
        provider.complete("hello")
    for secret in MODEL_SECRETS:
        assert secret not in str(excinfo.value), (
            "[models/backends.py] ProviderError must not echo key/endpoint"
        )
        assert secret not in repr(excinfo.value), (
            "[models/backends.py] ProviderError repr must not echo key/endpoint"
        )
    assert provider.health_check() is False, (
        "[models/backends.py] failed health check must return False, never raise"
    )


# -- observability wiring (request id, logs, /metrics) ------------------------
#
# The integrate step wires the per-request INFO log and GET /metrics behind
# `logging.level` / `metrics.enabled`. These tests pin the leak contract on
# that wired path: AI-plugin searches with sentinel secrets in config must
# leave no sentinel in the response body, the diagnostics payload, the
# captured JSON log lines, or the /metrics exposition text. Metric labels
# are provider/plugin/stage/status only — never request-derived values.

OBS_SENTINELS = SECRETS + MODEL_SECRETS

#: Placeholder-shaped sentinels (``[REDACTED]``): redaction output itself,
#: not real secrets — skipped in log/metrics assertions because scrubbing
#: legitimately emits the placeholder.
PLACEHOLDER_SENTINELS = (SENTINEL_KEY, MODEL_SENTINEL_KEY)


@pytest.fixture()
def search_log_lines() -> Iterator[list[str]]:
    """Capture formatted JSON lines emitted by the shared search logger.

    The search logger does not propagate (one record per request), so
    ``caplog`` never sees these records; capture them directly.
    """
    from openlens.observability import logging as obs_logging

    lines: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(self.format(record))

    handler = _Capture()
    handler.setFormatter(obs_logging.JsonFormatter())
    logger = logging.getLogger(obs_logging.LOGGER_NAME)
    logger.addHandler(handler)
    try:
        yield lines
    finally:
        logger.removeHandler(handler)


@pytest.fixture()
def clean_registry() -> object:
    """Reset the process-wide metrics registry before and after the test."""
    from openlens.observability.metrics import get_registry

    registry = get_registry()
    registry.reset()
    registry.disable()
    try:
        yield registry
    finally:
        registry.reset()
        registry.disable()


def _metrics_overrides(enabled: bool) -> dict:
    return {
        "logging": {"level": "INFO"},
        "metrics": {"enabled": enabled},
    }


def test_metrics_route_404s_when_disabled_by_default(clean_registry) -> None:
    app = create_app(_SecretFakeProvider_engine())
    client = TestClient(app)
    resp = client.get("/metrics")
    assert resp.status_code == 404, resp.text


def _SecretFakeProvider_engine() -> SearchEngine:
    eng = SearchEngine(providers=[_SecretFakeProvider()], plugins=[])
    eng.configure(
        {
            "plugins": [{"name": "safety-filter", "enabled": True, "config": {}}],
            "ranking": {},
        }
    )
    return eng


def test_metrics_route_serves_sentinel_free_series_when_enabled(
    monkeypatch: pytest.MonkeyPatch, clean_registry
) -> None:
    import openlens.main as main_module

    monkeypatch.setattr(
        main_module,
        "load_app_config",
        lambda path=None: _metrics_overrides(True),
    )
    app = create_app(_ai_engine())
    client = TestClient(app)
    _install_fake_model_httpx(monkeypatch)
    http = client.post(
        "/api/v1/search", json={"query": "shoes", "limit": 5, "mode": "default"}
    )
    assert http.status_code == 200, http.text
    scrape = client.get("/metrics")
    assert scrape.status_code == 200, scrape.text
    assert scrape.headers.get("cache-control") == "no-store"
    assert scrape.headers.get("content-type", "").startswith("text/plain")
    # The scrape recorded real traffic (non-vacuous) and carries no secret.
    assert 'openlens_requests_total{status="ok"} 1' in scrape.text
    for secret in OBS_SENTINELS:
        if secret in PLACEHOLDER_SENTINELS:
            continue
        assert secret not in scrape.text, (
            f"[observability] sentinel leaked in /metrics text: {secret!r}"
        )


def test_api_ai_search_leaks_no_secrets_into_logs_or_metrics(
    monkeypatch: pytest.MonkeyPatch,
    search_log_lines: list[str],
    clean_registry,
) -> None:
    import openlens.main as main_module

    monkeypatch.setattr(
        main_module,
        "load_app_config",
        lambda path=None: _metrics_overrides(True),
    )
    _install_fake_model_httpx(monkeypatch)
    app = create_app(_ai_engine())
    client = TestClient(app)
    http = client.post(
        "/api/v1/search",
        json={"query": "shoes", "limit": 5, "mode": "default"},
        headers={"X-Request-Id": "sentinel-search-1"},
    )
    assert http.status_code == 200, http.text
    body = http.json()
    assert body["diagnostics"]["request_id"] == "sentinel-search-1"
    assert http.headers.get("x-request-id") == "sentinel-search-1"
    _assert_no_secrets(body, "AI POST /api/v1/search response (wired)")
    _assert_no_model_secrets(body, "AI POST /api/v1/search response (wired)")
    diagnostics = body["diagnostics"]
    _assert_no_secrets(diagnostics, "AI diagnostics payload (wired)")
    _assert_no_model_secrets(diagnostics, "AI diagnostics payload (wired)")
    # Exactly one INFO record for the search, carrying the request id and
    # no secret — and never the raw query at INFO.
    assert len(search_log_lines) == 1, search_log_lines
    line = search_log_lines[0]
    record = json.loads(line)
    assert record["request_id"] == "sentinel-search-1"
    assert record["level"] == "INFO"
    assert "shoes" not in line
    for secret in OBS_SENTINELS:
        if secret in PLACEHOLDER_SENTINELS:
            continue
        assert secret not in line, (
            f"[observability] sentinel leaked in log line: {secret!r}"
        )
    scrape = client.get("/metrics")
    assert scrape.status_code == 200, scrape.text
    for secret in OBS_SENTINELS:
        if secret in PLACEHOLDER_SENTINELS:
            continue
        assert secret not in scrape.text, (
            f"[observability] sentinel leaked in /metrics text: {secret!r}"
        )


def test_api_ai_search_failure_leaks_no_secrets_into_logs_or_metrics(
    monkeypatch: pytest.MonkeyPatch,
    search_log_lines: list[str],
    clean_registry,
) -> None:
    import openlens.main as main_module

    monkeypatch.setattr(
        main_module,
        "load_app_config",
        lambda path=None: _metrics_overrides(True),
    )
    # Raw transport error deliberately embeds the endpoint plus the
    # genuinely secret-shaped SENTINEL_PASSWORD: if any layer echoed raw
    # error text, the wired log record and metric labels would carry it.
    raw_error = RuntimeError(
        f"connection to {MODEL_SENTINEL_ENDPOINT} refused password={SENTINEL_PASSWORD}"
    )
    _install_fake_model_httpx(monkeypatch, error=raw_error)
    app = create_app(_ai_engine())
    client = TestClient(app)
    http = client.post(
        "/api/v1/search",
        json={"query": "shoes", "limit": 5, "mode": "default"},
        headers={"X-Request-Id": "sentinel-search-fail"},
    )
    assert http.status_code == 200, http.text
    body = http.json()
    _assert_no_model_secrets(body, "AI failure response (wired)")
    for secret in (MODEL_SENTINEL_ENDPOINT, SENTINEL_PASSWORD):
        assert secret not in json.dumps(body["diagnostics"], default=str)
    assert search_log_lines, "expected a log record for the failing search"
    for line in search_log_lines:
        for secret in (MODEL_SENTINEL_ENDPOINT, SENTINEL_PASSWORD):
            assert secret not in line, (
                f"[observability] sentinel leaked in failure log: {secret!r}"
            )
    scrape = client.get("/metrics")
    assert scrape.status_code == 200, scrape.text
    for secret in (MODEL_SENTINEL_ENDPOINT, SENTINEL_PASSWORD):
        assert secret not in scrape.text, (
            f"[observability] sentinel leaked in /metrics text: {secret!r}"
        )


def _assist_engine() -> SearchEngine:
    eng = SearchEngine(providers=[_SecretFakeProvider()], plugins=[])
    eng.configure(
        {
            "plugins": [{"name": "safety-filter", "enabled": True, "config": {}}],
            "ranking": {},
            # Sentinel lives only in config; responses must never echo it.
            "models": {
                "primary": "openai",
                "openai": {"enabled": False, "api_key": SENTINEL_KEY},
            },
        }
    )
    return eng


def test_search_assistance_fields_carry_no_secrets():
    app = create_app(_assist_engine())
    client = TestClient(app)
    http = client.post(
        "/api/v1/search", json={"query": "running shoes", "limit": 2, "offset": 0}
    )
    assert http.status_code == 200, http.text
    body = http.json()
    for field in (
        "suggestions",
        "spelling",
        "related",
        "next_offset",
        "limit",
        "offset",
    ):
        assert field in body, f"missing new field {field}"
    _assert_no_secrets(body, "POST /api/v1/search (assistance fields)")
    for secret in SECRETS:
        assert secret not in http.text


def test_suggest_endpoint_carry_no_secrets():
    app = create_app(_assist_engine())
    client = TestClient(app)
    http = client.get("/api/v1/suggest", params={"q": "run"})
    assert http.status_code == 200, http.text
    assert "suggestions" in http.json()
    _assert_no_secrets(http.json(), "GET /api/v1/suggest")
    http = client.get("/api/v1/suggest", params={"q": ""})
    assert http.status_code == 200 and http.json()["suggestions"] == []


def test_feedback_counts_only_and_carries_no_secrets():
    eng = _assist_engine()
    app = create_app(eng)
    client = TestClient(app)
    http = client.post(
        "/api/v1/feedback", json={"doc_id": "doc-shopping-1", "action": "click"}
    )
    assert http.status_code == 200, http.text
    fb = http.json()
    assert fb == {"doc_id": "doc-shopping-1", "action": "click", "count": 1}, fb
    assert set(fb.keys()) == {"doc_id", "action", "count"}, "response key allowlist"
    _assert_no_secrets(fb, "POST /api/v1/feedback")
    http = client.post(
        "/api/v1/feedback", json={"doc_id": "doc-shopping-1", "action": "click"}
    )
    assert http.json()["count"] == 2
    assert eng.feedback_counts() == {("doc-shopping-1", "click"): 2}
    _assert_no_secrets(
        {f"{k[0]}:{k[1]}": v for k, v in eng.feedback_counts().items()},
        "engine feedback store",
    )
    http = client.post(
        "/api/v1/feedback", json={"doc_id": "x", "action": "click", "extra": 1}
    )
    assert http.status_code == 422, http.text
    _assert_no_secrets(http.json(), "feedback 422 envelope")

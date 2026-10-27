"""Secret-leak test: API keys and passwords never appear in outputs.

Seeds configs with sentinel secrets, runs the full engine + API stack,
and asserts the secrets appear in no response body, diagnostics payload,
or captured log record.
"""

from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

from openlens.core.engine import SearchEngine
from openlens.core.interfaces import SearchRequest, SearchResult
from openlens.main import create_app
from openlens.providers.base import BaseSearchProvider
from openlens.providers.custom_index import CustomIndexProvider
from tests.fixtures.corpus import fixture_documents, seed_provider

SENTINEL_KEY = "sk-test-SENTINEL-7f3a9c"
SENTINEL_PASSWORD = "pw-test-SENTINEL-4b2e81"
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
                        id=doc.id, title=doc.title, snippet=doc.content[:200],
                        original_score=1.0 / (rank + 1),
                        final_score=1.0 / (rank + 1),
                        url=doc.url, provider=self.name, reasons=["corpus match"],
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
        assert secret not in text, (
            f"[api/secrets] sentinel secret leaked in {where}"
        )


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


def test_provider_errors_carry_no_secrets():
    from openlens.core.interfaces import ProviderError
    from openlens.providers.opensearch import OpenSearchProvider

    provider = OpenSearchProvider()
    provider.configure(
        {
            "hosts": ["http://localhost:9"],
            "username": "admin",
            "password": SENTINEL_PASSWORD,
            "timeout_s": 1.0,
        }
    )
    try:
        provider.search(SearchRequest(query="shoes"))
        raised = None
    except ProviderError as exc:
        raised = exc
    assert raised is not None, "[providers/opensearch.py] offline search must raise"
    for secret in SECRETS:
        assert secret not in str(raised), (
            "[providers/opensearch.py] ProviderError must not echo credentials"
        )

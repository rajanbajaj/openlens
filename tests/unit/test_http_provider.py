"""Unit tests for CustomApiSearchProvider (integration plan step 2).

All tests are offline: ``_fetch`` is stubbed or a fake ``httpx`` module is
injected into ``sys.modules`` — no network access, no API keys.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from openlens.core.interfaces import ConfigError, ProviderError, SearchRequest
from openlens.providers.http import CustomApiSearchProvider


def make_config(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "endpoint": "https://api.example.com/search",
        "api_key": "secret-key",
        "timeout_s": 5,
    }
    config.update(overrides)
    return config


def make_provider(**overrides: Any) -> CustomApiSearchProvider:
    provider = CustomApiSearchProvider()
    provider.configure(make_config(**overrides))
    return provider


class FakeResponse:
    def __init__(
        self,
        payload: Any = None,
        status_code: int = 200,
        error: Exception | None = None,
    ) -> None:
        self._payload = payload
        self.status_code = status_code
        self._error = error

    def raise_for_status(self) -> None:
        if self._error is not None:
            raise self._error

    def json(self) -> Any:
        return self._payload


def install_fake_httpx(
    monkeypatch: pytest.MonkeyPatch,
    *,
    get_response: FakeResponse | None = None,
    post_response: FakeResponse | None = None,
    get_error: Exception | None = None,
    post_error: Exception | None = None,
) -> dict[str, Any]:
    """Inject a fake ``httpx`` module; return the captured-call record."""
    calls: dict[str, Any] = {}

    def fake_get(url: str, **kwargs: Any) -> FakeResponse:
        calls["get"] = {"url": url, **kwargs}
        if get_error is not None:
            raise get_error
        assert get_response is not None
        return get_response

    def fake_post(url: str, **kwargs: Any) -> FakeResponse:
        calls["post"] = {"url": url, **kwargs}
        if post_error is not None:
            raise post_error
        assert post_response is not None
        return post_response

    fake_module = types.ModuleType("httpx")
    fake_module.get = fake_get  # type: ignore[attr-defined]
    fake_module.post = fake_post  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "httpx", fake_module)
    return calls


PAYLOAD = {
    "results": [
        {"id": "a", "title": "Alpha", "text": "first hit", "link": "http://a"},
        {"id": "b", "title": "Beta", "text": "second hit", "link": "http://b"},
    ]
}
FIELDS = {"id": "id", "title": "title", "snippet": "text", "url": "link"}


def test_get_sends_params_and_header_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = make_provider(fields=FIELDS)
    calls = install_fake_httpx(monkeypatch, get_response=FakeResponse(PAYLOAD))
    results = provider.search(SearchRequest(query="cats", limit=3, offset=1))

    assert "get" in calls
    assert "post" not in calls
    assert calls["get"]["url"] == "https://api.example.com/search"
    assert calls["get"]["params"] == {"q": "cats", "count": 3, "offset": 1}
    assert calls["get"]["headers"] == {
        "Accept": "application/json",
        "X-Api-Key": "secret-key",
    }
    assert calls["get"]["timeout"] == 5.0
    assert [r.id for r in results] == ["a", "b"]
    assert all(r.provider == "http" for r in results)
    assert [r.original_score for r in results] == [1.0, 0.5]


def test_get_custom_param_names(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = make_provider(
        fields=FIELDS, params={"query": "search", "limit": "n", "offset": "skip"}
    )
    calls = install_fake_httpx(monkeypatch, get_response=FakeResponse(PAYLOAD))
    provider.search(SearchRequest(query="dogs", limit=2, offset=4))
    assert calls["get"]["params"] == {"search": "dogs", "n": 2, "skip": 4}


def test_post_sends_fixed_json_body(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = make_provider(
        fields=FIELDS,
        method="POST",
        body={"query": "q", "limit": "size"},
    )
    calls = install_fake_httpx(monkeypatch, post_response=FakeResponse(PAYLOAD))
    results = provider.search(SearchRequest(query="cats", limit=3, offset=1))

    assert "post" in calls
    assert "get" not in calls
    assert calls["post"]["url"] == "https://api.example.com/search"
    assert calls["post"]["json"] == {"q": "cats", "size": 3}
    assert calls["post"]["params"] == {}
    assert calls["post"]["headers"]["X-Api-Key"] == "secret-key"
    assert [r.id for r in results] == ["a", "b"]


def test_auth_in_query_get(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = make_provider(fields=FIELDS, auth_in="query", auth_name="api_key")
    calls = install_fake_httpx(monkeypatch, get_response=FakeResponse(PAYLOAD))
    provider.search(SearchRequest(query="cats"))
    assert calls["get"]["params"]["api_key"] == "secret-key"
    assert calls["get"]["params"]["q"] == "cats"
    assert "api_key" not in calls["get"]["headers"]
    assert calls["get"]["headers"] == {"Accept": "application/json"}


def test_auth_in_query_post(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = make_provider(
        fields=FIELDS, method="POST", auth_in="query", auth_name="token"
    )
    calls = install_fake_httpx(monkeypatch, post_response=FakeResponse(PAYLOAD))
    provider.search(SearchRequest(query="cats"))
    assert calls["post"]["params"] == {"token": "secret-key"}
    assert "token" not in calls["post"]["headers"]
    assert calls["post"]["json"] == {"q": "cats", "count": 10, "offset": 0}


def test_dotted_path_extraction_via_fetch_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = make_provider(
        results_path="data.hits",
        fields={"id": "key", "title": "head.t", "snippet": "body", "url": "href"},
        score={"field": "meta.relevance"},
    )
    foreign = {
        "data": {
            "hits": [
                {
                    "key": "1",
                    "head": {"t": "Title"},
                    "body": "Snippet",
                    "href": "http://x",
                    "meta": {"relevance": 0.7},
                },
                {"key": "missing-score", "head": {"t": "T"}, "body": "S"},
                {"head": {"t": "no-id"}, "body": "S"},
            ]
        }
    }
    monkeypatch.setattr(provider, "_fetch", lambda request: foreign)
    results = provider.search(SearchRequest(query="x", mode="web"))
    assert len(results) == 1
    hit = results[0]
    assert (hit.id, hit.title, hit.snippet, hit.url) == (
        "1",
        "Title",
        "Snippet",
        "http://x",
    )
    assert hit.original_score == hit.final_score == 0.7
    assert hit.provider == "http"
    assert hit.metadata == {"engine": "http", "request_mode": "web"}


@pytest.mark.parametrize(
    "overrides",
    [
        {"endpoint": ""},
        {"endpoint": "   "},
        {"endpoint": 123},
        {"api_key": ""},
        {"api_key": 123},
        {"method": "PUT"},
        {"method": ""},
        {"method": 123},
        {"auth_in": "cookie"},
        {"auth_in": ""},
        {"auth_name": ""},
        {"auth_name": "  "},
        {"params": "q"},
        {"params": {}},
        {"params": {"bogus": "q"}},
        {"params": {"query": ""}},
        {"body": {"limit": 5}},
        {"results_path": ""},
        {"results_path": 42},
        {"fields": {"title": "t", "snippet": "s"}},
        {"fields": {"id": "", "title": "t", "snippet": "s"}},
        {"fields": {"id": "i", "title": "t", "snippet": "s", "bogus": "x"}},
        {"fields": {"id": "i", "title": "t", "snippet": "s", "url": ""}},
        {"fields": ["id"]},
        {"score": "bm25"},
        {"score": {}},
        {"score": {"field": ""}},
        {"score": {"field": "s", "extra": 1}},
        {"score": 42},
        {"timeout_s": -1},
    ],
)
def test_bad_config_rejected(overrides: dict[str, Any]) -> None:
    provider = CustomApiSearchProvider()
    with pytest.raises(ConfigError):
        provider.configure(make_config(**overrides))


def test_method_case_insensitive() -> None:
    provider = make_provider(method="post")
    assert provider.method == "POST"


def test_non_object_payload_raises_provider_error() -> None:
    provider = make_provider()
    with pytest.raises(ProviderError):
        provider.parse_payload([1, 2, 3], SearchRequest(query="x"))


def test_search_wraps_parse_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = make_provider()

    def boom(_request: SearchRequest) -> Any:
        return {"results": []}

    monkeypatch.setattr(provider, "_fetch", boom)

    def bad_parse(_payload: Any, _request: SearchRequest) -> Any:
        raise ValueError("mapper boom")

    monkeypatch.setattr(provider, "parse_payload", bad_parse)
    with pytest.raises(ProviderError):
        provider.search(SearchRequest(query="x"))


class FakeTimeout(Exception):
    pass


class ConnectTimeout(FakeTimeout):
    pass


class FakeStatusError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"upstream HTTP {status_code}")
        self.response = types.SimpleNamespace(status_code=status_code)


def test_inherited_timeout_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = make_provider(fields=FIELDS)
    install_fake_httpx(monkeypatch, get_error=ConnectTimeout("timed out"))
    with pytest.raises(ProviderError) as exc_info:
        provider.search(SearchRequest(query="x"))
    assert exc_info.value.code == ProviderError.TIMEOUT
    assert exc_info.value.provider_name == "http"
    assert exc_info.value.retryable is True


def test_inherited_auth_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = make_provider(fields=FIELDS)
    install_fake_httpx(monkeypatch, get_error=FakeStatusError(401))
    with pytest.raises(ProviderError) as exc_info:
        provider.search(SearchRequest(query="x"))
    assert exc_info.value.code == ProviderError.AUTH
    assert exc_info.value.retryable is False


def test_inherited_rate_limit_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = make_provider(fields=FIELDS)
    install_fake_httpx(monkeypatch, get_error=FakeStatusError(429))
    with pytest.raises(ProviderError) as exc_info:
        provider.search(SearchRequest(query="x"))
    assert exc_info.value.code == ProviderError.RATE_LIMIT
    assert exc_info.value.retryable is True


def test_inherited_generic_error_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = make_provider(fields=FIELDS)
    install_fake_httpx(monkeypatch, get_error=RuntimeError("connection reset"))
    with pytest.raises(ProviderError) as exc_info:
        provider.search(SearchRequest(query="x"))
    assert exc_info.value.code == ProviderError.UNAVAILABLE
    assert exc_info.value.retryable is True


def test_health_check_offline_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = CustomApiSearchProvider()
    assert provider.health_check() is False  # unconfigured

    provider = make_provider(fields=FIELDS)
    install_fake_httpx(monkeypatch, get_error=RuntimeError("no network"))
    assert provider.health_check() is False  # never raises

    calls = install_fake_httpx(
        monkeypatch, get_response=FakeResponse({}, status_code=200)
    )
    assert provider.health_check() is True
    assert calls["get"]["headers"]["X-Api-Key"] == "secret-key"


def test_registered_as_type_http() -> None:
    from openlens.providers import registry

    registry.register_builtin_providers()
    assert registry.get("http") is CustomApiSearchProvider

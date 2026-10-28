"""Offline tests for the neutral OpenAI-compatible model backend.

Loopback stub server only (skips when loopback bind unavailable,
matching tests/unit/test_model_local_client.py); no external network
or keys. The stub records the last request so tests assert the
OpenAI-compatible wire shape (path, bearer auth, model, messages).
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from openlens.core.interfaces import ConfigError, ProviderError
from openlens.models.manager import ModelManager
from openlens.models.openai_compat import OpenAICompatModelProvider
from openlens.models.registry import create, register_builtin_models


def _loopback_server(handler: type[BaseHTTPRequestHandler]) -> ThreadingHTTPServer:
    try:
        return ThreadingHTTPServer(("127.0.0.1", 0), handler)
    except OSError as exc:
        pytest.skip(f"loopback bind unavailable in this environment: {exc}")


class _Handler(BaseHTTPRequestHandler):
    last: dict[str, Any] = {}

    def log_message(self, *args: object) -> None:
        pass

    def _send(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: object) -> None:
        self._send(status, json.dumps(payload).encode())

    def _record(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {}
        type(self).last = {
            "path": self.path,
            "auth": self.headers.get("Authorization"),
            "body": body,
        }
        return body

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler name
        if self.path.endswith("/models"):
            self._send_json(200, {"data": [{"id": "muse-spark-1.3"}]})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler name
        body = self._record()
        for code in (401, 403, 429, 500):
            if f"/status/{code}" in self.path:
                self._send_json(code, {"error": "upstream failure"})
                return
        if self.path.endswith("/chat/completions"):
            messages = body.get("messages") or []
            text = " ".join(
                str(m.get("content", "")) for m in messages if isinstance(m, dict)
            )
            self._send_json(
                200,
                {
                    "choices": [
                        {"message": {"role": "assistant", "content": f"echo:{text}"}}
                    ]
                },
            )
        elif self.path.endswith("/embeddings"):
            inputs = body.get("input") or []
            self._send_json(
                200,
                {"data": [{"embedding": [float(len(t)), 0.5]} for t in inputs]},
            )
        else:
            self._send_json(404, {"error": "not found"})


@pytest.fixture()
def stub_base() -> Any:
    server = _loopback_server(_Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


_TEST_KEY = "test-key"


def _provider(base_url: str, **overrides: Any) -> OpenAICompatModelProvider:
    provider = OpenAICompatModelProvider()
    config: dict[str, Any] = {
        "base_url": base_url,
        "api_key": _TEST_KEY,
        "timeout_s": 5,
    }
    config.update(overrides)
    provider.configure(config)
    return provider


def _expected_auth() -> str:
    return "Bearer " + _TEST_KEY


def test_configure_validation() -> None:
    with pytest.raises(ConfigError):
        OpenAICompatModelProvider().configure({})
    with pytest.raises(ConfigError):
        OpenAICompatModelProvider().configure({"base_url": "  ", "api_key": "k"})
    with pytest.raises(ConfigError):
        OpenAICompatModelProvider().configure({"base_url": "http://x", "api_key": ""})
    provider = _provider("http://127.0.0.1:9/")
    assert provider.name == "openai_compat"
    assert provider.base_url == "http://127.0.0.1:9"  # trailing slash stripped
    assert provider.model == "muse-spark-1.3"  # default model


def test_complete_uses_openai_wire_shape(stub_base: str) -> None:
    provider = _provider(stub_base, model="muse-spark-1.3")
    assert provider.complete("hello") == "echo:hello"
    last = _Handler.last
    assert last["path"] == "/chat/completions"
    assert last["auth"] == "Bearer test-key"
    assert last["body"]["model"] == "muse-spark-1.3"
    assert last["body"]["messages"] == [{"role": "user", "content": "hello"}]


def test_embed_and_classify(stub_base: str) -> None:
    provider = _provider(stub_base)
    assert provider.embed(["ab", "c"]) == [[2.0, 0.5], [1.0, 0.5]]
    assert _Handler.last["path"] == "/embeddings"
    assert provider.classify("I love sports", ["sports", "news"]) == "sports"
    assert provider.health_check() is True


def test_blank_prompt_rejected(stub_base: str) -> None:
    provider = _provider(stub_base)
    with pytest.raises(ProviderError) as exc_info:
        provider.complete("   ")
    assert exc_info.value.code == ProviderError.INVALID_REQUEST


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (401, ProviderError.AUTH, False),
        (403, ProviderError.AUTH, False),
        (429, ProviderError.RATE_LIMIT, True),
        (500, ProviderError.UNAVAILABLE, True),
    ],
)
def test_error_code_mapping(
    stub_base: str, status: int, code: str, retryable: bool
) -> None:
    provider = _provider(f"{stub_base}/status/{status}")
    with pytest.raises(ProviderError) as exc_info:
        provider.complete("hello")
    assert exc_info.value.code == code
    assert exc_info.value.retryable is retryable
    assert exc_info.value.provider_name == "openai_compat"


def test_errors_do_not_leak_key() -> None:
    secret = "sentinel-openai-key-456"  # noqa: S105 - intentional non-secret fixture value
    provider = _provider("http://127.0.0.1:9", timeout_s=1.0, api_key=secret)
    with pytest.raises(ProviderError) as exc_info:
        provider.complete("hello")
    assert secret not in str(exc_info.value)
    assert secret not in repr(exc_info.value)
    assert provider.health_check() is False


class _FakeResponse:
    def __init__(self, payload: object = None, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._payload


def _install_fake_httpx(monkeypatch: pytest.MonkeyPatch, **kw: Any) -> dict[str, Any]:
    import sys
    import types

    calls: dict[str, Any] = {}

    def fake_post(url: str, **kwargs: Any) -> Any:
        calls["post"] = {"url": url, **kwargs}
        if kw.get("post_error") is not None:
            raise kw["post_error"]
        return kw.get("post_response")

    def fake_get(url: str, **kwargs: Any) -> Any:
        calls["get"] = {"url": url, **kwargs}
        if kw.get("get_error") is not None:
            raise kw["get_error"]
        return kw.get("get_response", _FakeResponse({}, 200))

    module = types.ModuleType("httpx")
    module.post = fake_post  # type: ignore[attr-defined]
    module.get = fake_get  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "httpx", module)
    return calls


def test_fake_transport_complete_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _provider("http://llm.test/v1", timeout_s=3)
    calls = _install_fake_httpx(
        monkeypatch,
        post_response=_FakeResponse({"choices": [{"message": {"content": "ready"}}]}),
    )
    assert provider.complete("Reply with the single word: ready") == "ready"
    post = calls["post"]
    assert post["url"] == "http://llm.test/v1/chat/completions"
    assert post["timeout"] == 3.0
    assert post["json"]["model"] == "muse-spark-1.3"
    assert post["json"]["messages"] == [
        {"role": "user", "content": "Reply with the single word: ready"}
    ]
    assert post["headers"]["Authorization"] == "Bearer test-key"


def test_fake_transport_embed_and_auth_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider("http://llm.test/v1")
    _install_fake_httpx(
        monkeypatch,
        post_response=_FakeResponse(
            {"data": [{"embedding": [0.1, 0.2]}, {"embedding": [0.3, 0.4]}]}
        ),
    )
    assert provider.embed(["a", "bb"]) == [[0.1, 0.2], [0.3, 0.4]]

    _install_fake_httpx(monkeypatch, post_response=_FakeResponse({}, 401))
    with pytest.raises(ProviderError) as exc_info:
        provider.complete("hello")
    assert exc_info.value.code == ProviderError.AUTH
    assert exc_info.value.retryable is False


def test_registered_as_builtin_and_creatable(stub_base: str) -> None:
    register_builtin_models()
    provider = create(
        "openai_compat",
        {"base_url": stub_base, "api_key": "k", "model": "muse-spark-1.3"},
    )
    assert isinstance(provider, OpenAICompatModelProvider)
    manager = ModelManager.from_config(
        {
            "models": {
                "primary": "spark",
                "spark": {
                    "enabled": True,
                    "type": "openai_compat",
                    "base_url": stub_base,
                    "api_key": "k",
                    "model": "muse-spark-1.3",
                },
            }
        }
    )
    assert manager.primary == "spark"
    assert manager.complete("ping") == "echo:ping"

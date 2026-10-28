"""Offline tests for the live LocalModelProvider HTTP client.

Loopback stub server only (skips when loopback bind unavailable,
matching tests/unit/test_crawler.py); no external network or keys.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from openlens.core.interfaces import ConfigError, ProviderError
from openlens.models.backends import (
    ClaudeModelProvider,
    LocalModelProvider,
    MuseModelProvider,
)


def _loopback_server(handler: type[BaseHTTPRequestHandler]) -> ThreadingHTTPServer:
    try:
        return ThreadingHTTPServer(("127.0.0.1", 0), handler)
    except OSError as exc:
        pytest.skip(f"loopback bind unavailable in this environment: {exc}")


class _Handler(BaseHTTPRequestHandler):
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

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler name
        if "/hang" in self.path:
            time.sleep(2.0)
        if "/dead-health" in self.path:
            self._send_json(500, {"error": "down"})
            return
        self._send_json(200, {"status": "ok"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler name
        path = self.path
        if "/hang" in path:
            time.sleep(2.0)
            self._send_json(200, {"text": "slow"})
            return
        for code in (401, 403, 429, 500, 503):
            if f"/status/{code}" in path:
                self._send_json(code, {"error": "upstream failure"})
                return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body: dict[str, Any] = json.loads(raw or b"{}")
        except ValueError:
            body = {}
        if "prompt" in body:
            self._send_json(200, {"text": f"echo:{body['prompt']}"})
        elif "labels" in body:
            labels = body.get("labels") or ["unknown"]
            text = str(body.get("text", "")).lower()
            picked = next(
                (label for label in labels if label.lower() in text), labels[0]
            )
            self._send_json(200, {"label": picked})
        elif "texts" in body or "input" in body:
            texts = body.get("texts") or body.get("input") or []
            self._send_json(
                200,
                {"embeddings": [[float(len(t)), 0.5] for t in texts]},
            )
        else:
            self._send_json(200, {"text": "ok"})


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


def _provider(endpoint: str, **overrides: Any) -> LocalModelProvider:
    provider = LocalModelProvider()
    config: dict[str, Any] = {"endpoint": endpoint, "timeout_s": 5}
    config.update(overrides)
    provider.configure(config)
    return provider


def test_endpoint_validation() -> None:
    with pytest.raises(ConfigError):
        LocalModelProvider().configure({})
    with pytest.raises(ConfigError):
        LocalModelProvider().configure({"endpoint": ""})
    with pytest.raises(ConfigError):
        LocalModelProvider().configure({"endpoint": "   "})
    provider = _provider("http://127.0.0.1:9")
    assert provider.name == "local"
    assert provider.model == "default"
    provider2 = LocalModelProvider()
    provider2.configure({"endpoint": "http://127.0.0.1:9", "model": "m1"})
    assert provider2.model == "m1"


def test_complete_embed_classify_happy_path(stub_base: str) -> None:
    provider = _provider(stub_base)
    assert provider.complete("hello") == "echo:hello"
    assert provider.embed(["ab", "c"]) == [[2.0, 0.5], [1.0, 0.5]]
    assert provider.classify("I love sports", ["sports", "news"]) == "sports"
    assert provider.health_check() is True


def test_timeout_enforced(stub_base: str) -> None:
    provider = _provider(f"{stub_base}/hang", timeout_s=0.2)
    start = time.monotonic()
    with pytest.raises(ProviderError) as exc_info:
        provider.complete("hello")
    elapsed = time.monotonic() - start
    assert exc_info.value.code == ProviderError.TIMEOUT
    assert exc_info.value.retryable is True
    assert exc_info.value.provider_name == "local"
    assert elapsed < 2 * 0.2 + 0.5  # native httpx timeout, well under hang
    assert elapsed < 1.0


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (401, ProviderError.AUTH, False),
        (403, ProviderError.AUTH, False),
        (429, ProviderError.RATE_LIMIT, True),
        (500, ProviderError.UNAVAILABLE, True),
        (503, ProviderError.UNAVAILABLE, True),
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
    assert exc_info.value.provider_name == "local"


def test_connection_error_maps_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider("http://127.0.0.1:9", timeout_s=1.0)
    _install_fake_httpx(monkeypatch, post_error=ConnectionError("connection refused"))
    with pytest.raises(ProviderError) as exc_info:
        provider.complete("hello")
    assert exc_info.value.code == ProviderError.UNAVAILABLE
    assert exc_info.value.retryable is True


def test_health_check_false_on_dead_endpoint() -> None:
    provider = _provider("http://127.0.0.1:9", timeout_s=1.0)
    assert provider.health_check() is False
    assert LocalModelProvider().health_check() is False


def test_errors_do_not_leak_secret_or_endpoint_credential() -> None:
    secret = "sentinel-local-secret-123"  # noqa: S105 - intentional non-secret fixture value
    provider = _provider("http://127.0.0.1:9", timeout_s=1.0, api_key=secret)
    with pytest.raises(ProviderError) as exc_info:
        provider.complete("hello")
    assert secret not in str(exc_info.value)
    assert secret not in repr(exc_info.value)


class _FakeResponse:
    def __init__(self, payload: object = None, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._payload


class ReadTimeout(Exception):
    pass


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


def test_fake_transport_happy_and_timeout_arg(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _provider("http://local.test/v1", timeout_s=3)
    calls = _install_fake_httpx(
        monkeypatch, post_response=_FakeResponse({"text": "hi there"})
    )
    assert provider.complete("hello") == "hi there"
    assert calls["post"]["timeout"] == 3.0
    assert calls["post"]["json"]["prompt"] == "hello"
    assert calls["post"]["json"]["model"] == "default"


def test_fake_transport_timeout_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _provider("http://local.test/v1", timeout_s=0.2)
    _install_fake_httpx(monkeypatch, post_error=ReadTimeout("slow"))
    start = time.monotonic()
    with pytest.raises(ProviderError) as exc_info:
        provider.complete("hello")
    assert time.monotonic() - start < 2 * 0.2 + 0.5
    assert exc_info.value.code == ProviderError.TIMEOUT
    assert exc_info.value.retryable is True


def test_fake_transport_status_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _provider("http://local.test/v1")
    _install_fake_httpx(monkeypatch, post_response=_FakeResponse({}, 429))
    with pytest.raises(ProviderError) as exc_info:
        provider.embed(["x"])
    assert exc_info.value.code == ProviderError.RATE_LIMIT


def test_claude_muse_stubs_unchanged() -> None:
    claude = ClaudeModelProvider()
    claude.configure({"api_key": "test-key"})
    assert claude.health_check() is False
    with pytest.raises(ProviderError) as exc_info:
        claude.complete("hi")
    assert exc_info.value.code == ProviderError.UNAVAILABLE

    muse = MuseModelProvider()
    muse.configure({"api_key": "test-key"})
    assert muse.health_check() is False
    with pytest.raises(ProviderError) as exc_info:
        muse.embed(["hi"])
    assert exc_info.value.code == ProviderError.UNAVAILABLE

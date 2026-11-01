"""Body-cap regression tests: header fast-reject + streamed counting."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse
from starlette.types import Message, Receive, Scope, Send

from openlens.api.body_limit import BodyLimitMiddleware, max_body_bytes
from openlens.main import create_app

from tests.unit.test_api import FakeEngine


def _scope(content_length: int | None) -> Scope:
    headers = (
        [(b"content-length", str(content_length).encode("latin-1"))]
        if content_length is not None
        else []
    )
    return {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/",
        "query_string": b"",
        "headers": headers,
    }


def _receive(*bodies: bytes, more_body: bool = False) -> Receive:
    messages: list[Message] = [
        {"type": "http.request", "body": body, "more_body": more_body}
        for body in bodies
    ]
    messages.append({"type": "http.disconnect"})
    iterator = iter(messages)

    async def receive() -> Message:
        return next(iterator)

    return receive


def _ok_app(seen: list[bytes]):
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        body = b""
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        seen.append(body)
        await JSONResponse({"consumed": len(body)})(scope, receive, send)

    return app


async def _run(app, scope: Scope, receive: Receive) -> list[Message]:
    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return sent


def _status(sent: list[Message]) -> int:
    for message in sent:
        if message["type"] == "http.response.start":
            return int(message["status"])
    raise AssertionError("no response start sent")


def test_max_body_bytes_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENLENS_MAX_BODY_BYTES", "2048")
    assert max_body_bytes() == 2048
    monkeypatch.setenv("OPENLENS_MAX_BODY_BYTES", "0")
    assert max_body_bytes() == 1024 * 1024
    monkeypatch.setenv("OPENLENS_MAX_BODY_BYTES", "huge")
    assert max_body_bytes() == 1024 * 1024
    monkeypatch.delenv("OPENLENS_MAX_BODY_BYTES", raising=False)
    assert max_body_bytes() == 1024 * 1024


def test_declared_over_cap_rejected_before_read() -> None:
    seen: list[bytes] = []
    sent = asyncio.run(
        _run(
            BodyLimitMiddleware(_ok_app(seen), max_bytes=10),
            _scope(11),
            _receive(b"0123456789A"),
        )
    )
    assert _status(sent) == 413
    assert seen == []


def test_chunked_over_cap_short_circuits() -> None:
    seen: list[bytes] = []
    sent = asyncio.run(
        _run(
            BodyLimitMiddleware(_ok_app(seen), max_bytes=10),
            _scope(None),
            _receive(b"01234", b"56789A", more_body=True),
        )
    )
    assert _status(sent) == 413
    assert seen == []


def test_under_cap_passes_through_intact() -> None:
    seen: list[bytes] = []
    sent = asyncio.run(
        _run(
            BodyLimitMiddleware(_ok_app(seen), max_bytes=10),
            _scope(5),
            _receive(b"hello"),
        )
    )
    assert _status(sent) == 200
    assert seen == [b"hello"]


def test_non_http_scope_passes_through() -> None:
    called: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        called.append(scope["type"])

    asyncio.run(
        _run(
            BodyLimitMiddleware(app, max_bytes=1),
            {"type": "websocket"},
            _receive(),
        )
    )
    assert called == ["websocket"]


def _client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    for var in ("OPENLENS_MAX_BODY_BYTES",):
        monkeypatch.delenv(var, raising=False)
    return TestClient(create_app(engine=FakeEngine()))


def test_http_search_rejected_over_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch)
    monkeypatch.setenv("OPENLENS_MAX_BODY_BYTES", "10")
    response = client.post("/api/v1/search", json={"query": "a"})
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


def test_http_search_passes_under_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch)
    monkeypatch.setenv("OPENLENS_MAX_BODY_BYTES", str(1024 * 1024))
    assert client.post("/api/v1/search", json={"query": "a"}).status_code == 200

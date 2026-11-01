"""Global request-body size cap (standard library + starlette only).

Two layers: bodies declaring more than the cap via ``Content-Length``
are refused with 413 before a byte is consumed; chunked (or lying)
bodies are counted as they stream through a wrapped ``receive``
channel, and the first chunk that crosses the cap short-circuits to
413 without invoking downstream handlers. Pure ASGI (not a
``@app.middleware("http")`` decorator) so the wrapped channel is what
downstream reads — no double buffering, no re-injection tricks.

Configuration is environment-driven (``OPENLENS_MAX_BODY_BYTES``,
default 1048576 = 1 MiB). Legitimate traffic is KBs (queries are
capped at 1000 chars, chat histories at 10 turns); the largest
legitimate body is an admin seed paste of thousands of URLs, which
fits comfortably under 1 MiB.
"""

from __future__ import annotations

import os
from typing import Any, Awaitable, Callable

from starlette.responses import JSONResponse
from starlette.types import Message, Receive, Scope, Send

from openlens.api.errors import error_body

MAX_BODY_BYTES_ENV = "OPENLENS_MAX_BODY_BYTES"

#: Default cap: 1 MiB.
DEFAULT_MAX_BODY_BYTES = 1024 * 1024


def max_body_bytes() -> int:
    """Resolve the cap: positive ints win, everything else is the default."""
    try:
        value = int(os.environ.get(MAX_BODY_BYTES_ENV, "").strip())
    except (TypeError, ValueError):
        return DEFAULT_MAX_BODY_BYTES
    return value if value > 0 else DEFAULT_MAX_BODY_BYTES


def _content_length(scope: Scope) -> int | None:
    """Declared body size, or None when absent/unparseable (fail open)."""
    for name, value in scope.get("headers", []):
        if name.lower() == b"content-length":
            try:
                return int(value.decode("latin-1").strip())
            except ValueError:
                return None
    return None


def _too_large(limit: int) -> JSONResponse:
    return JSONResponse(
        status_code=413,
        content=error_body("payload_too_large", f"request body exceeds {limit} bytes"),
    )


class _BodyTooLarge(Exception):
    """Internal signal: a streamed chunk crossed the cap."""


class BodyLimitMiddleware:
    """Reject oversized request bodies with 413 (see module docstring)."""

    def __init__(
        self,
        app: Callable[[Scope, Receive, Send], Awaitable[Any]],
        max_bytes: int | None = None,
    ) -> None:
        self.app = app
        self._max_bytes = max_bytes

    def _limit(self) -> int:
        if isinstance(self._max_bytes, int) and not isinstance(self._max_bytes, bool):
            return self._max_bytes if self._max_bytes > 0 else DEFAULT_MAX_BODY_BYTES
        return max_body_bytes()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        limit = self._limit()
        declared = _content_length(scope)
        if declared is not None and declared > limit:
            await _too_large(limit)(scope, receive, send)
            return
        consumed = 0

        async def capped_receive() -> Message:
            nonlocal consumed
            message = await receive()
            if message.get("type") == "http.request":
                chunk = message.get("body", b"")
                consumed += len(chunk) if isinstance(chunk, bytes) else 0
                if consumed > limit:
                    raise _BodyTooLarge()
            return message

        try:
            await self.app(scope, capped_receive, send)
        except _BodyTooLarge:
            await _too_large(limit)(scope, receive, send)

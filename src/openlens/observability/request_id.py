"""Per-request id resolution for observability.

The id travels in the ``X-Request-Id`` header: a client-supplied value is
echoed back unchanged, otherwise a fresh ``uuid4`` hex id is generated.
The resolved id is recorded on ``SearchContext.request_id``, surfaced in
``diagnostics()["request_id"]``, and (once the integrate step lands) echoed
in the ``X-Request-Id`` response header.
"""

from __future__ import annotations

import uuid
from typing import Optional

from fastapi import Request

#: Header carrying the client-supplied request id (request and response).
REQUEST_ID_HEADER = "X-Request-Id"


def new_request_id() -> str:
    """Generate a fresh request id (uuid4 hex, no dashes)."""
    return uuid.uuid4().hex


def resolve_request_id(supplied: Optional[str]) -> str:
    """Return the client-supplied id when non-blank, else a fresh one."""
    if supplied is not None and supplied.strip():
        return supplied.strip()
    return new_request_id()


def get_request_id(request: Request) -> str:
    """FastAPI dependency: echo ``X-Request-Id`` or generate one."""
    return resolve_request_id(request.headers.get(REQUEST_ID_HEADER))

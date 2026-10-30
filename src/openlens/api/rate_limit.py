"""Per-IP fixed-window rate limiting (standard library only).

Scopes protect the public, unauthenticated surface: expensive retrieval
(`search`, `suggest`), writable spam targets (`feedback`), the admin
mutation plane (`admin`), and live-socket handshakes (`ws`). Reads that
dashboards poll (status/settings/workers) stay unlimited.

Configuration is environment-driven so tests and deploys tune without
rebuilds (``<limit>/<window_seconds>`` per scope, all optional):

- ``OPENLENS_RATE_LIMIT_ENABLED`` — master switch, default ``true``.
  Set to ``false`` to disable (local profiling, trusted networks).
- ``OPENLENS_RATE_LIMIT_SEARCH`` — default ``120/60``.
- ``OPENLENS_RATE_LIMIT_SUGGEST`` — default ``120/60``.
- ``OPENLENS_RATE_LIMIT_FEEDBACK`` — default ``20/60``.
- ``OPENLENS_RATE_LIMIT_ADMIN`` — default ``60/60``.
- ``OPENLENS_RATE_LIMIT_WS`` — default ``30/60``.

State is in-memory per backend replica (one replica in the local
compose stack). Limits are approximate under concurrency and reset on
restart; that is acceptable for abuse throttling, not billing.
"""

from __future__ import annotations

import math
import os
import threading
import time
from typing import Callable

RATE_LIMIT_ENABLED_ENV = "OPENLENS_RATE_LIMIT_ENABLED"

_DEFAULTS: dict[str, tuple[int, float]] = {
    "search": (120, 60.0),
    "suggest": (120, 60.0),
    "feedback": (20, 60.0),
    "admin": (60, 60.0),
    "ws": (30, 60.0),
}


def _parse_limit(raw: str | None, default: tuple[int, float]) -> tuple[int, float]:
    """Parse ``<limit>/<window_seconds>``; fall back to `default`."""
    if not raw:
        return default
    try:
        count_raw, _, window_raw = raw.strip().partition("/")
        count = int(count_raw)
        window = float(window_raw) if window_raw else default[1]
        if count <= 0 or window <= 0:
            return default
        return count, window
    except (ValueError, ArithmeticError):
        return default


def rate_limit_enabled() -> bool:
    """Whether the master switch enables limiting (default on)."""
    return os.environ.get(RATE_LIMIT_ENABLED_ENV, "").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def limit_for(scope: str) -> tuple[int, float]:
    """Resolve ``(max_requests, window_seconds)`` for `scope`."""
    default = _DEFAULTS.get(scope, (60, 60.0))
    return _parse_limit(os.environ.get(f"OPENLENS_RATE_LIMIT_{scope.upper()}"), default)


def scope_for_request(method: str, path: str) -> str | None:
    """Map an HTTP request to a rate-limit scope (None = unlimited)."""
    if method == "POST" and path == "/api/v1/search":
        return "search"
    if method == "GET" and path == "/api/v1/suggest":
        return "suggest"
    if method == "POST" and path == "/api/v1/feedback":
        return "feedback"
    if method in ("POST", "PUT", "DELETE", "PATCH") and (
        path.startswith("/api/v1/frontier/") or path.startswith("/api/v1/admin/")
    ):
        return "admin"
    return None


def client_ip(headers: dict[str, str], peer: str | None) -> str:
    """Best-effort client identity for bucketing (not for auth).

    Prefers edge-set headers (Cloudflare tunnel, nginx) over the TCP
    peer. Headers are client-spoofable on direct connections; that only
    moves the spoofer to a different bucket, which is harmless here.
    """
    lowered = {key.lower(): value for key, value in headers.items()}
    for name in ("cf-connecting-ip", "x-real-ip"):
        value = (lowered.get(name) or "").strip()
        if value:
            return value
    forwarded = (lowered.get("x-forwarded-for") or "").strip()
    if forwarded:
        return forwarded.split(",")[0].strip() or peer or "unknown"
    return peer or "unknown"


class RateLimiter:
    """Thread-safe fixed-window limiter keyed by ``(scope, identity)``."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: dict[tuple[str, str], list[float]] = {}

    def check(self, scope: str, identity: str) -> float | None:
        """Consume one unit; return retry-after seconds when over limit."""
        if not rate_limit_enabled():
            return None
        count, window = limit_for(scope)
        now = self._clock()
        key = (scope, identity)
        with self._lock:
            hits = [hit for hit in self._buckets.get(key, []) if now - hit < window]
            if len(hits) >= count:
                oldest = min(hits)
                return max(0.0, window - (now - oldest))
            hits.append(now)
            # Opportunistic prune keeps memory bounded to one window.
            self._buckets[key] = hits
            if len(self._buckets) > 10000:
                self._buckets = {
                    item: stamps
                    for item, stamps in self._buckets.items()
                    if any(now - hit < window for hit in stamps)
                }
            return None

    def reset(self) -> None:
        """Drop all buckets (tests, operator intervention)."""
        with self._lock:
            self._buckets.clear()


def retry_after_seconds(retry_after: float) -> str:
    """Format a retry delay for the ``Retry-After`` header."""
    return str(max(1, int(math.ceil(retry_after))))

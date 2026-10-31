"""Ephemeral in-memory frontier state backing the dashboard REST endpoints.

This is the default store until a persistent ``FrontierStore`` lands
(storage owner): process-local only, seeded from the app ``indexing:``
config section when available, and resettable in tests via
:func:`reset_frontier_state`. No network, no threads, no persistence.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import time
from typing import Any

from openlens.storage.frontier import DEFAULT_CRAWL_ID, resolve_worker_id


DEFAULT_SETTINGS: dict[str, Any] = {
    "max_pages": 100,
    "max_queued_urls": 1000,
    "crawl_delay_s": 0.5,
    "default_max_depth": 3,
    "respect_robots": True,
}


@dataclass
class FrontierState:
    """Mutable crawl-queue snapshot served by the dashboard endpoints."""

    paused: bool = False
    queue: list[dict[str, Any]] = field(default_factory=list)
    checkpoint: dict[str, Any] = field(
        default_factory=lambda: {
            "phase": "idle",
            "pages_attempted": 0,
            "pages_indexed": 0,
        }
    )
    new_urls: list[dict[str, Any]] = field(default_factory=list)
    settings: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_SETTINGS))


_STATE = FrontierState()


def get_frontier_state() -> FrontierState:
    """Return the process-local frontier singleton."""
    return _STATE


def reset_frontier_state() -> FrontierState:
    """Restore factory defaults (tests only)."""
    reset_workers()
    _STATE.paused = False
    _STATE.queue = []
    _STATE.checkpoint = {"phase": "idle", "pages_attempted": 0, "pages_indexed": 0}
    _STATE.new_urls = []
    _STATE.settings = dict(DEFAULT_SETTINGS)
    return _STATE


def seed_settings_from_config(config: Any) -> None:
    """Best-effort seed of settings from the app ``indexing:`` section."""
    if not isinstance(config, dict):
        return
    indexing = config.get("indexing")
    if not isinstance(indexing, dict):
        return
    for key in DEFAULT_SETTINGS:
        value = indexing.get(key)
        if value is not None:
            _STATE.settings[key] = value


def snapshot() -> dict[str, Any]:
    """Deep copy of the current state for response shaping."""
    return {
        "paused": _STATE.paused,
        "queue": deepcopy(_STATE.queue),
        "checkpoint": deepcopy(_STATE.checkpoint),
        "new_urls": deepcopy(_STATE.new_urls),
        "settings": deepcopy(_STATE.settings),
    }


#: Ephemeral worker liveness registry (single API replica).
#:
#: The persistent per-worker liveness rows live in the frontier store
#: itself (``FrontierStore.register_worker/heartbeat_worker/list_workers``,
#: Postgres-first). The API process holds no store handle — it serves the
#: ephemeral dashboard snapshot — so this registry only tracks workers
#: that heartbeat *this* API replica. It answers "who is crawling right
#: now" for one replica; cross-replica visibility comes from the store's
#: ``workers`` table (lease owner). Entries older than
#: ``WORKER_STALE_AFTER_S`` are treated as gone.
WORKER_STALE_AFTER_S = 240.0

_WORKERS: dict[str, dict[str, Any]] = {}


def heartbeat_worker(
    worker_id: str | None = None,
    *,
    crawl_id: str = DEFAULT_CRAWL_ID,
    claimed: int = 0,
    status: str = "alive",
    role: str = "worker",
    now: float | None = None,
) -> dict[str, Any]:
    """Record a worker heartbeat; return its stored entry (a copy)."""
    resolved = resolve_worker_id(worker_id)
    entry = {
        "worker_id": resolved,
        "crawl_id": crawl_id.strip() or DEFAULT_CRAWL_ID,
        "last_seen": float(now) if now is not None else time.time(),
        "claimed": max(0, int(claimed)),
        "status": (status or "alive").strip() or "alive",
        "role": (role or "worker").strip()[:64] or "worker",
    }
    _WORKERS[resolved] = entry
    return dict(entry)


def list_workers(
    *,
    max_age_s: float = WORKER_STALE_AFTER_S,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """Return live worker entries, newest heartbeat first; prune stale."""
    moment = float(now) if now is not None else time.time()
    live: list[dict[str, Any]] = []
    for worker_id in list(_WORKERS.keys()):
        entry = _WORKERS[worker_id]
        if moment - float(entry["last_seen"]) > max_age_s:
            del _WORKERS[worker_id]
            continue
        live.append(dict(entry))
    live.sort(key=lambda item: float(item["last_seen"]), reverse=True)
    return live


def reset_workers() -> None:
    """Forget every worker heartbeat (tests only)."""
    _WORKERS.clear()

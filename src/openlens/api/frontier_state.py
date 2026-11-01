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
    "allow_domains": [],
    "deny_domains": [],
    "discovery_feeds": [],
    "refresh_after_days": 0,
    "discovery_interval_s": 21600,
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


def sanitize_discovery_summary(value: Any, *, now: float) -> dict[str, Any] | None:
    """Validate a worker discovery summary; None when missing/malformed.

    Accepts the ``{"at", "feeds_checked", "new_urls", "refreshed"}``
    heartbeat fragment: counts coerce to non-negative ints, ``at``
    coerces to a unix-epoch float (defaulting to ``now``). Never raises:
    anything unusable degrades to None.
    """
    if not isinstance(value, dict):
        return None
    try:
        summary: dict[str, Any] = {}
        for key in ("feeds_checked", "new_urls", "refreshed"):
            try:
                summary[key] = max(0, int(value.get(key, 0) or 0))
            except (TypeError, ValueError):
                summary[key] = 0
        raw_at = value.get("at", now)
        try:
            summary["at"] = float(raw_at) if raw_at is not None else float(now)
        except (TypeError, ValueError):
            summary["at"] = float(now)
        return summary
    except Exception:
        return None


def sanitize_stats_summary(value: Any, *, now: float) -> dict[str, Any] | None:
    """Validate cumulative intake counters; None when missing/malformed.

    Accepts the ``{"at", "pages", "tokens", "errors"}`` heartbeat
    fragment: counts coerce to non-negative ints, ``at`` coerces to a
    unix-epoch float (defaulting to ``now``). Never raises: anything
    unusable degrades to None.
    """
    if not isinstance(value, dict):
        return None
    try:
        summary: dict[str, Any] = {}
        for key in ("pages", "tokens", "errors"):
            try:
                summary[key] = max(0, int(value.get(key, 0) or 0))
            except (TypeError, ValueError):
                summary[key] = 0
        raw_at = value.get("at", now)
        try:
            summary["at"] = float(raw_at) if raw_at is not None else float(now)
        except (TypeError, ValueError):
            summary["at"] = float(now)
        return summary
    except Exception:
        return None


def _throughput_rates(
    current: dict[str, Any] | None, previous: dict[str, Any] | None
) -> tuple[float | None, float | None]:
    """Per-minute pages/tokens rates between two cumulative samples.

    None when samples are missing, out of order, or share a timestamp:
    callers surface cumulative totals instead of a misleading zero.
    """
    try:
        if not isinstance(current, dict) or not isinstance(previous, dict):
            return None, None
        dt = float(current.get("at", 0.0)) - float(previous.get("at", 0.0))
        if dt <= 0:
            return None, None
        pages = (
            (int(current.get("pages", 0)) - int(previous.get("pages", 0))) / dt * 60.0
        )
        tokens = (
            (int(current.get("tokens", 0)) - int(previous.get("tokens", 0))) / dt * 60.0
        )
        return max(0.0, pages), max(0.0, tokens)
    except (TypeError, ValueError):
        return None, None


def heartbeat_worker(
    worker_id: str | None = None,
    *,
    crawl_id: str = DEFAULT_CRAWL_ID,
    claimed: int = 0,
    status: str = "alive",
    role: str = "worker",
    discovery: Any = None,
    stats: Any = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Record a worker heartbeat; return its stored entry (a copy)."""
    resolved = resolve_worker_id(worker_id)
    moment = float(now) if now is not None else time.time()
    latest = sanitize_stats_summary(stats, now=moment)
    # Keep the previous cumulative sample for rate computation: only
    # shift forward on a newer timestamp so reordered/delayed beats
    # never invert the rate.
    previous_entry = _WORKERS.get(resolved)
    previous_stats = (
        previous_entry.get("stats") if isinstance(previous_entry, dict) else None
    )
    if not isinstance(previous_stats, dict):
        previous_stats = (
            previous_entry.get("stats_prev")
            if isinstance(previous_entry, dict)
            else None
        )
    stats_prev: dict[str, Any] | None = None
    if latest is None:
        # A stat-less beat carries no new sample: keep the last known
        # one as the baseline for the next sample instead of dropping
        # rate history for one missed fragment.
        if isinstance(previous_stats, dict):
            stats_prev = dict(previous_stats)
    elif isinstance(previous_stats, dict):
        try:
            if float(latest.get("at", 0.0)) > float(previous_stats.get("at", 0.0)):
                stats_prev = dict(previous_stats)
        except (TypeError, ValueError):
            stats_prev = None
    entry = {
        "worker_id": resolved,
        "crawl_id": crawl_id.strip() or DEFAULT_CRAWL_ID,
        "last_seen": moment,
        "claimed": max(0, int(claimed)),
        "status": (status or "alive").strip() or "alive",
        "role": (role or "worker").strip()[:64] or "worker",
        "discovery": sanitize_discovery_summary(discovery, now=moment),
        "stats": latest,
        "stats_prev": stats_prev,
    }
    _WORKERS[resolved] = entry
    stored = dict(entry)
    fragment = stored.get("discovery")
    if isinstance(fragment, dict):
        stored["discovery"] = dict(fragment)
    summary = stored.get("stats")
    if isinstance(summary, dict):
        stored["stats"] = dict(summary)
    stored.pop("stats_prev", None)
    return stored


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
        copied = dict(entry)
        fragment = copied.get("discovery")
        if isinstance(fragment, dict):
            copied["discovery"] = dict(fragment)
        summary = copied.get("stats")
        if isinstance(summary, dict):
            copied["stats"] = dict(summary)
        baseline = copied.pop("stats_prev", None)
        pages_per_min, tokens_per_min = _throughput_rates(
            copied.get("stats") if isinstance(copied.get("stats"), dict) else None,
            baseline if isinstance(baseline, dict) else None,
        )
        copied["pages_per_min"] = pages_per_min
        copied["tokens_per_min"] = tokens_per_min
        live.append(copied)
    live.sort(key=lambda item: float(item["last_seen"]), reverse=True)
    return live


def reset_workers() -> None:
    """Forget every worker heartbeat (tests only)."""
    _WORKERS.clear()

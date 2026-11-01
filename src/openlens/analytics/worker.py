"""Analytics worker: consume ``analytics.events`` into the analytics store.

Batch loop: poll raw payloads, fail-closed normalize, buffer, flush to
the store every ``batch_size`` events or ``flush_interval_s`` seconds,
then commit offsets (events are only acknowledged once durable).
Retention purges rows older than ``retention_days`` on a slow cadence.
Every failure mode degrades without losing crawl/search health: a dead
broker idles, a sick store keeps the offsets uncommitted for the next
worker, and malformed events are dropped.
"""

from __future__ import annotations

import contextlib
import time
from typing import Any, Callable, Mapping

from openlens.analytics.events import AnalyticsQueue, normalize_event

DEFAULT_BATCH_SIZE = 100
DEFAULT_FLUSH_INTERVAL_S = 5.0
DEFAULT_RETENTION_DAYS = 90
DEFAULT_RETENTION_CHECK_S = 3600.0


def _analytics_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    section = (config or {}).get("analytics", {})
    if not isinstance(section, Mapping):
        section = {}
    kafka_cfg = (config or {}).get("kafka", {})
    if not isinstance(kafka_cfg, Mapping):
        kafka_cfg = {}

    def _int(key: str, default: int) -> int:
        try:
            value = int(section.get(key, default))
        except (TypeError, ValueError):
            return default
        return value if value > 0 else default

    def _float(key: str, default: float) -> float:
        try:
            value = float(section.get(key, default))
        except (TypeError, ValueError):
            return default
        return value if value > 0 else default

    return {
        "batch_size": _int("batch_size", DEFAULT_BATCH_SIZE),
        "flush_interval_s": _float("flush_interval_s", DEFAULT_FLUSH_INTERVAL_S),
        "retention_days": _int("retention_days", DEFAULT_RETENTION_DAYS),
        "retention_check_s": _float("retention_check_s", DEFAULT_RETENTION_CHECK_S),
        "kafka": kafka_cfg,
        "store": section.get("store", {}),
    }


def run_analytics_worker(
    config: Mapping[str, Any] | None,
    *,
    queue: Any | None = None,
    store: Any | None = None,
    queue_factory: Callable[[], Any] | None = None,
    store_factory: Callable[[], Any] | None = None,
    max_iterations: int | None = None,
    stop_event: Any | None = None,
) -> dict[str, int]:
    """Consume analytics events until stopped; returns intake counters.

    ``max_iterations``/``stop_event`` bound the loop for tests and CLI
    runs; ``None`` iterations run forever. Fakes inject via
    ``queue``/``store`` (or factories) to stay offline.
    """
    resolved = _analytics_config(config)
    batch_size = resolved["batch_size"]
    counts = {"received": 0, "stored": 0, "dropped": 0, "purged": 0}
    if queue is None:
        if queue_factory is not None:
            queue = queue_factory()
        else:
            queue = AnalyticsQueue.from_app_config(config)
    if store is None:
        if store_factory is not None:
            store = store_factory()
        else:
            from openlens.analytics.store import open_analytics_store

            store_cfg = resolved["store"]
            store = open_analytics_store(
                store_cfg if isinstance(store_cfg, Mapping) else {}
            )
    buffered: list[dict[str, Any]] = []
    last_flush = time.monotonic()
    last_retention = time.monotonic()

    def _flush() -> None:
        if not buffered:
            return
        try:
            counts["stored"] += int(store.insert_many(buffered) or 0)
        except Exception:
            # Sick store: keep offsets uncommitted so the next worker
            # (or the next flush) retries the same batch.
            return
        buffered.clear()
        with contextlib.suppress(Exception):
            queue.commit()

    iteration = 0
    try:
        while True:
            if max_iterations is not None and iteration >= max_iterations:
                break
            iteration += 1
            if stop_event is not None:
                is_set = False
                with contextlib.suppress(Exception):
                    is_set = bool(stop_event.is_set())
                if is_set:
                    break
            try:
                raw_events = queue.poll_events()
            except Exception:
                raw_events = []
            for raw in raw_events or []:
                counts["received"] += 1
                event = normalize_event(raw)
                if event is None:
                    counts["dropped"] += 1
                    continue
                buffered.append(event.to_payload())
            now = time.monotonic()
            if len(buffered) >= batch_size or (
                buffered and now - last_flush >= resolved["flush_interval_s"]
            ):
                _flush()
                last_flush = now
            if now - last_retention >= resolved["retention_check_s"]:
                with contextlib.suppress(Exception):
                    counts["purged"] += int(
                        store.purge_older_than(resolved["retention_days"]) or 0
                    )
                last_retention = now
    finally:
        _flush()
        for closer in (queue, store):
            close = getattr(closer, "close", None)
            if callable(close):
                with contextlib.suppress(Exception):
                    close()
    return counts

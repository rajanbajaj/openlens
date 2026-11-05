"""Write-behind mirror flusher: Redis hot path → Postgres cold mirror.

When ``frontier.backend == redis``, crawlers claim/complete URLs against
:class:`openlens.storage.frontier_redis.RedisFrontierStore` (shared leases
without Postgres write pressure). Every terminal transition and discovery
insert the hot path makes is also appended to an in-memory buffer
(:class:`FlushRecord`); this module drains that buffer into the
``storage:``-keyed store (Postgres in production, SQLite in tests) so the
ops reads — ``/frontier/failed*``, ``/frontier/queued/domains``,
``/frontier/status``, auto-triage — keep serving Postgres with a bounded
staleness of ``flush_interval_s`` (default 5 s).

Slice ownership: the hot path owns claims/leases/heartbeats; the mirror
owns every ops read. Requeue/purge/skip mutations land on the mirror
directly and converge on the next flush cycle.
"""

from __future__ import annotations

import contextlib
import threading
import time
from typing import Any, Mapping

from openlens.storage.frontier import (
    CrawlCheckpoint,
    FrontierStore,
    normalize_frontier_url,
)

#: Prometheus-style gauge name for mirror staleness: seconds since the
#: last successful flush (``now - last_ok``; ``inf`` before the first
#: flush). The ops doc SLA references this name.
FRONTIER_MIRROR_LAG_GAUGE = "frontier_mirror_lag_seconds"

#: Default flush cadence, mirroring ``frontier.redis.flush_interval_s``.
DEFAULT_FLUSH_INTERVAL_S = 5.0

#: Biggest single drain; bounds one flush pass under a deep backlog.
DEFAULT_FLUSH_BATCH_SIZE = 1000

_last_mirror_flush_at: float | None = None
_last_mirror_flush_lock = threading.Lock()


def record_mirror_flush(now: float | None = None) -> None:
    """Mark a successful mirror flush (updates the lag gauge)."""
    global _last_mirror_flush_at
    with _last_mirror_flush_lock:
        _last_mirror_flush_at = time.time() if now is None else float(now)


def frontier_mirror_lag_seconds(now: float | None = None) -> float:
    """Seconds since the last successful mirror flush (``inf`` if never)."""
    with _last_mirror_flush_lock:
        last = _last_mirror_flush_at
    if last is None:
        return float("inf")
    return max(0.0, (time.time() if now is None else float(now)) - last)


class FrontierMirrorFlusher:
    """Batched write-behind drain from a Redis hot path to a mirror store.

    ``source`` is the Redis hot-path store (anything exposing
    ``drain_pending_flush``/``load_checkpoint``/``get_settings``);
    ``mirror`` is the ``storage:``-keyed store (Postgres in prod) that
    serves every ops read. Each :meth:`flush_once` pass replays buffered
    records in order — enqueue first so terminal transitions always have
    a row — then checkpoints/settings by re-reading the source payload.
    Attempt counts converge exactly: every source ``mark_failed`` bumps
    once and appends one record, and the mirror bumps once per replay.
    """

    def __init__(
        self,
        source: Any,
        mirror: FrontierStore,
        interval_s: float = DEFAULT_FLUSH_INTERVAL_S,
    ) -> None:
        try:
            interval = float(interval_s)
        except (TypeError, ValueError):
            interval = DEFAULT_FLUSH_INTERVAL_S
        self._source = source
        self._mirror = mirror
        self._interval_s = interval if interval > 0 else DEFAULT_FLUSH_INTERVAL_S
        self._last_ok: float | None = None
        self._stop: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def interval_s(self) -> float:
        """Flush cadence in seconds (the documented staleness bound)."""
        return self._interval_s

    @property
    def mirror_lag_s(self) -> float:
        """Seconds since this flusher's last successful pass (``inf``)."""
        if self._last_ok is None:
            return float("inf")
        return max(0.0, time.time() - self._last_ok)

    def _note_ok(self) -> None:
        self._last_ok = time.time()
        record_mirror_flush(self._last_ok)

    def _ensure_row(
        self, url: str, depth: int, parent_url: str | None, priority: int
    ) -> None:
        """Idempotent mirror row so terminal replays always have a target."""
        self._mirror.enqueue(url, depth, parent_url, priority)

    def _apply(self, record: Any) -> None:
        kind = getattr(record, "kind", "")
        url = str(getattr(record, "url", "") or "")
        if kind == "checkpoint":
            checkpoint = self._source.load_checkpoint(url or "default")
            if isinstance(checkpoint, CrawlCheckpoint):
                self._mirror.save_checkpoint(checkpoint)
            return
        if kind == "settings":
            payload = self._source.get_settings(url or "default")
            if isinstance(payload, dict):
                self._mirror.save_settings(dict(payload), url or "default")
            return
        if not url:
            return
        if kind == "enqueued":
            self._mirror.enqueue(
                url,
                int(getattr(record, "depth", 0) or 0),
                getattr(record, "parent_url", None),
                int(getattr(record, "priority", 0) or 0),
            )
        elif kind == "done":
            self._ensure_row(
                url,
                int(getattr(record, "depth", 0) or 0),
                getattr(record, "parent_url", None),
                int(getattr(record, "priority", 0) or 0),
            )
            self._mirror.mark_done(
                url,
                etag=getattr(record, "etag", None),
                last_modified=getattr(record, "last_modified", None),
            )
        elif kind == "failed":
            self._ensure_row(
                url,
                int(getattr(record, "depth", 0) or 0),
                getattr(record, "parent_url", None),
                int(getattr(record, "priority", 0) or 0),
            )
            self._mirror.mark_failed(url, str(getattr(record, "last_error", "") or ""))
        elif kind == "skipped":
            self._ensure_row(
                url,
                int(getattr(record, "depth", 0) or 0),
                None,
                int(getattr(record, "priority", 0) or 0),
            )
            self._mirror.mark_skipped(url)
        # Unknown kinds are ignored (forward-compatible buffer).

    def flush_once(self, batch_size: int = DEFAULT_FLUSH_BATCH_SIZE) -> int:
        """Drain one batch into the mirror; returns records applied.

        On a mid-batch mirror error the unapplied tail is returned to
        the source buffer head (at-least-once) and the error propagates;
        the lag gauge only advances on a fully successful pass.
        """
        try:
            count = max(0, int(batch_size))
        except (TypeError, ValueError):
            count = 0
        records = self._source.drain_pending_flush(count)
        if not records:
            self._note_ok()
            return 0
        applied = 0
        try:
            for record in records:
                self._apply(record)
                applied += 1
        except Exception:
            requeue = getattr(self._source, "requeue_flush", None)
            if callable(requeue):
                with contextlib.suppress(Exception):
                    requeue(records[applied:])
            raise
        self._note_ok()
        return applied

    def backfill_once(self, batch_size: int = 1000, max_urls: int = 500000) -> int:
        """Replay mirror ``queued`` rows into the hot store (cutover once).

        Runs at worker boot when the Redis backend is fresh: the mirror
        holds the pre-cutover backlog but the hot ready queue is empty,
        so without this the fleet idles while the dashboard shows a
        frozen queue. First claimant via ``SET NX`` wins; losers return
        0. Skips entirely when a backfill already completed. Mirror
        ``active`` rows come along too — their pre-cutover owners are
        gone, so replaying them as queued is recovery, not duplication.
        Attempts reset to the fresh-enqueue budget (at-least-once may
        refetch); priorities, depths, and parents carry over. A capped
        pass does not seal the migration, so a later boot resumes the
        remainder. Never raises: returns 0 when there is nothing to do
        or the mirror is unreadable, so boot always proceeds.
        """
        source, mirror = self._source, self._mirror
        for probe in ("backfill_done", "try_claim_backfill", "enqueue"):
            if not callable(getattr(source, probe, None)):
                return 0
        try:
            if bool(source.backfill_done()):
                return 0
        except Exception:
            return 0
        try:
            if not bool(source.try_claim_backfill()):
                return 0
        except Exception:
            return 0
        try:
            batch = max(1, int(batch_size or 0))
        except (TypeError, ValueError):
            batch = 1000
        try:
            cap = max(0, int(max_urls or 0))
        except (TypeError, ValueError):
            cap = 0
        moved = 0
        capped = False
        seen: set[str] = set()
        try:
            # Always page from offset 0: replayed rows stay queued in the
            # mirror until terminal flushes move them, so the local seen
            # set (not the offset) is what guarantees termination.
            while True:
                rows = mirror.pending(limit=batch)
                if not rows:
                    break
                progressed = False
                for entry in rows:
                    url = str(getattr(entry, "url", "") or "")
                    if not url.strip():
                        continue
                    try:
                        norm = normalize_frontier_url(url)
                    except (AttributeError, ValueError):
                        continue
                    if norm in seen:
                        continue
                    seen.add(norm)
                    try:
                        depth = int(getattr(entry, "depth", 0) or 0)
                    except (TypeError, ValueError):
                        depth = 0
                    try:
                        prio = getattr(entry, "priority", 0)
                        priority = int(prio) if prio is not None else None
                    except (TypeError, ValueError):
                        priority = None
                    parent = getattr(entry, "parent_url", None)
                    try:
                        source.enqueue(
                            url,
                            depth,
                            parent_url=parent if isinstance(parent, str) else None,
                            priority=priority,
                        )
                    except Exception:  # noqa: S112 - one junk URL skips silently
                        continue
                    moved += 1
                    progressed = True
                    if 0 < cap <= moved:
                        capped = True
                        break
                if capped:
                    break
                if not progressed:
                    break
            # A capped pass leaves a remainder behind: only seal the
            # migration when the mirror drained, so a later boot (after
            # the claim lock expires) resumes where this one stopped.
            if not capped:
                with contextlib.suppress(Exception):
                    source.mark_backfill_done()
        except Exception:
            return moved
        return moved

    def run_forever(
        self,
        stop_event: threading.Event | None = None,
        *,
        batch_size: int = DEFAULT_FLUSH_BATCH_SIZE,
    ) -> int:
        """Flush every ``interval_s`` until ``stop_event`` is set.

        Per-pass errors are fail-open (the lag gauge reveals them); only
        a set stop event ends the loop. Returns total records applied.
        """
        stop = stop_event if stop_event is not None else threading.Event()
        total = 0
        while not stop.is_set():
            try:
                total += self.flush_once(batch_size)
            except Exception as exc:  # noqa: BLE001 - loop must survive blips
                print(f"[frontier-flusher] mirror flush failed: {exc}", flush=True)
            if stop.wait(self._interval_s):
                break
        return total

    def start(self, *, batch_size: int = DEFAULT_FLUSH_BATCH_SIZE) -> threading.Event:
        """Start the background flush thread; returns its stop event."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                if self._stop is None:  # pragma: no cover - lock-step invariant
                    self._stop = threading.Event()
                return self._stop
            stop = threading.Event()
            thread = threading.Thread(
                target=self.run_forever,
                args=(stop,),
                kwargs={"batch_size": batch_size},
                name="ol-frontier-flusher",
                daemon=True,
            )
            self._stop = stop
            self._thread = thread
            thread.start()
            return stop

    def stop_and_flush(
        self,
        *,
        batch_size: int = DEFAULT_FLUSH_BATCH_SIZE,
        join_timeout_s: float = 10.0,
    ) -> int:
        """Stop the background thread and flush any remainder. Returns applied."""
        with self._lock:
            stop, thread = self._stop, self._thread
        if stop is not None:
            stop.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, float(join_timeout_s or 0.0)))
        return self.flush_once(batch_size)


def open_mirror_flusher(
    config: Mapping[str, Any],
    *,
    source: Any | None = None,
) -> FrontierMirrorFlusher | None:
    """Build the write-behind flusher, or ``None`` when not Redis-backed.

    Returns ``None`` unless ``frontier.backend == redis`` (shipped default
    ``postgres`` is untouched). With Redis, ``source`` defaults to the
    crawl-path hot store and the mirror is the ``storage:``-keyed store;
    either raising (unreachable backend) propagates fail-loud at boot.
    """
    from openlens.storage.frontier import open_frontier_store
    from openlens.storage.frontier_redis import open_frontier_store_for_crawl

    if not isinstance(config, dict):
        return None
    section = config.get("frontier", None)
    if not isinstance(section, dict):
        return None
    if section.get("backend", "postgres") != "redis":
        return None
    hot = source if source is not None else open_frontier_store_for_crawl(config)
    mirror = open_frontier_store(config)
    redis_cfg = section.get("redis", {})
    if not isinstance(redis_cfg, dict):
        redis_cfg = {}
    try:
        interval = float(redis_cfg.get("flush_interval_s", DEFAULT_FLUSH_INTERVAL_S))
    except (TypeError, ValueError):
        interval = DEFAULT_FLUSH_INTERVAL_S
    if interval <= 0:
        interval = DEFAULT_FLUSH_INTERVAL_S
    return FrontierMirrorFlusher(hot, mirror, interval)


def stop_and_flush_mirror(flusher: FrontierMirrorFlusher | None) -> int:
    """Shutdown helper: stop a flusher and drain the remainder (fail-open).

    ``None`` (non-Redis backend) is a no-op returning 0. Mirror errors
    propagate to the caller, which shuts down under ``contextlib.suppress``.
    """
    if flusher is None:
        return 0
    return flusher.stop_and_flush()

"""Persistent URL frontier: queue + crawl checkpoints + crawler settings.

Postgres-first with SQLite parity. Two tables live alongside (never
inside) the ``documents`` table:

- ``frontier_queue``: one row per discovered URL (normalized URL is the
  primary key), with depth, status, parent URL, fetch attempts, last
  error, and conditional validators (ETag / Last-Modified) so a paused
  crawl can resume without refetching validators.
- ``crawl_checkpoints``: one row per crawl id (``id`` TEXT PRIMARY KEY)
  carrying seeds, phase (``running`` | ``paused`` | ``done``), page
  counters, and the ``CrawlStats`` counters as JSON.
- ``frontier_settings``: one row per settings id (a single ``default``
  row in practice) carrying the crawler settings payload as JSON.

``FrontierStore`` is the parallel ABC (alongside, not inside,
``storage/base.py::DocumentStore``) so no existing backend or
conformance suite needs to change. ``SqliteFrontierStore`` is the
zero-dependency reference; ``PostgresFrontierStore`` mirrors it over
``psycopg`` (lazy driver import, ``ProviderError(UNAVAILABLE)`` when the
driver or server is missing). MySQL follows the same shape.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import queue
import re
import signal
import socket
import sqlite3
import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from openlens.core.interfaces import ProviderError

_PROVIDER_NAME = "custom_index"

#: Env var carrying an explicit worker identity for shared-queue crawls.
WORKER_ID_ENV = "OPENLENS_WORKER_ID"

#: Default lease time-to-live for a claimed URL (seconds). A claim whose
#: heartbeat is older than this is considered expired and may be
#: requeued by any worker. Tune against fetch timeouts: TTL must exceed
#: ``timeout_s + max crawl-delay`` for the slowest domain.
DEFAULT_LEASE_TTL_S = 120

#: Default heartbeat period (seconds): owners refresh owned rows every
#: ``lease_ttl_s // 3`` (TTL/3), so ~3 heartbeats fit inside one TTL.
DEFAULT_HEARTBEAT_INTERVAL_S = 40

#: Default reaper sweep period (seconds) for a background expiry loop.
DEFAULT_REAPER_INTERVAL_S = 30

#: Queue row states.
STATUS_QUEUED = "queued"
STATUS_ACTIVE = "active"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

_VALID_STATUSES = frozenset(
    {STATUS_QUEUED, STATUS_ACTIVE, STATUS_DONE, STATUS_FAILED, STATUS_SKIPPED}
)

#: User-requested crawl URL states (``crawl_requests`` table).
CRAWL_REQUEST_PENDING = "pending"
CRAWL_REQUEST_APPROVED = "approved"
CRAWL_REQUEST_DECLINED = "declined"

#: Terminal decision states accepted by ``set_crawl_request_status``.
VALID_CRAWL_REQUEST_DECISIONS = frozenset(
    {CRAWL_REQUEST_APPROVED, CRAWL_REQUEST_DECLINED}
)

#: Checkpoint phases.
PHASE_RUNNING = "running"
PHASE_PAUSED = "paused"
PHASE_DONE = "done"

_VALID_PHASES = frozenset({PHASE_RUNNING, PHASE_PAUSED, PHASE_DONE})

#: Default settings row id (single-row table in practice).
DEFAULT_SETTINGS_ID = "default"

#: Default crawl id used when the caller names none.
DEFAULT_CRAWL_ID = "default"

#: SQLite write-lock wait before surfacing UNAVAILABLE.
_BUSY_TIMEOUT_MS = 5000

#: Postgres driver module (lazy import only).
_DRIVER_MODULE = "psycopg"


def _utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _utcnow_precise() -> str:
    """UTC timestamp with microsecond resolution (ISO-8601, ``Z`` suffix).

    Lease columns use this (not :func:`_utcnow`) so heartbeats written
    moments apart still order deterministically under lexicographic
    comparison. The fixed-width format keeps string comparison correct.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _cutoff_iso(max_age_s: float, now: float | None = None) -> str:
    """ISO cutoff: heartbeats older than ``now - max_age_s`` are expired."""
    moment = now if now is not None else time.time()
    return datetime.fromtimestamp(moment - max_age_s, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )


def resolve_worker_id(explicit: str | None = None) -> str:
    """Resolve a worker identity for shared-queue coordination.

    Precedence: explicit argument, ``OPENLENS_WORKER_ID`` env, then
    ``{hostname}-{pid}-{8 hex}``. Always returns a non-empty string.
    """
    if explicit and explicit.strip():
        return explicit.strip()
    env = os.environ.get(WORKER_ID_ENV, "").strip()
    if env:
        return env
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


@dataclass
class FrontierEntry:
    """One queued/discovered URL."""

    url: str
    depth: int = 0
    status: str = STATUS_QUEUED
    parent_url: str | None = None
    priority: int = 0
    attempts: int = 0
    last_error: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    owner: str | None = None
    leased_at: str | None = None
    heartbeat_at: str | None = None
    lease_ttl_s: int = DEFAULT_LEASE_TTL_S


@dataclass
class CrawlCheckpoint:
    """Resumable per-crawl progress row."""

    crawl_id: str = DEFAULT_CRAWL_ID
    seeds: list[str] = field(default_factory=list)
    phase: str = PHASE_RUNNING
    pages_attempted: int = 0
    pages_indexed: int = 0
    frontier_size: int = 0
    stats: dict[str, Any] = field(default_factory=dict)
    updated_at: str = ""


class FrontierStore(ABC):
    """Abstract persistent URL frontier + checkpoints + settings."""

    @abstractmethod
    def enqueue(
        self,
        url: str,
        depth: int = 0,
        parent_url: str | None = None,
        priority: int | None = None,
    ) -> bool:
        """Queue ``url`` (normalized) unless already known.

        Returns True when the URL was newly queued, False when the
        normalized URL was already present (dedup, like the in-crawl
        ``seen`` set plus ``normalize_url``). An explicit ``priority``
        is stored on insert and overwrites the stored value on
        conflict; ``None`` keeps whatever is there (``0`` for new rows).
        """
        raise NotImplementedError

    @abstractmethod
    def dequeue(self) -> FrontierEntry | None:
        """Pop the oldest queued URL (depth, then discovery order).

        The row is marked ``active`` so a crash/pause leaves it
        distinguishable from ``queued``; callers finish it with
        :meth:`mark_done` / :meth:`mark_failed`, or return it to the
        queue with :meth:`requeue_active` on pause/resume.
        """
        raise NotImplementedError

    @abstractmethod
    def mark_done(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> None:
        """Mark a URL fetched (storing conditional validators)."""
        raise NotImplementedError

    @abstractmethod
    def mark_failed(self, url: str, error: str) -> None:
        """Mark a URL failed, bumping its attempt counter."""
        raise NotImplementedError

    @abstractmethod
    def mark_skipped(self, url: str) -> int:
        """Drop pending rows past capacity (oldest-first eviction).

        Mirrors ``BoundedFrontier`` oldest-first eviction: removes the
        oldest ``queued`` rows until ``limit`` rows remain, counting the
        removals. Returns the number evicted.
        """
        raise NotImplementedError

    def skip_queued_domains(self, domains: Any) -> int:
        """Bulk-skip ``queued`` rows whose URL host matches ``domains``.

        Settings enforcement for the ``skip_domains`` list: every
        ``queued`` row whose host equals (or is a subdomain of) a listed
        domain moves to ``skipped`` with a ``last_error`` reason, so
        in-flight queue URLs stop without waiting for their turn. New
        enqueues from listed domains are still stored (never silently
        dropped, so counts stay auditable); they are skipped at
        claim/bulk time instead. Returns the number of rows moved.
        Concrete default (``0``) so older/test doubles keep working;
        the shipped SQLite and Postgres stores override it.
        """
        _ = domains
        return 0

    @abstractmethod
    def requeue_active(self) -> int:
        """Return ``active`` rows to ``queued`` (pause/resume path).

        Returns the number of rows moved.
        """
        raise NotImplementedError

    @abstractmethod
    def requeue(self, url: str) -> bool:
        """Return one ``active``/``failed`` URL to ``queued`` (retry).

        Returns True when a row was moved.
        """
        raise NotImplementedError

    def requeue_stale(self, max_age_days: int, limit: int) -> int:
        """Return ``done`` rows older than ``max_age_days`` to ``queued``.

        Powers the refresh pass: stale pages refetch (conditionally via
        stored ETag/Last-Modified, so unchanged pages cost one 304) and
        re-index. Rows never fetched (``fetched_at`` NULL) count as
        stale. Attempts reset so a refresh is a fresh fetch, not a
        retry. Invalid bounds fail closed to ``0``; oldest first up to
        ``limit``. Concrete default (``0``) so older/test doubles keep
        working; the shipped SQLite and Postgres stores override it.
        """
        _ = (max_age_days, limit)
        return 0

    @abstractmethod
    def clear_queued(self) -> int:
        """Delete every ``queued`` row (admin queue-clean).

        In-flight (``active``) rows are kept, as is all history
        (``done``/``failed``) and coordination state. Returns the
        number of rows removed.
        """
        raise NotImplementedError

    @abstractmethod
    def is_known(self, url: str) -> bool:
        """True when the normalized URL already has a queue row."""
        raise NotImplementedError

    def status_of(self, url: str) -> str | None:
        """Row status for the normalized URL, None when unknown.

        Concrete (non-abstract) so older/test doubles keep working;
        lets the dashboard drop stale in-memory seeds the store
        already finished instead of reshowing them as queued.
        """
        return None

    def statuses_of(self, urls: Iterable[str]) -> dict[str, str]:
        """Row statuses for normalized URLs, keyed by normalized URL.

        Bulk form of :meth:`status_of`: unknown URLs are omitted. The
        default loops ``status_of`` (correct everywhere); backends
        override with a chunked single-query lookup so a dashboard
        holding thousands of in-memory seeds does not pay one round
        trip per seed on every status poll.
        """
        out: dict[str, str] = {}
        for url in urls or []:
            status: str | None = None
            with contextlib.suppress(Exception):
                status = self.status_of(url)
            if status is None:
                continue
            try:
                out[normalize_frontier_url(url)] = str(status)
            except (AttributeError, ValueError):
                continue
        return out

    def reseed(self, url: str) -> bool:
        """Revive a terminal row (failed/skipped/done) back to queued.

        Explicit reseeds (dashboard Resume) must refetch even when the
        URL already finished once; plain ``enqueue`` is insert-only and
        would no-op. Resets attempts and clears the error. Concrete
        default (False) so older/test doubles keep working.
        """
        return False

    def reseed_many(self, urls: Iterable[str]) -> int:
        """Revive terminal rows in bulk; returns the revived count.

        Bulk form of :meth:`reseed` for dashboard Resume with a pasted
        seed list: one statement instead of one UPDATE per seed. The
        default loops :meth:`reseed` (correct everywhere); backends
        override with a chunked single-UPDATE lookup.
        """
        revived = 0
        for url in urls or []:
            ok = False
            with contextlib.suppress(Exception):
                ok = bool(self.reseed(url))
            if ok:
                revived += 1
        return revived

    # -- user-requested crawl URLs (concrete defaults) -------------------
    # Shipped stores persist these in ``crawl_requests``; the concrete
    # defaults below keep older/test doubles instantiable. ``submit``
    # returns an unpersisted marker (``id`` None) on doubles — routes
    # treat that as unavailable (503) instead of inventing an id.

    def submit_crawl_request(self, url: str) -> dict[str, Any]:
        """Record a user-requested crawl URL as ``pending`` (deduped).

        Returns the stored record (``id``, ``url``, ``status``,
        ``created_at``, ``decided_at``); an existing ``pending`` row
        for the URL is returned unchanged.
        """
        return {
            "id": None,
            "url": url,
            "status": CRAWL_REQUEST_PENDING,
            "created_at": None,
            "decided_at": None,
        }

    def list_crawl_requests(
        self, status: str | None = None, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        """List crawl requests, newest first, optionally by status."""
        return []

    def set_crawl_request_status(self, request_id: int, status: str) -> bool:
        """Move a request to ``approved``/``declined`` (stamps decided_at).

        Returns True when a row changed; False for unknown ids,
        non-pending rows, or statuses outside the pair.
        """
        return False

    @abstractmethod
    def pending(self, limit: int = 500, offset: int = 0) -> list[FrontierEntry]:
        """List ``queued`` + ``active`` rows in dequeue order."""
        raise NotImplementedError

    @abstractmethod
    def failed(self, limit: int = 100, offset: int = 0) -> list[FrontierEntry]:
        """List ``failed`` rows, most-retried first.

        Powers the admin failed-URL view (URL + short reason per row).
        """
        raise NotImplementedError

    @abstractmethod
    def failed_domains(
        self, limit: int = 100, offset: int = 0
    ) -> list[tuple[str, int, str]]:
        """Aggregate ``failed`` rows by host: ``(domain, count, top_reason)``.

        Powers the admin failed-domains view (one row per offending
        host with its failure count and most common reason,
        most-failed first).
        """
        raise NotImplementedError

    @abstractmethod
    def failed_domain_count(self) -> int:
        """Number of distinct hosts among ``failed`` rows."""
        raise NotImplementedError

    @abstractmethod
    def requeue_failed_domains(self, domains: Any) -> int:
        """Revive ``failed`` rows from ``domains`` back to ``queued``.

        Attempts reset to 0 and the error clears (mirror of
        :meth:`reseed` semantics), so the rows actually refetch instead
        of re-terminating on the spent retry budget. Returns rows moved.
        """
        raise NotImplementedError

    @abstractmethod
    def purge_failed_domains(self, domains: Any) -> int:
        """Delete ``failed`` rows from ``domains`` (exact+subdomain).

        History only — queued/active/done rows are untouched. Returns
        rows deleted.
        """
        raise NotImplementedError

    @abstractmethod
    def queued_domains(
        self, limit: int = 100, offset: int = 0
    ) -> list[tuple[str, int]]:
        """Aggregate ``queued`` rows by host with per-host counts.

        Powers the admin queued-domains view (priority/skip/block per
        host). Same host bucketing as :meth:`failed_domains` minus the
        reason column.
        """
        raise NotImplementedError

    @abstractmethod
    def queued_domain_count(self) -> int:
        """Number of distinct hosts among ``queued`` rows."""
        raise NotImplementedError

    @abstractmethod
    def bump_queued_priority(self, domains: Any, delta: int = 1) -> int:
        """Shift ``priority`` of ``queued`` rows from ``domains`` by ``delta``.

        Exact+subdomain host match, one ``UPDATE`` per normalized domain;
        active/done/failed/skipped rows are untouched. Returns rows moved.
        """
        raise NotImplementedError

    @abstractmethod
    def counts(self) -> dict[str, int]:
        """Row counts keyed by status plus ``total``."""
        raise NotImplementedError

    @abstractmethod
    def save_checkpoint(self, checkpoint: CrawlCheckpoint) -> None:
        """Upsert a crawl checkpoint row."""
        raise NotImplementedError

    @abstractmethod
    def load_checkpoint(
        self, crawl_id: str = DEFAULT_CRAWL_ID
    ) -> CrawlCheckpoint | None:
        """Fetch a checkpoint row, or None when unknown."""
        raise NotImplementedError

    @abstractmethod
    def get_settings(self, settings_id: str = DEFAULT_SETTINGS_ID) -> dict[str, Any]:
        """Return the settings payload ({} when never saved)."""
        raise NotImplementedError

    @abstractmethod
    def save_settings(
        self, payload: Mapping[str, Any], settings_id: str = DEFAULT_SETTINGS_ID
    ) -> None:
        """Upsert the settings payload."""
        raise NotImplementedError

    @abstractmethod
    def close(self) -> None:
        """Release backend resources. Idempotent."""
        raise NotImplementedError

    # -- shared-queue coordination (concrete defaults) -------------------
    # The defaults below preserve single-worker behavior so third-party
    # ``FrontierStore`` subclasses keep working: ``claim`` degrades to
    # ``dequeue``, ``reap_expired_leases`` degrades to ``requeue_active``,
    # and the registry/politeness/counter helpers are safe no-ops. The
    # shipped SQLite and Postgres stores override all of these with
    # lease-aware implementations backed by shared tables.

    def claim(
        self,
        worker_id: str,
        *,
        lease_ttl_s: int = DEFAULT_LEASE_TTL_S,
    ) -> FrontierEntry | None:
        """Atomically claim one URL for ``worker_id`` (multi-worker dequeue).

        Unlike :meth:`dequeue`, the row records its owner plus lease
        timestamps, so a crashed worker's rows are distinguishable from
        live ones and recoverable via :meth:`reap_expired_leases`.
        Returns None when no queued (or expired) URL is available.
        """
        _ = lease_ttl_s
        return self.dequeue()

    def heartbeat(
        self,
        worker_id: str,
        *,
        lease_ttl_s: int = DEFAULT_LEASE_TTL_S,
    ) -> int:
        """Refresh the lease on ``worker_id``'s owned ``active`` rows.

        Returns the number of rows touched. Owners call this every
        ``lease_ttl_s // 3`` seconds while a URL is in flight.
        """
        _ = (worker_id, lease_ttl_s)
        return 0

    def reap_expired_leases(
        self,
        *,
        max_age_s: float = DEFAULT_LEASE_TTL_S,
        now: float | None = None,
    ) -> int:
        """Return expired ``active`` rows to ``queued`` (lease-aware requeue).

        Only rows whose heartbeat is older than ``max_age_s`` (or that
        never heartbeated, i.e. legacy ownerless rows) are moved; rows
        with a live heartbeat are never stolen. This replaces the
        unconditional :meth:`requeue_active` on shared-queue startup.
        Returns the number of rows moved.
        """
        _ = (max_age_s, now)
        return self.requeue_active()

    def requeue_owned(self, worker_id: str) -> int:
        """Return ``worker_id``'s ``active`` rows to ``queued``.

        Graceful-shutdown path: a worker finishing (SIGTERM/SIGINT,
        cooperative stop) releases its in-flight rows for other workers
        instead of leaving them ``active`` until lease expiry. Returns
        the number of rows moved.
        """
        _ = worker_id
        return 0

    def owned(self, worker_id: str) -> list[FrontierEntry]:
        """List ``worker_id``'s currently owned ``active`` rows."""
        _ = worker_id
        return []

    def reserve_politeness(
        self,
        host: str,
        delay_s: float,
        *,
        now: float | None = None,
    ) -> float:
        """Shared per-domain politeness gate (cross-worker crawl-delay).

        Atomically reserves the next fetch slot for ``host``: returns the
        seconds the caller must wait before fetching (``0.0`` means fetch
        now) and advances the shared ``next_fetch_at`` by ``delay_s``.
        ``delay_s`` mirrors ``_wait_polite``: ``max(crawl_delay_s,
        per-domain rate, robots crawl-delay)``. The default honors no
        shared state (returns ``0.0``); the shipped stores persist it.
        """
        _ = (host, delay_s, now)
        return 0.0

    def get_politeness_delay(self, host: str) -> float:
        """Return the last reserved delay for ``host`` (0.0 when unknown)."""
        _ = host
        return 0.0

    def register_worker(
        self,
        worker_id: str,
        crawl_id: str = DEFAULT_CRAWL_ID,
    ) -> None:
        """Record ``worker_id`` as live on ``crawl_id`` (idempotent)."""
        _ = (worker_id, crawl_id)

    def heartbeat_worker(
        self,
        worker_id: str,
        crawl_id: str = DEFAULT_CRAWL_ID,
        *,
        claimed: int = 0,
    ) -> None:
        """Refresh ``worker_id``'s liveness row (idempotent upsert)."""
        _ = (worker_id, crawl_id, claimed)

    def list_workers(self, crawl_id: str | None = None) -> list[dict[str, Any]]:
        """List known worker liveness rows, newest heartbeat first."""
        _ = crawl_id
        return []

    def prune_workers(
        self,
        *,
        max_age_s: float = DEFAULT_LEASE_TTL_S * 2,
        now: float | None = None,
    ) -> int:
        """Delete worker liveness rows older than ``max_age_s``."""
        _ = (max_age_s, now)
        return 0

    def increment_counters(
        self,
        crawl_id: str = DEFAULT_CRAWL_ID,
        *,
        pages_attempted: int = 0,
        pages_indexed: int = 0,
    ) -> CrawlCheckpoint:
        """Atomically add deltas to a checkpoint's counters.

        Unlike :meth:`save_checkpoint` (last-writer-wins full-row
        upsert), concurrent workers calling this never clobber each
        other's progress. Creates the row when missing. Returns the
        merged checkpoint.
        """
        checkpoint = self.load_checkpoint(crawl_id)
        if checkpoint is None:
            checkpoint = CrawlCheckpoint(crawl_id=crawl_id)
        checkpoint.pages_attempted += max(0, pages_attempted)
        checkpoint.pages_indexed += max(0, pages_indexed)
        self.save_checkpoint(checkpoint)
        return checkpoint


def normalize_frontier_url(url: str) -> str:
    """Normalize a frontier URL (strip, drop #fragment, lowercase)."""
    from urllib.parse import urldefrag

    bare, _ = urldefrag(url.strip())
    return bare.lower().rstrip("/")


def normalize_priority(priority: Any) -> int | None:
    """Normalize an enqueue priority: ints win, ``None`` keeps, else ignore.

    ``None`` (or a non-int like a bool) means "don't touch the stored
    value": inserts fall back to ``0``, conflicts keep the row as is.
    An explicit int always wins, including on already-queued rows.
    """
    if priority is None:
        return None
    if isinstance(priority, bool) or not isinstance(priority, int):
        return None
    return priority


#: Bare-hostname shape for one ``skip_domains`` entry (single labels like
#: ``localhost`` allowed; each label 1-63 chars, total capped at 253 by
#: the caller, mirroring the API schema bounds).
_SKIP_DOMAIN_RE = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*"
)


def normalize_skip_domain(entry: Any) -> str | None:
    """Normalize one ``skip_domains`` entry to a bare lowercase hostname.

    Strips whitespace, ``*.``/leading-dot wildcards, schemes, userinfo,
    ports, and paths; returns None for malformed entries (non-strings,
    empties, oversize, or non-hostnames), which callers ignore instead
    of failing.
    """
    if not isinstance(entry, str):
        return None
    text = entry.strip().lower()
    if not text:
        return None
    if "://" in text:
        from urllib.parse import urlsplit

        try:
            text = (urlsplit(text).hostname or "").lower()
        except ValueError:
            return None
    else:
        for sep in ("/", "?", "#"):
            text = text.split(sep)[0]
        text = text.strip()
        if "@" in text:
            return None
        if ":" in text:
            host_part, _, port = text.partition(":")
            if not port.isdigit():
                return None
            text = host_part.strip()
    text = text.strip().lstrip(".")
    if text.startswith("*."):
        text = text[2:]
    text = text.rstrip(".")
    if not 1 <= len(text) <= 253:
        return None
    if not _SKIP_DOMAIN_RE.fullmatch(text):
        return None
    return text


#: Postgres host extraction for the failed-domains rollup. Must not
#: contain a literal ``%`` outside ``%s`` placeholders: psycopg scans
#: ``%X`` client-side and rejects e.g. the ``%:`` in ``'http%://%'``
#: (ProgrammingError), so the scheme check uses split_part/IN.
FAILED_DOMAIN_HOST_EXPR_PG = (
    "CASE WHEN lower(split_part(url, ':', 1)) IN ('http', 'https')"
    " AND split_part(split_part(split_part(url, '://', 2),"
    " '?', 1), '/', 1) <> ''"
    " THEN lower(split_part(split_part(split_part(url, '://', 2),"
    " '?', 1), '/', 1)) ELSE '(invalid)' END"
)


#: SQLite host extraction for the failed-domains rollup (mirrors the
#: Postgres constant below; ``?`` placeholders are bound params here).
FAILED_DOMAIN_HOST_EXPR_SQLITE = (
    "CASE WHEN lower(url) LIKE 'http%://%'"
    " THEN lower(substr(hq, 1, instr(hq || '/', '/') - 1))"
    " ELSE '(invalid)' END"
)
_FAILED_DOMAIN_HOST_FROM_SQLITE = (
    "FROM (SELECT substr(h, 1, instr(h || '?', '?') - 1)"
    " AS hq, url, last_error FROM (SELECT"
    " substr(url, instr(url, '://') + 3) AS h, url, last_error"
    " FROM frontier_queue WHERE status = 'failed'))"
)
_QUEUED_DOMAIN_HOST_FROM_SQLITE = (
    "FROM (SELECT substr(h, 1, instr(h || '?', '?') - 1)"
    " AS hq, url FROM (SELECT"
    " substr(url, instr(url, '://') + 3) AS h, url"
    " FROM frontier_queue WHERE status = 'queued'))"
)


def normalize_skip_domains(domains: Any) -> list[str] | None:
    """Validate a ``skip_domains`` setting value.

    Returns the normalized (lowercase, bare-hostname, deduped) list, or
    None when the value is missing/not-a-list. Malformed entries are
    ignored, never fatal.
    """
    if domains is None:
        return None
    if isinstance(domains, str) or not isinstance(domains, (list, tuple)):
        return None
    out: list[str] = []
    for entry in domains:
        normalized = normalize_skip_domain(entry)
        if normalized is not None and normalized not in out:
            out.append(normalized)
    return out


def host_matches_skip_domains(host: Any, domains: Any) -> bool:
    """True when ``host`` equals (or is a subdomain of) a listed domain.

    The single Python-side matcher behind skip enforcement (exact host
    or any subdomain — the same semantics as the preference scorer's
    domain match). SQL bulk-skip mirrors it with equivalent LIKE
    patterns; do not invent a third matcher.
    """
    if not isinstance(host, str):
        return False
    candidate = host.strip().lower().rstrip(".")
    if not candidate:
        return False
    entries = domains if isinstance(domains, (list, tuple)) else []
    for entry in entries:
        if not isinstance(entry, str):
            continue
        domain = entry.strip().lower()
        if not domain:
            continue
        if candidate == domain or candidate.endswith("." + domain):
            return True
    return False


def skip_url_matches(url: Any, domains: Any) -> bool:
    """True when ``url``'s host matches the ``skip_domains`` list."""
    if not isinstance(url, str) or not url.strip():
        return False
    from urllib.parse import urlsplit

    try:
        host = (urlsplit(url.strip()).hostname or "").lower()
    except ValueError:
        return False
    return host_matches_skip_domains(host, domains)


def _escape_like_literal(text: str) -> str:
    """Escape ``\\``/``%``/``_`` so a domain matches literally under LIKE."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _skip_domain_like_patterns(domain: str) -> tuple[str, ...]:
    """LIKE patterns matching a bare domain's queued URLs.

    Covers the exact host (bare, with path/query/port) and any
    subdomain — the SQL equivalent of :func:`host_matches_skip_domains`.
    The leading ``%://`` binds the scheme; every pattern is a bound
    parameter (never interpolated), matched with ``ESCAPE '\\'``.
    """
    escaped = _escape_like_literal(domain)
    return (
        f"%://{escaped}",
        f"%://{escaped}/%",
        f"%://{escaped}?%",
        f"%://{escaped}:%",
        f"%://%.{escaped}",
        f"%://%.{escaped}/%",
        f"%://%.{escaped}?%",
        f"%://%.{escaped}:%",
    )


#: Coordination columns added to ``frontier_queue`` (also applied to
#: pre-existing databases via :func:`sqlite_frontier_coordination_migrations`).
_SQLITE_COORD_COLUMNS = (
    ("owner", "TEXT"),
    ("leased_at", "TEXT"),
    ("heartbeat_at", "TEXT"),
    ("lease_ttl_s", "INTEGER NOT NULL DEFAULT 120"),
    ("priority", "INTEGER NOT NULL DEFAULT 0"),
)


def sqlite_frontier_coordination_migrations() -> list[str]:
    """Idempotent ``ALTER TABLE`` statements for pre-existing SQLite DBs.

    ``CREATE TABLE IF NOT EXISTS`` only helps fresh databases; databases
    created before coordination need these columns/tables added.
    """
    stmts = [
        f"ALTER TABLE frontier_queue ADD COLUMN {name} {decl}"
        for name, decl in _SQLITE_COORD_COLUMNS
    ]
    stmts += [
        "CREATE TABLE IF NOT EXISTS crawl_workers ("
        " worker_id TEXT PRIMARY KEY,"
        " crawl_id TEXT NOT NULL DEFAULT 'default',"
        " claimed INTEGER NOT NULL DEFAULT 0,"
        " last_heartbeat TEXT NOT NULL"
        " DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),"
        " updated_at TEXT NOT NULL"
        " DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))"
        ")",
        "CREATE TABLE IF NOT EXISTS domain_politeness ("
        " host TEXT PRIMARY KEY,"
        " next_fetch_at REAL NOT NULL DEFAULT 0,"
        " delay_s REAL NOT NULL DEFAULT 0,"
        " updated_at TEXT NOT NULL"
        " DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))"
        ")",
        # Priority-first claim/pending ordering (claim ... ORDER BY
        # priority DESC, depth ASC, discovered_at ASC). Lives here rather
        # than SQLITE_FRONTIER_DDL because that script runs before the
        # coordination column backfill on legacy files, where `priority`
        # does not exist yet; _ensure_sqlite_coordination runs this after
        # the columns exist (IF NOT EXISTS keeps re-opens idempotent).
        "CREATE INDEX IF NOT EXISTS idx_frontier_status_priority"
        " ON frontier_queue(status, priority DESC, depth ASC, discovered_at ASC)",
        # Oldest-first eviction (evict_oldest ... ORDER BY discovered_at).
        # Same legacy-file reasoning as above: migrations run after the
        # coordination columns exist.
        "CREATE INDEX IF NOT EXISTS idx_frontier_queued_discovered"
        " ON frontier_queue(status, discovered_at)",
    ]
    return stmts


def _ensure_sqlite_coordination(conn: sqlite3.Connection) -> None:
    """Bring a pre-existing SQLite frontier DB up to the coordination schema.

    Adds missing ``frontier_queue`` columns (checked via ``PRAGMA
    table_info`` so re-runs are no-ops) and creates the workers /
    politeness tables. Fresh databases already match via
    ``SQLITE_FRONTIER_DDL``; this is the migration path for old files.
    """
    existing = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(frontier_queue)").fetchall()
    }
    for name, decl in _SQLITE_COORD_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE frontier_queue ADD COLUMN {name} {decl}")  # noqa: S608
    for stmt in sqlite_frontier_coordination_migrations()[len(_SQLITE_COORD_COLUMNS) :]:
        conn.execute(stmt)


#: SQLite DDL for the frontier tables (runs inside one executescript,
#: mirroring ``repository.py::_init_schema``).
SQLITE_FRONTIER_DDL = """
CREATE TABLE IF NOT EXISTS frontier_queue (
    url TEXT PRIMARY KEY,
    depth INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'queued',
    priority INTEGER NOT NULL DEFAULT 0,
    parent_url TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    etag TEXT,
    last_modified TEXT,
    discovered_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    fetched_at TEXT,
    owner TEXT,
    leased_at TEXT,
    heartbeat_at TEXT,
    lease_ttl_s INTEGER NOT NULL DEFAULT 120
);
CREATE TABLE IF NOT EXISTS crawl_workers (
    worker_id TEXT PRIMARY KEY,
    crawl_id TEXT NOT NULL DEFAULT 'default',
    claimed INTEGER NOT NULL DEFAULT 0,
    last_heartbeat TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE TABLE IF NOT EXISTS domain_politeness (
    host TEXT PRIMARY KEY,
    next_fetch_at REAL NOT NULL DEFAULT 0,
    delay_s REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE TABLE IF NOT EXISTS crawl_checkpoints (
    crawl_id TEXT PRIMARY KEY,
    seeds TEXT NOT NULL DEFAULT '[]',
    phase TEXT NOT NULL DEFAULT 'running',
    pages_attempted INTEGER NOT NULL DEFAULT 0,
    pages_indexed INTEGER NOT NULL DEFAULT 0,
    frontier_size INTEGER NOT NULL DEFAULT 0,
    stats TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE TABLE IF NOT EXISTS frontier_settings (
    id TEXT PRIMARY KEY,
    payload TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE TABLE IF NOT EXISTS crawl_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    decided_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_frontier_status_depth
ON frontier_queue(status, depth, discovered_at);
CREATE INDEX IF NOT EXISTS idx_crawl_requests_status
ON crawl_requests(status, id);
"""


def postgres_frontier_ddl(schema: str = "public") -> list[str]:
    """DDL statements for the frontier tables on Postgres.

    ``schema`` is the caller's already-validated identifier; every value
    below is static SQL (bound parameters carry the runtime values).
    """
    q = f'"{schema}"'
    return [
        f"CREATE TABLE IF NOT EXISTS {q}.frontier_queue ("
        "url TEXT PRIMARY KEY,"
        " depth INTEGER NOT NULL DEFAULT 0,"
        " status TEXT NOT NULL DEFAULT 'queued',"
        " priority INTEGER NOT NULL DEFAULT 0,"
        " parent_url TEXT NULL,"
        " attempts INTEGER NOT NULL DEFAULT 0,"
        " last_error TEXT NULL,"
        " etag TEXT NULL,"
        " last_modified TEXT NULL,"
        " discovered_at TIMESTAMPTZ NOT NULL DEFAULT now(),"
        " fetched_at TIMESTAMPTZ NULL,"
        " owner TEXT NULL,"
        " leased_at TIMESTAMPTZ NULL,"
        " heartbeat_at TIMESTAMPTZ NULL,"
        f" lease_ttl_s INTEGER NOT NULL DEFAULT {DEFAULT_LEASE_TTL_S}"
        ")",
        f"CREATE TABLE IF NOT EXISTS {q}.crawl_workers ("
        "worker_id TEXT PRIMARY KEY,"
        " crawl_id TEXT NOT NULL DEFAULT 'default',"
        " claimed INTEGER NOT NULL DEFAULT 0,"
        " last_heartbeat TIMESTAMPTZ NOT NULL DEFAULT now(),"
        " updated_at TIMESTAMPTZ NOT NULL DEFAULT now()"
        ")",
        f"CREATE TABLE IF NOT EXISTS {q}.domain_politeness ("
        "host TEXT PRIMARY KEY,"
        " next_fetch_at DOUBLE PRECISION NOT NULL DEFAULT 0,"
        " delay_s DOUBLE PRECISION NOT NULL DEFAULT 0,"
        " updated_at TIMESTAMPTZ NOT NULL DEFAULT now()"
        ")",
        f"CREATE TABLE IF NOT EXISTS {q}.crawl_checkpoints ("
        "crawl_id TEXT PRIMARY KEY,"
        " seeds JSONB NOT NULL DEFAULT '[]'::jsonb,"
        " phase TEXT NOT NULL DEFAULT 'running',"
        " pages_attempted INTEGER NOT NULL DEFAULT 0,"
        " pages_indexed INTEGER NOT NULL DEFAULT 0,"
        " frontier_size INTEGER NOT NULL DEFAULT 0,"
        " stats JSONB NOT NULL DEFAULT '{}'::jsonb,"
        " updated_at TIMESTAMPTZ NOT NULL DEFAULT now()"
        ")",
        f"CREATE TABLE IF NOT EXISTS {q}.frontier_settings ("
        "id TEXT PRIMARY KEY,"
        " payload JSONB NOT NULL DEFAULT '{}'::jsonb,"
        " updated_at TIMESTAMPTZ NOT NULL DEFAULT now()"
        ")",
        f"CREATE TABLE IF NOT EXISTS {q}.crawl_requests ("  # noqa: S608
        "id SERIAL PRIMARY KEY,"
        " url TEXT NOT NULL,"
        " status TEXT NOT NULL DEFAULT 'pending',"
        " created_at TIMESTAMPTZ NOT NULL DEFAULT now(),"
        " decided_at TIMESTAMPTZ NULL"
        ")",
        f"CREATE INDEX IF NOT EXISTS idx_frontier_status_depth"
        f" ON {q}.frontier_queue (status, depth, discovered_at)",
        f"CREATE INDEX IF NOT EXISTS idx_frontier_owner"
        f" ON {q}.frontier_queue (owner, status)",
        f"CREATE INDEX IF NOT EXISTS idx_frontier_status_priority"
        f" ON {q}.frontier_queue"
        " (status, priority DESC, depth ASC, discovered_at ASC)",
        f"CREATE INDEX IF NOT EXISTS idx_frontier_queued_discovered"
        f" ON {q}.frontier_queue (status, discovered_at)",
        f"CREATE INDEX IF NOT EXISTS idx_crawl_requests_status"  # noqa: S608
        f" ON {q}.crawl_requests (status, id)",
    ]


def postgres_frontier_coordination_migrations(
    schema: str = "public",
) -> list[str]:
    """Idempotent migrations for pre-existing Postgres deployments.

    Fresh databases match via :func:`postgres_frontier_ddl`; these
    ``ADD COLUMN IF NOT EXISTS`` / ``CREATE TABLE IF NOT EXISTS``
    statements bring older schemas forward. All values are static SQL.
    """
    q = f'"{schema}"'
    return [
        f"ALTER TABLE {q}.frontier_queue"  # noqa: S608
        " ADD COLUMN IF NOT EXISTS owner TEXT",
        f"ALTER TABLE {q}.frontier_queue"  # noqa: S608
        " ADD COLUMN IF NOT EXISTS leased_at TIMESTAMPTZ",
        f"ALTER TABLE {q}.frontier_queue"  # noqa: S608
        " ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ",
        f"ALTER TABLE {q}.frontier_queue"  # noqa: S608
        f" ADD COLUMN IF NOT EXISTS lease_ttl_s INTEGER NOT NULL"
        f" DEFAULT {DEFAULT_LEASE_TTL_S}",
        f"ALTER TABLE {q}.frontier_queue"  # noqa: S608
        " ADD COLUMN IF NOT EXISTS priority INTEGER NOT NULL DEFAULT 0",
        f"CREATE TABLE IF NOT EXISTS {q}.crawl_workers ("  # noqa: S608
        "worker_id TEXT PRIMARY KEY,"
        " crawl_id TEXT NOT NULL DEFAULT 'default',"
        " claimed INTEGER NOT NULL DEFAULT 0,"
        " last_heartbeat TIMESTAMPTZ NOT NULL DEFAULT now(),"
        " updated_at TIMESTAMPTZ NOT NULL DEFAULT now()"
        ")",
        f"CREATE TABLE IF NOT EXISTS {q}.domain_politeness ("  # noqa: S608
        "host TEXT PRIMARY KEY,"
        " next_fetch_at DOUBLE PRECISION NOT NULL DEFAULT 0,"
        " delay_s DOUBLE PRECISION NOT NULL DEFAULT 0,"
        " updated_at TIMESTAMPTZ NOT NULL DEFAULT now()"
        ")",
        f"CREATE TABLE IF NOT EXISTS {q}.crawl_requests ("  # noqa: S608
        "id SERIAL PRIMARY KEY,"
        " url TEXT NOT NULL,"
        " status TEXT NOT NULL DEFAULT 'pending',"
        " created_at TIMESTAMPTZ NOT NULL DEFAULT now(),"
        " decided_at TIMESTAMPTZ NULL"
        ")",
        f"CREATE INDEX IF NOT EXISTS idx_frontier_owner"  # noqa: S608
        f" ON {q}.frontier_queue (owner, status)",
        f"CREATE INDEX IF NOT EXISTS idx_frontier_status_priority"  # noqa: S608
        f" ON {q}.frontier_queue"
        " (status, priority DESC, depth ASC, discovered_at ASC)",
        f"CREATE INDEX IF NOT EXISTS idx_frontier_queued_discovered"  # noqa: S608
        f" ON {q}.frontier_queue (status, discovered_at)",
        f"CREATE INDEX IF NOT EXISTS idx_crawl_requests_status"  # noqa: S608
        f" ON {q}.crawl_requests (status, id)",
    ]


def ordered_postgres_frontier_schema(schema: str = "public") -> list[str]:
    """Correctly ordered Postgres frontier statements for ``_init_schema``.

    Tables first (fresh databases), then coordination migrations (older
    databases gain missing columns such as ``owner``), then indexes last
    (they reference migrated columns).

    Running the raw :func:`postgres_frontier_ddl` list before the
    migrations fails on pre-coordination databases with
    ``column "owner" does not exist`` from the ``idx_frontier_owner``
    index, because ``CREATE TABLE IF NOT EXISTS`` is a no-op there.
    """
    ddl = postgres_frontier_ddl(schema)
    tables = [s for s in ddl if s.lstrip().upper().startswith("CREATE TABLE")]
    indexes = [s for s in ddl if not s.lstrip().upper().startswith("CREATE TABLE")]
    return tables + postgres_frontier_coordination_migrations(schema) + indexes


def _request_from_row(row: Any) -> dict[str, Any]:
    """Normalize a ``crawl_requests`` row to a JSON-ready record."""

    def _get(key: str) -> Any:
        try:
            value = row.get(key) if isinstance(row, dict) else row[key]
        except (KeyError, IndexError, TypeError):
            return None
        return value

    def _iso(value: Any) -> str | None:
        if value is None:
            return None
        if hasattr(value, "isoformat"):
            try:
                return str(value.isoformat())
            except Exception:
                return None
        return str(value)

    raw_id = _get("id")
    try:
        request_id = int(raw_id) if raw_id is not None else None
    except (TypeError, ValueError):
        request_id = None
    return {
        "id": request_id,
        "url": str(_get("url") or ""),
        "status": str(_get("status") or CRAWL_REQUEST_PENDING),
        "created_at": _iso(_get("created_at")),
        "decided_at": _iso(_get("decided_at")),
    }


def _pending_branch_sql(
    table: str, param: str, status: str, extra_cols: str = "", tiebreak: str = ""
) -> str:
    """One index-probed page branch over a single queue status.

    ``WHERE status IN (...) ORDER BY ... LIMIT n`` scans the whole backlog
    once the queued set grows (planners correctly prefer a seq scan past
    ~25% selectivity), so each branch probes one status with an equality
    + full ORDER BY + LIMIT — a pure index walk. ``extra_cols`` carries
    the merge sort keys (e.g. ``discovered_at``) that are not part of the
    entry; ``tiebreak`` (e.g. SQLite's ``, rowid ASC``) keeps each
    backend's existing tie order.
    """
    cols = (
        "url, depth, status, parent_url, attempts,"
        f" last_error, etag, last_modified, priority{extra_cols}"
    )
    order = f"priority DESC, depth ASC, discovered_at ASC{tiebreak}"
    # `status` is one of the two internal literals passed by pending();
    # never caller input (same static-SQL convention as the module's
    # other queries).
    return (
        f"SELECT {cols} FROM {table} WHERE status = '{status}'"  # noqa: S608
        f" ORDER BY {order} LIMIT {param}"
    )


def _merge_pending(
    pages: list[list[tuple[tuple[Any, ...], FrontierEntry]]],
    limit: int,
    offset: int,
) -> list[FrontierEntry]:
    """Merge per-status branch pages into one global page.

    Each branch holds its status's top ``limit + offset`` rows; a row
    inside the global top ``limit + offset`` is inside its own status's
    top ``limit + offset``, so slicing the merged sort is exact.
    """
    merged = sorted((item for page in pages for item in page), key=lambda item: item[0])
    return [entry for _, entry in merged[offset : offset + limit]]


def _entry_from_row(row: Any) -> FrontierEntry:
    def _get(key: str, default: Any = None) -> Any:
        try:
            value = row.get(key) if isinstance(row, dict) else row[key]
        except (KeyError, IndexError, TypeError):
            return default
        return default if value is None and default is not None else value

    lease_ttl_raw = _get("lease_ttl_s", DEFAULT_LEASE_TTL_S)
    try:
        lease_ttl = int(lease_ttl_raw or DEFAULT_LEASE_TTL_S)
    except (TypeError, ValueError):
        lease_ttl = DEFAULT_LEASE_TTL_S
    owner = _get("owner")
    return FrontierEntry(
        url=str(_get("url")),
        depth=int(_get("depth") or 0),
        status=str(_get("status") or STATUS_QUEUED),
        parent_url=_get("parent_url"),
        priority=int(_get("priority") or 0),
        attempts=int(_get("attempts") or 0),
        last_error=_get("last_error"),
        etag=_get("etag"),
        last_modified=_get("last_modified"),
        owner=str(owner) if owner is not None else None,
        leased_at=_get("leased_at"),
        heartbeat_at=_get("heartbeat_at"),
        lease_ttl_s=lease_ttl,
    )


_CLAIM_COLUMNS = (
    "url, depth, status, parent_url, attempts,"
    " last_error, etag, last_modified, priority,"
    " owner, leased_at, heartbeat_at, lease_ttl_s"
)


def _checkpoint_from_parts(
    crawl_id: str,
    seeds_raw: Any,
    phase: Any,
    pages_attempted: Any,
    pages_indexed: Any,
    frontier_size: Any,
    stats_raw: Any,
    updated_at: Any,
) -> CrawlCheckpoint:
    try:
        seeds = json.loads(seeds_raw) if isinstance(seeds_raw, str) else seeds_raw
    except (ValueError, TypeError):
        seeds = []
    try:
        stats = json.loads(stats_raw) if isinstance(stats_raw, str) else stats_raw
    except (ValueError, TypeError):
        stats = {}
    return CrawlCheckpoint(
        crawl_id=str(crawl_id),
        seeds=list(seeds) if isinstance(seeds, list) else [],
        phase=str(phase) if phase in _VALID_PHASES else PHASE_RUNNING,
        pages_attempted=int(pages_attempted or 0),
        pages_indexed=int(pages_indexed or 0),
        frontier_size=int(frontier_size or 0),
        stats=dict(stats) if isinstance(stats, dict) else {},
        updated_at=str(updated_at or ""),
    )


class SqliteFrontierStore(FrontierStore):
    """SQLite-backed frontier (zero-dependency reference)."""

    def __init__(self, db_path: str = ":memory:") -> None:
        self._lock = threading.Lock()
        self._closed = False
        if db_path != ":memory:":
            # Bare runs (docker without the data volume, fresh checkouts)
            # may lack the parent dir; sqlite creates the file but never
            # its parents. Mirrors SqliteAppConfigStore.
            parent = os.path.dirname(os.path.abspath(db_path))
            if parent:
                with contextlib.suppress(OSError):
                    os.makedirs(parent, exist_ok=True)
        try:
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            with self._lock:
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")  # noqa: S608
                self._conn.executescript(SQLITE_FRONTIER_DDL)
                _ensure_sqlite_coordination(self._conn)
                self._conn.commit()
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to open frontier store: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def enqueue(
        self,
        url: str,
        depth: int = 0,
        parent_url: str | None = None,
        priority: int | None = None,
    ) -> bool:
        if not isinstance(url, str) or not url.strip():
            return False
        norm = normalize_frontier_url(url)
        if depth is None or not isinstance(depth, int) or depth < 0:
            depth = 0
        prio = normalize_priority(priority)
        try:
            with self._lock, self._conn:
                cursor = self._conn.execute(
                    "INSERT OR IGNORE INTO frontier_queue"
                    " (url, depth, status, parent_url, priority)"
                    " VALUES (?, ?, 'queued', ?, ?)",
                    (norm, depth, parent_url, prio if prio is not None else 0),
                )
                if cursor.rowcount == 1:
                    return True
                if prio is not None:
                    self._conn.execute(
                        "UPDATE frontier_queue SET priority = ? WHERE url = ?",
                        (prio, norm),
                    )
                return False
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to enqueue frontier URL: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def dequeue(self) -> FrontierEntry | None:
        try:
            with self._lock, self._conn:
                row = self._conn.execute(
                    "SELECT url, depth, status, parent_url, attempts,"
                    " last_error, etag, last_modified, priority FROM frontier_queue"
                    " WHERE status = 'queued'"
                    " ORDER BY priority DESC, depth ASC,"
                    " discovered_at ASC, rowid ASC LIMIT 1"
                ).fetchone()
                if row is None:
                    return None
                entry = _entry_from_row(row)
                self._conn.execute(
                    "UPDATE frontier_queue SET status = 'active' WHERE url = ?",
                    (entry.url,),
                )
                entry.status = STATUS_ACTIVE
                return entry
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to dequeue frontier URL: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def mark_done(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> None:
        norm = normalize_frontier_url(url)
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "UPDATE frontier_queue SET status = 'done',"
                    " etag = COALESCE(?, etag),"
                    " last_modified = COALESCE(?, last_modified),"
                    " fetched_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')"
                    " WHERE url = ?",
                    (etag, last_modified, norm),
                )
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to mark frontier URL done: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def mark_failed(self, url: str, error: str) -> None:
        norm = normalize_frontier_url(url)
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "UPDATE frontier_queue SET status = 'failed',"
                    " attempts = attempts + 1, last_error = ? WHERE url = ?",
                    (error[:500], norm),
                )
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to mark frontier URL failed: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def mark_skipped(self, url: str) -> int:
        """Mark one URL skipped (single-row form of eviction)."""
        norm = normalize_frontier_url(url)
        try:
            with self._lock, self._conn:
                cursor = self._conn.execute(
                    "UPDATE frontier_queue SET status = 'skipped' WHERE url = ?",
                    (norm,),
                )
                return cursor.rowcount
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to skip frontier URL: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def skip_queued_domains(self, domains: Any) -> int:
        """Bulk-skip ``queued`` rows from ``domains`` (exact+subdomain).

        One ``UPDATE`` per normalized domain (malformed entries ignored,
        never fatal); each moved row carries a ``last_error`` reason
        naming its domain. Returns the total number of rows moved.
        """
        targets = normalize_skip_domains(domains)
        if not targets:
            return 0
        total = 0
        try:
            with self._lock, self._conn:
                for domain in targets:
                    patterns = _skip_domain_like_patterns(domain)
                    condition = " OR ".join(["url LIKE ? ESCAPE '\\'"] * len(patterns))
                    statement = (
                        "UPDATE frontier_queue SET status = 'skipped',"  # noqa: S608
                        " last_error = ? WHERE status = 'queued'"
                        f" AND ({condition})"
                    )
                    cursor = self._conn.execute(
                        statement,
                        (f"skipped: domain {domain}", *patterns),
                    )
                    total += cursor.rowcount
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to skip queued domains: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc
        return total

    def evict_oldest(self, limit: int) -> int:
        """Drop oldest queued rows past ``limit`` (oldest-first eviction).

        Single-statement eviction: one ``COUNT`` sizes the overflow, then
        one ``UPDATE ... WHERE url IN (SELECT ... ORDER BY ... LIMIT ?)``
        moves the victims. Victims match the old per-row loop exactly
        (oldest ``queued`` rows by ``discovered_at``, ``rowid`` tiebreak).
        """
        cap = max(0, limit)
        try:
            with self._lock, self._conn:
                count_row = self._conn.execute(
                    "SELECT COUNT(*) FROM frontier_queue WHERE status = 'queued'"
                ).fetchone()
                overflow = int(count_row[0]) - cap
                if overflow <= 0:
                    return 0
                cursor = self._conn.execute(
                    "UPDATE frontier_queue SET status = 'skipped',"
                    " last_error = 'evicted: queue cap exceeded'"
                    " WHERE url IN (SELECT url FROM frontier_queue"
                    " WHERE status = 'queued'"
                    " ORDER BY discovered_at ASC, rowid ASC LIMIT ?)",
                    (overflow,),
                )
                return cursor.rowcount
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to evict frontier URLs: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def requeue_active(self) -> int:
        try:
            with self._lock, self._conn:
                cursor = self._conn.execute(
                    "UPDATE frontier_queue SET status = 'queued'"
                    " WHERE status = 'active'"
                )
                return cursor.rowcount
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to requeue active frontier URLs: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def requeue(self, url: str) -> bool:
        try:
            with self._lock, self._conn:
                cursor = self._conn.execute(
                    "UPDATE frontier_queue SET status = 'queued'"
                    " WHERE url = ? AND status IN ('active', 'failed')",
                    (normalize_frontier_url(url),),
                )
                return cursor.rowcount == 1
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to requeue frontier URL: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def requeue_stale(self, max_age_days: int, limit: int) -> int:
        if (
            not isinstance(max_age_days, int)
            or isinstance(max_age_days, bool)
            or max_age_days < 0
            or not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit <= 0
        ):
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        try:
            with self._lock, self._conn:
                cursor = self._conn.execute(
                    "UPDATE frontier_queue SET status = 'queued',"
                    " attempts = 0, owner = NULL, leased_at = NULL,"
                    " heartbeat_at = NULL, last_error = NULL"
                    " WHERE url IN (SELECT url FROM frontier_queue"
                    " WHERE status = 'done'"
                    " AND (fetched_at IS NULL OR fetched_at < ?)"
                    " ORDER BY fetched_at ASC LIMIT ?)",
                    (cutoff, limit),
                )
                return cursor.rowcount
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to requeue stale frontier URLs: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def clear_queued(self) -> int:
        try:
            with self._lock, self._conn:
                cursor = self._conn.execute(
                    "DELETE FROM frontier_queue WHERE status = 'queued'"
                )
                return cursor.rowcount
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to clear queued frontier URLs: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    # -- shared-queue coordination (SQLite) ------------------------------
    # NOTE: the threading lock serializes claims inside one process.
    # SQLite has no inter-process row lock, so multi-PROCESS workers must
    # use the Postgres backend; same-file multi-threaded workers and
    # multi-connection tests share state correctly through the file.

    def claim(
        self,
        worker_id: str,
        *,
        lease_ttl_s: int = DEFAULT_LEASE_TTL_S,
    ) -> FrontierEntry | None:
        if not worker_id or not worker_id.strip():
            raise ProviderError(
                "claim requires a non-empty worker_id",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.INVALID_REQUEST,
            )
        owner = worker_id.strip()
        ttl = max(1, int(lease_ttl_s or DEFAULT_LEASE_TTL_S))
        try:
            with self._lock, self._conn:
                row = self._conn.execute(
                    "SELECT " + _CLAIM_COLUMNS + " FROM frontier_queue"  # noqa: S608
                    " WHERE status = 'queued'"
                    " ORDER BY priority DESC, depth ASC,"
                    " discovered_at ASC, rowid ASC LIMIT 1"
                ).fetchone()
                if row is None:
                    cutoff = _cutoff_iso(ttl)
                    row = self._conn.execute(
                        "SELECT " + _CLAIM_COLUMNS + " FROM frontier_queue"  # noqa: S608
                        " WHERE status = 'active'"
                        " AND (heartbeat_at IS NULL OR heartbeat_at < ?)"
                        " ORDER BY priority DESC, depth ASC,"
                        " discovered_at ASC, rowid ASC LIMIT 1",
                        (cutoff,),
                    ).fetchone()
                    if row is None:
                        return None
                entry = _entry_from_row(row)
                stamp = _utcnow_precise()
                self._conn.execute(
                    "UPDATE frontier_queue SET status = 'active', owner = ?,"
                    " leased_at = ?, heartbeat_at = ?, lease_ttl_s = ?"
                    " WHERE url = ?",
                    (owner, stamp, stamp, ttl, entry.url),
                )
                entry.status = STATUS_ACTIVE
                entry.owner = owner
                entry.leased_at = stamp
                entry.heartbeat_at = stamp
                entry.lease_ttl_s = ttl
                return entry
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to claim frontier URL: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def heartbeat(
        self,
        worker_id: str,
        *,
        lease_ttl_s: int = DEFAULT_LEASE_TTL_S,
    ) -> int:
        try:
            with self._lock, self._conn:
                cursor = self._conn.execute(
                    "UPDATE frontier_queue SET heartbeat_at = ?,"
                    " lease_ttl_s = ? WHERE status = 'active' AND owner = ?",
                    (
                        _utcnow_precise(),
                        max(1, int(lease_ttl_s or DEFAULT_LEASE_TTL_S)),
                        worker_id,
                    ),
                )
                return cursor.rowcount
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to heartbeat frontier lease: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def reap_expired_leases(
        self,
        *,
        max_age_s: float = DEFAULT_LEASE_TTL_S,
        now: float | None = None,
    ) -> int:
        cutoff = _cutoff_iso(max(0, max_age_s), now)
        try:
            with self._lock, self._conn:
                cursor = self._conn.execute(
                    "UPDATE frontier_queue SET status = 'queued', owner = NULL"
                    " WHERE status = 'active'"
                    " AND (heartbeat_at IS NULL OR heartbeat_at < ?)",
                    (cutoff,),
                )
                return cursor.rowcount
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to reap expired frontier leases: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def requeue_owned(self, worker_id: str) -> int:
        try:
            with self._lock, self._conn:
                cursor = self._conn.execute(
                    "UPDATE frontier_queue SET status = 'queued', owner = NULL"
                    " WHERE status = 'active' AND owner = ?",
                    (worker_id,),
                )
                return cursor.rowcount
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to requeue owned frontier URLs: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def owned(self, worker_id: str) -> list[FrontierEntry]:
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT " + _CLAIM_COLUMNS + " FROM frontier_queue"  # noqa: S608
                    " WHERE status = 'active' AND owner = ?"
                    " ORDER BY depth ASC, discovered_at ASC, rowid ASC",
                    (worker_id,),
                ).fetchall()
                return [_entry_from_row(row) for row in rows]
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to list owned frontier URLs: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def reserve_politeness(
        self,
        host: str,
        delay_s: float,
        *,
        now: float | None = None,
    ) -> float:
        key = (host or "").strip().lower()
        if not key:
            return 0.0
        delay = max(0.0, float(delay_s or 0.0))
        moment = now if now is not None else time.time()
        try:
            with self._lock, self._conn:
                row = self._conn.execute(
                    "SELECT next_fetch_at, delay_s FROM domain_politeness"
                    " WHERE host = ?",
                    (key,),
                ).fetchone()
                current = float(row["next_fetch_at"] or 0.0) if row else 0.0
                wait = max(0.0, current - moment)
                self._conn.execute(
                    "INSERT INTO domain_politeness (host, next_fetch_at,"
                    " delay_s, updated_at) VALUES (?, ?, ?, ?)"
                    " ON CONFLICT(host) DO UPDATE SET"
                    " next_fetch_at = excluded.next_fetch_at,"
                    " delay_s = excluded.delay_s,"
                    " updated_at = excluded.updated_at",
                    (key, max(moment, current) + delay, delay, _utcnow()),
                )
                return wait
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to reserve politeness slot: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def get_politeness_delay(self, host: str) -> float:
        key = (host or "").strip().lower()
        if not key:
            return 0.0
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT delay_s FROM domain_politeness WHERE host = ?",
                    (key,),
                ).fetchone()
                return float(row["delay_s"] or 0.0) if row else 0.0
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to read politeness delay: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def register_worker(
        self,
        worker_id: str,
        crawl_id: str = DEFAULT_CRAWL_ID,
    ) -> None:
        self.heartbeat_worker(worker_id, crawl_id, claimed=0)

    def heartbeat_worker(
        self,
        worker_id: str,
        crawl_id: str = DEFAULT_CRAWL_ID,
        *,
        claimed: int = 0,
    ) -> None:
        if not worker_id or not worker_id.strip():
            return
        stamp = _utcnow_precise()
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "INSERT INTO crawl_workers (worker_id, crawl_id, claimed,"
                    " last_heartbeat, updated_at)"
                    " VALUES (?, ?, ?, ?, ?)"
                    " ON CONFLICT(worker_id) DO UPDATE SET"
                    " crawl_id = excluded.crawl_id,"
                    " claimed = excluded.claimed,"
                    " last_heartbeat = excluded.last_heartbeat,"
                    " updated_at = excluded.updated_at",
                    (
                        worker_id.strip(),
                        crawl_id,
                        max(0, int(claimed or 0)),
                        stamp,
                        stamp,
                    ),
                )
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to heartbeat worker: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def list_workers(self, crawl_id: str | None = None) -> list[dict[str, Any]]:
        try:
            with self._lock:
                if crawl_id is None:
                    rows = self._conn.execute(
                        "SELECT worker_id, crawl_id, claimed, last_heartbeat,"
                        " updated_at FROM crawl_workers"
                        " ORDER BY last_heartbeat DESC"
                    ).fetchall()
                else:
                    rows = self._conn.execute(
                        "SELECT worker_id, crawl_id, claimed, last_heartbeat,"
                        " updated_at FROM crawl_workers WHERE crawl_id = ?"
                        " ORDER BY last_heartbeat DESC",
                        (crawl_id,),
                    ).fetchall()
                return [
                    {
                        "worker_id": str(row["worker_id"]),
                        "crawl_id": str(row["crawl_id"]),
                        "claimed": int(row["claimed"] or 0),
                        "last_heartbeat": str(row["last_heartbeat"] or ""),
                        "updated_at": str(row["updated_at"] or ""),
                    }
                    for row in rows
                ]
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to list workers: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def prune_workers(
        self,
        *,
        max_age_s: float = DEFAULT_LEASE_TTL_S * 2,
        now: float | None = None,
    ) -> int:
        cutoff = _cutoff_iso(max(0, max_age_s), now)
        try:
            with self._lock, self._conn:
                cursor = self._conn.execute(
                    "DELETE FROM crawl_workers WHERE last_heartbeat < ?",
                    (cutoff,),
                )
                return cursor.rowcount
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to prune workers: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def increment_counters(
        self,
        crawl_id: str = DEFAULT_CRAWL_ID,
        *,
        pages_attempted: int = 0,
        pages_indexed: int = 0,
    ) -> CrawlCheckpoint:
        attempted = max(0, int(pages_attempted or 0))
        indexed = max(0, int(pages_indexed or 0))
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "INSERT INTO crawl_checkpoints (crawl_id, seeds, phase,"
                    " pages_attempted, pages_indexed, frontier_size, stats,"
                    " updated_at) VALUES (?, '[]', 'running', ?, ?, 0, '{}', ?)"
                    " ON CONFLICT(crawl_id) DO UPDATE SET"
                    " pages_attempted = crawl_checkpoints.pages_attempted"
                    " + excluded.pages_attempted,"
                    " pages_indexed = crawl_checkpoints.pages_indexed"
                    " + excluded.pages_indexed,"
                    " updated_at = excluded.updated_at",
                    (crawl_id, attempted, indexed, _utcnow()),
                )
                row = self._conn.execute(
                    "SELECT crawl_id, seeds, phase, pages_attempted,"
                    " pages_indexed, frontier_size, stats, updated_at"
                    " FROM crawl_checkpoints WHERE crawl_id = ?",
                    (crawl_id,),
                ).fetchone()
                if row is None:
                    raise ProviderError(
                        "failed to increment crawl counters: missing row",
                        provider_name=_PROVIDER_NAME,
                        code=ProviderError.UNAVAILABLE,
                    )
                return _checkpoint_from_parts(
                    row["crawl_id"],
                    row["seeds"],
                    row["phase"],
                    row["pages_attempted"],
                    row["pages_indexed"],
                    row["frontier_size"],
                    row["stats"],
                    row["updated_at"],
                )
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to increment crawl counters: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def is_known(self, url: str) -> bool:
        try:
            norm = normalize_frontier_url(url)
        except (AttributeError, ValueError):
            return False
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT 1 AS found FROM frontier_queue WHERE url = ? LIMIT 1",
                    (norm,),
                ).fetchone()
                return row is not None
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to look up frontier URL: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def status_of(self, url: str) -> str | None:
        try:
            norm = normalize_frontier_url(url)
        except (AttributeError, ValueError):
            return None
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT status FROM frontier_queue WHERE url = ? LIMIT 1",
                    (norm,),
                ).fetchone()
                return str(row["status"]) if row is not None else None
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to look up frontier URL status: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def statuses_of(self, urls: Iterable[str]) -> dict[str, str]:
        norms: list[str] = []
        for url in urls or []:
            try:
                norm = normalize_frontier_url(url)
            except (AttributeError, ValueError):
                continue
            if norm not in norms:
                norms.append(norm)
        out: dict[str, str] = {}
        try:
            with self._lock:
                for start in range(0, len(norms), 500):
                    chunk = norms[start : start + 500]
                    marks = ",".join("?" for _ in chunk)
                    rows = self._conn.execute(
                        "SELECT url, status FROM frontier_queue"  # noqa: S608
                        f" WHERE url IN ({marks})",
                        tuple(chunk),
                    ).fetchall()
                    for row in rows:
                        out[str(row["url"])] = str(row["status"])
                return out
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to look up frontier URL statuses: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def reseed(self, url: str) -> bool:
        try:
            norm = normalize_frontier_url(url)
        except (AttributeError, ValueError):
            return False
        try:
            with self._lock, self._conn:
                cursor = self._conn.execute(
                    "UPDATE frontier_queue SET status = 'queued',"
                    " attempts = 0, last_error = NULL"
                    " WHERE url = ? AND status IN ('failed', 'skipped', 'done')",
                    (norm,),
                )
                return cursor.rowcount > 0
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to reseed frontier URL: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def reseed_many(self, urls: Iterable[str]) -> int:
        norms: list[str] = []
        for url in urls or []:
            try:
                norm = normalize_frontier_url(url)
            except (AttributeError, ValueError):
                continue
            if norm not in norms:
                norms.append(norm)
        if not norms:
            return 0
        try:
            revived = 0
            with self._lock, self._conn:
                for start in range(0, len(norms), 500):
                    chunk = norms[start : start + 500]
                    marks = ",".join("?" for _ in chunk)
                    cursor = self._conn.execute(
                        "UPDATE frontier_queue SET status = 'queued',"  # noqa: S608
                        " attempts = 0, last_error = NULL"
                        f" WHERE url IN ({marks})"
                        " AND status IN ('failed', 'skipped', 'done')",
                        tuple(chunk),
                    )
                    revived += cursor.rowcount or 0
                return revived
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to reseed frontier URLs: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def pending(self, limit: int = 500, offset: int = 0) -> list[FrontierEntry]:
        capped = max(0, limit)
        skipped = max(0, offset)
        want = capped + skipped
        try:
            with self._lock:
                pages: list[list[tuple[tuple[Any, ...], FrontierEntry]]] = []
                for status in ("queued", "active"):
                    rows = self._conn.execute(
                        _pending_branch_sql(
                            "frontier_queue",
                            "?",
                            status,
                            extra_cols=", discovered_at, rowid",
                            tiebreak=", rowid ASC",
                        ),
                        (want,),
                    ).fetchall()
                    pages.append(
                        [
                            (
                                (
                                    -(row["priority"] or 0),
                                    row["depth"] or 0,
                                    row["discovered_at"],
                                    row["rowid"],
                                ),
                                _entry_from_row(row),
                            )
                            for row in rows
                        ]
                    )
                return _merge_pending(pages, capped, skipped)
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to list pending frontier URLs: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def failed(self, limit: int = 100, offset: int = 0) -> list[FrontierEntry]:
        capped = max(0, limit)
        skipped = max(0, offset)
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT url, depth, status, parent_url, attempts,"
                    " last_error, etag, last_modified, priority"
                    " FROM frontier_queue WHERE status = 'failed'"
                    " ORDER BY attempts DESC, rowid DESC LIMIT ? OFFSET ?",
                    (capped, skipped),
                ).fetchall()
                return [_entry_from_row(row) for row in rows]
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to list failed frontier URLs: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def failed_domains(
        self, limit: int = 100, offset: int = 0
    ) -> list[tuple[str, int, str]]:
        """Aggregate ``failed`` rows by host with the top reason each.

        Host parsing is pure SQL (``://`` split, cut at first ``/`` or
        ``?``, lowercased); non-http(s) rows bucket as ``(invalid)``.
        The per-host reason is the mode of ``last_error`` (ties break
        alphabetically). Values are bound; only fixed fragments
        interpolate.
        """
        capped = max(0, int(limit or 0))
        skipped = max(0, int(offset or 0))
        inner = (
            f"SELECT {FAILED_DOMAIN_HOST_EXPR_SQLITE} AS domain,"
            " COALESCE(last_error, '') AS reason"
            f" {_FAILED_DOMAIN_HOST_FROM_SQLITE}"
        )
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT t.domain, t.failed, r.reason AS top_reason"  # noqa: S608
                    f" FROM (SELECT domain, COUNT(*) AS failed FROM ({inner})"
                    " GROUP BY domain) AS t JOIN (SELECT domain, reason,"
                    " ROW_NUMBER() OVER (PARTITION BY domain"
                    " ORDER BY COUNT(*) DESC, reason ASC) AS rn"
                    f" FROM ({inner}) GROUP BY domain, reason) AS r"
                    " ON r.domain = t.domain AND r.rn = 1"
                    " ORDER BY t.failed DESC, t.domain ASC"
                    " LIMIT ? OFFSET ?",
                    (capped, skipped),
                ).fetchall()
                return [
                    (str(row["domain"]), int(row["failed"]), str(row["top_reason"]))
                    for row in rows
                ]
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to aggregate failed domains: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def failed_domain_count(self) -> int:
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM (SELECT DISTINCT"  # noqa: S608
                    f" {FAILED_DOMAIN_HOST_EXPR_SQLITE} AS domain"
                    f" {_FAILED_DOMAIN_HOST_FROM_SQLITE})"
                ).fetchone()
                return int(row["n"]) if row is not None else 0
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to count failed domains: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def requeue_failed_domains(self, domains: Any) -> int:
        """Revive ``failed`` rows from ``domains`` (exact+subdomain).

        One ``UPDATE`` per normalized domain (malformed entries
        ignored, never fatal); attempts reset to 0 with owner/lease
        fields and the error cleared, mirroring :meth:`reseed`.
        """
        targets = normalize_skip_domains(domains)
        if not targets:
            return 0
        total = 0
        try:
            with self._lock, self._conn:
                for domain in targets:
                    patterns = _skip_domain_like_patterns(domain)
                    condition = " OR ".join(["url LIKE ? ESCAPE '\\'"] * len(patterns))
                    statement = (
                        "UPDATE frontier_queue SET status = 'queued',"  # noqa: S608
                        " attempts = 0, last_error = NULL"
                        " WHERE status = 'failed'"
                        f" AND ({condition})"
                    )
                    cursor = self._conn.execute(statement, (*patterns,))
                    total += cursor.rowcount
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to requeue failed domains: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc
        return total

    def purge_failed_domains(self, domains: Any) -> int:
        """Delete ``failed`` rows from ``domains`` (exact+subdomain).

        One ``DELETE`` per normalized domain (malformed entries
        ignored, never fatal). Queued/active/done rows are untouched.
        """
        targets = normalize_skip_domains(domains)
        if not targets:
            return 0
        total = 0
        try:
            with self._lock, self._conn:
                for domain in targets:
                    patterns = _skip_domain_like_patterns(domain)
                    condition = " OR ".join(["url LIKE ? ESCAPE '\\'"] * len(patterns))
                    cursor = self._conn.execute(
                        "DELETE FROM frontier_queue"  # noqa: S608
                        f" WHERE status = 'failed' AND ({condition})",
                        (*patterns,),
                    )
                    total += cursor.rowcount
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to purge failed domains: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc
        return total

    def queued_domains(
        self, limit: int = 100, offset: int = 0
    ) -> list[tuple[str, int]]:
        """Aggregate ``queued`` rows by host (SQLite mirror of failed)."""
        capped = max(0, int(limit or 0))
        skipped = max(0, int(offset or 0))
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT domain, COUNT(*) AS queued"  # noqa: S608
                    f" FROM (SELECT {FAILED_DOMAIN_HOST_EXPR_SQLITE} AS domain"
                    f" {_QUEUED_DOMAIN_HOST_FROM_SQLITE})"
                    " GROUP BY domain ORDER BY queued DESC, domain ASC"
                    " LIMIT ? OFFSET ?",
                    (capped, skipped),
                ).fetchall()
                return [(str(row["domain"]), int(row["queued"])) for row in rows]
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to aggregate queued domains: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def queued_domain_count(self) -> int:
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM (SELECT DISTINCT"  # noqa: S608
                    f" {FAILED_DOMAIN_HOST_EXPR_SQLITE} AS domain"
                    f" {_QUEUED_DOMAIN_HOST_FROM_SQLITE})"
                ).fetchone()
                return int(row["n"]) if row is not None else 0
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to count queued domains: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def bump_queued_priority(self, domains: Any, delta: int = 1) -> int:
        """Shift ``queued`` rows' priority by ``delta`` (exact+subdomain)."""
        targets = normalize_skip_domains(domains)
        step = int(delta or 0)
        if not targets or step == 0:
            return 0
        total = 0
        try:
            with self._lock, self._conn:
                for domain in targets:
                    patterns = _skip_domain_like_patterns(domain)
                    condition = " OR ".join(["url LIKE ? ESCAPE '\\'"] * len(patterns))
                    cursor = self._conn.execute(
                        "UPDATE frontier_queue SET priority = priority + ?"  # noqa: S608
                        f" WHERE status = 'queued' AND ({condition})",
                        (step, *patterns),
                    )
                    total += cursor.rowcount
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to bump queued priority: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc
        return total

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {"total": 0}
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT status, COUNT(*) AS n FROM frontier_queue GROUP BY status"
                ).fetchall()
                for row in rows:
                    out[str(row["status"])] = int(row["n"])
                    out["total"] += int(row["n"])
                return out
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to count frontier URLs: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def submit_crawl_request(self, url: str) -> dict[str, Any]:
        if not isinstance(url, str) or not url.strip():
            raise ProviderError(
                "crawl request URL must not be blank",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.INVALID_REQUEST,
            )
        norm = normalize_frontier_url(url)
        try:
            with self._lock, self._conn:
                existing = self._conn.execute(
                    "SELECT id, url, status, created_at, decided_at"
                    " FROM crawl_requests WHERE url = ? AND status = 'pending'"
                    " ORDER BY id DESC LIMIT 1",
                    (norm,),
                ).fetchone()
                if existing is not None:
                    return _request_from_row(existing)
                cursor = self._conn.execute(
                    "INSERT INTO crawl_requests (url, status) VALUES (?, 'pending')",
                    (norm,),
                )
                row = self._conn.execute(
                    "SELECT id, url, status, created_at, decided_at"
                    " FROM crawl_requests WHERE id = ?",
                    (cursor.lastrowid,),
                ).fetchone()
                return _request_from_row(row)
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to submit crawl request: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def list_crawl_requests(
        self, status: str | None = None, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        base = (
            "SELECT id, url, status, created_at, decided_at"
            " FROM crawl_requests ORDER BY id DESC LIMIT ? OFFSET ?"
        )
        filtered = (
            "SELECT id, url, status, created_at, decided_at"
            " FROM crawl_requests WHERE status = ?"
            " ORDER BY id DESC LIMIT ? OFFSET ?"
        )
        try:
            with self._lock:
                if status is not None:
                    rows = self._conn.execute(
                        filtered, (status, max(0, limit), max(0, offset))
                    ).fetchall()
                else:
                    rows = self._conn.execute(
                        base, (max(0, limit), max(0, offset))
                    ).fetchall()
                return [_request_from_row(row) for row in rows]
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to list crawl requests: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def set_crawl_request_status(self, request_id: int, status: str) -> bool:
        if status not in VALID_CRAWL_REQUEST_DECISIONS:
            return False
        try:
            request_id = int(request_id)
        except (TypeError, ValueError):
            return False
        try:
            with self._lock, self._conn:
                cursor = self._conn.execute(
                    "UPDATE crawl_requests SET status = ?,"
                    " decided_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')"
                    " WHERE id = ? AND status = 'pending'",
                    (status, request_id),
                )
                return cursor.rowcount > 0
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to decide crawl request: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def validators(self) -> dict[str, dict[str, str]]:
        """Return stored ETag/Last-Modified validators keyed by URL."""
        out: dict[str, dict[str, str]] = {}
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT url, etag, last_modified FROM frontier_queue"
                    " WHERE etag IS NOT NULL OR last_modified IS NOT NULL"
                ).fetchall()
                for row in rows:
                    entry: dict[str, str] = {}
                    if row["etag"]:
                        entry["etag"] = str(row["etag"])
                    if row["last_modified"]:
                        entry["last_modified"] = str(row["last_modified"])
                    if entry:
                        out[str(row["url"])] = entry
                return out
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to read frontier validators: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def save_checkpoint(self, checkpoint: CrawlCheckpoint) -> None:
        phase = checkpoint.phase if checkpoint.phase in _VALID_PHASES else PHASE_RUNNING
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "INSERT INTO crawl_checkpoints (crawl_id, seeds, phase,"
                    " pages_attempted, pages_indexed, frontier_size, stats,"
                    " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(crawl_id) DO UPDATE SET seeds = excluded.seeds,"
                    " phase = excluded.phase,"
                    " pages_attempted = excluded.pages_attempted,"
                    " pages_indexed = excluded.pages_indexed,"
                    " frontier_size = excluded.frontier_size,"
                    " stats = excluded.stats, updated_at = excluded.updated_at",
                    (
                        checkpoint.crawl_id,
                        json.dumps(list(checkpoint.seeds)),
                        phase,
                        checkpoint.pages_attempted,
                        checkpoint.pages_indexed,
                        checkpoint.frontier_size,
                        json.dumps(dict(checkpoint.stats)),
                        checkpoint.updated_at or _utcnow(),
                    ),
                )
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to save crawl checkpoint: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def load_checkpoint(
        self, crawl_id: str = DEFAULT_CRAWL_ID
    ) -> CrawlCheckpoint | None:
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT crawl_id, seeds, phase, pages_attempted,"
                    " pages_indexed, frontier_size, stats, updated_at"
                    " FROM crawl_checkpoints WHERE crawl_id = ?",
                    (crawl_id,),
                ).fetchone()
                if row is None:
                    return None
                return _checkpoint_from_parts(
                    row["crawl_id"],
                    row["seeds"],
                    row["phase"],
                    row["pages_attempted"],
                    row["pages_indexed"],
                    row["frontier_size"],
                    row["stats"],
                    row["updated_at"],
                )
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to load crawl checkpoint: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def get_settings(self, settings_id: str = DEFAULT_SETTINGS_ID) -> dict[str, Any]:
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT payload FROM frontier_settings WHERE id = ?",
                    (settings_id,),
                ).fetchone()
                if row is None:
                    return {}
                try:
                    payload = json.loads(row["payload"] or "{}")
                except (ValueError, TypeError):
                    return {}
                return dict(payload) if isinstance(payload, dict) else {}
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to read frontier settings: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def save_settings(
        self, payload: Mapping[str, Any], settings_id: str = DEFAULT_SETTINGS_ID
    ) -> None:
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "INSERT INTO frontier_settings (id, payload, updated_at)"
                    " VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET"
                    " payload = excluded.payload,"
                    " updated_at = excluded.updated_at",
                    (settings_id, json.dumps(dict(payload)), _utcnow()),
                )
        except sqlite3.Error as exc:
            raise ProviderError(
                f"failed to save frontier settings: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        with contextlib.suppress(Exception):
            self._conn.close()


def _load_driver() -> Any:
    try:
        return importlib.import_module(_DRIVER_MODULE)
    except ImportError as exc:
        raise ProviderError(
            "Postgres driver 'psycopg' is not installed; "
            "install the postgres extra to use the Postgres frontier backend",
            provider_name=_PROVIDER_NAME,
            code=ProviderError.UNAVAILABLE,
        ) from exc


class PostgresFrontierStore(FrontierStore):
    """Postgres-backed frontier (same contract as :class:`SqliteFrontierStore`)."""

    def __init__(
        self,
        *,
        host: str = "localhost",
        port: int = 5432,
        dbname: str = "openlens",
        user: str = "postgres",
        password: str = "",
        schema: str = "public",
        pool_size: int = 5,
        connect_timeout: int = 5,
        checkout_timeout: float = 5.0,
    ) -> None:
        import re

        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema or ""):
            raise ValueError(f"invalid schema name: {schema!r}")
        self._schema = schema
        self._checkout_timeout = checkout_timeout
        self._connect_kwargs: dict[str, Any] = {
            "host": host,
            "port": int(port),
            "dbname": dbname,
            "user": user,
            "password": password,
            "connect_timeout": connect_timeout,
        }
        self._state_lock = threading.Lock()
        self._closed = False
        self._pool: queue.Queue[Any] = queue.Queue()
        try:
            for _ in range(max(1, pool_size)):
                self._pool.put(self._connect())
            self._init_schema()
        except ProviderError:
            self._drain()
            raise

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "PostgresFrontierStore":
        """Build a store from ``storage.postgres`` fields (dbname aliases).

        Pool sizes may be strings (YAML/env resolution yields text); they
        are coerced here, mirroring ``PostgresDocumentStore.from_config``.
        """
        dbname = config.get(
            "dbname", config.get("database", config.get("db", "openlens"))
        )
        kwargs: dict[str, Any] = {
            "host": config.get("host", "localhost"),
            "port": int(config.get("port", 5432)),
            "dbname": dbname,
            "user": config.get("user", "postgres"),
            "password": config.get("password", ""),
        }
        if "schema" in config:
            kwargs["schema"] = config["schema"]
        for key in ("pool_size", "connect_timeout"):
            if key in config:
                kwargs[key] = int(config[key])
        if "checkout_timeout" in config:
            kwargs["checkout_timeout"] = float(config["checkout_timeout"])
        return cls(**kwargs)

    def _connect(self) -> Any:
        driver = _load_driver()
        try:
            return driver.connect(**self._connect_kwargs)
        except Exception as exc:
            raise ProviderError(
                "failed to connect to Postgres for frontier store: "
                f"{self._connect_kwargs['host']}:{self._connect_kwargs['port']}: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    @contextlib.contextmanager
    def _connection(self):  # type: ignore[no-untyped-def]
        with self._state_lock:
            closed = self._closed
        if closed:
            raise ProviderError(
                "Postgres frontier store is closed",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            )
        try:
            conn = self._pool.get(timeout=self._checkout_timeout)
        except queue.Empty as exc:
            raise ProviderError(
                "Postgres frontier connection pool exhausted",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc
        try:
            yield conn
        except Exception:
            with contextlib.suppress(Exception):
                conn.close()
            with contextlib.suppress(Exception):
                self._pool.put(self._connect())
            raise
        else:
            # See PostgresDocumentStore._release: reads must not return
            # "idle in transaction" connections to the pool.
            with contextlib.suppress(Exception):
                conn.rollback()
            self._pool.put(conn)

    def _drain(self) -> None:
        while True:
            try:
                conn = self._pool.get_nowait()
            except queue.Empty:
                return
            with contextlib.suppress(Exception):
                conn.close()

    def _qualified(self, table: str) -> str:
        return f'"{self._schema}".{table}'

    def _init_schema(self) -> None:
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    for stmt in ordered_postgres_frontier_schema(self._schema):
                        cur.execute(stmt)
                conn.commit()
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"failed to initialize Postgres frontier schema: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def _write(self, sql: str, params: tuple[Any, ...]) -> Any:
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    rowcount = cur.rowcount
                conn.commit()
                return rowcount
        except Exception as exc:
            raise ProviderError(
                f"frontier write failed: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def enqueue(
        self,
        url: str,
        depth: int = 0,
        parent_url: str | None = None,
        priority: int | None = None,
    ) -> bool:
        if not isinstance(url, str) or not url.strip():
            return False
        norm = normalize_frontier_url(url)
        table = self._qualified("frontier_queue")
        prio = normalize_priority(priority)
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"INSERT INTO {table}"  # noqa: S608
                        " (url, depth, status, parent_url, priority)"
                        " VALUES (%s, %s, 'queued', %s, %s)"
                        " ON CONFLICT (url) DO NOTHING",
                        (
                            norm,
                            max(0, int(depth or 0)),
                            parent_url,
                            prio if prio is not None else 0,
                        ),
                    )
                    inserted = bool(cur.rowcount == 1)
                    if not inserted and prio is not None:
                        cur.execute(
                            f"UPDATE {table} SET priority = %s"  # noqa: S608
                            " WHERE url = %s",
                            (prio, norm),
                        )
                conn.commit()
                return inserted
        except Exception as exc:
            raise ProviderError(
                f"failed to enqueue frontier URL: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def dequeue(self) -> FrontierEntry | None:
        table = self._qualified("frontier_queue")
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT url, depth, status, parent_url, attempts,"  # noqa: S608
                        f" last_error, etag, last_modified, priority FROM {table}"
                        " WHERE status = 'queued'"
                        " ORDER BY priority DESC, depth ASC,"
                        " discovered_at ASC LIMIT 1"
                        " FOR UPDATE SKIP LOCKED"
                    )
                    row = cur.fetchone()
                    if row is None:
                        conn.commit()
                        return None
                    cols = (
                        "url",
                        "depth",
                        "status",
                        "parent_url",
                        "attempts",
                        "last_error",
                        "etag",
                        "last_modified",
                        "priority",
                    )
                    entry = _entry_from_row(dict(zip(cols, row)))
                    cur.execute(
                        f"UPDATE {table} SET status = 'active' WHERE url = %s",  # noqa: S608
                        (entry.url,),
                    )
                    entry.status = STATUS_ACTIVE
                conn.commit()
                return entry
        except Exception as exc:
            raise ProviderError(
                f"failed to dequeue frontier URL: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def mark_done(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> None:
        table = self._qualified("frontier_queue")
        self._write(
            f"UPDATE {table} SET status = 'done',"  # noqa: S608
            " etag = COALESCE(%s, etag),"
            " last_modified = COALESCE(%s, last_modified),"
            " fetched_at = now() WHERE url = %s",
            (etag, last_modified, normalize_frontier_url(url)),
        )

    def mark_failed(self, url: str, error: str) -> None:
        table = self._qualified("frontier_queue")
        self._write(
            f"UPDATE {table} SET status = 'failed',"  # noqa: S608
            " attempts = attempts + 1, last_error = %s WHERE url = %s",
            (error[:500], normalize_frontier_url(url)),
        )

    def mark_skipped(self, url: str) -> int:
        table = self._qualified("frontier_queue")
        return int(
            self._write(
                f"UPDATE {table} SET status = 'skipped' WHERE url = %s",  # noqa: S608
                (normalize_frontier_url(url),),
            )
            or 0
        )

    def skip_queued_domains(self, domains: Any) -> int:
        """Bulk-skip ``queued`` rows from ``domains`` (exact+subdomain).

        Postgres mirror of the SQLite implementation: one ``UPDATE``
        per normalized domain (malformed entries ignored, never fatal)
        with a ``last_error`` reason naming the domain. Patterns are
        bound parameters with ``ESCAPE '\\'``; only the validated table
        name is interpolated. Returns the total number of rows moved.
        """
        targets = normalize_skip_domains(domains)
        if not targets:
            return 0
        table = self._qualified("frontier_queue")
        total = 0
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    for domain in targets:
                        patterns = _skip_domain_like_patterns(domain)
                        condition = " OR ".join(
                            ["url LIKE %s ESCAPE '\\'"] * len(patterns)
                        )
                        cur.execute(
                            f"UPDATE {table} SET status = 'skipped',"  # noqa: S608
                            " last_error = %s WHERE status = 'queued'"
                            f" AND ({condition})",
                            (f"skipped: domain {domain}", *patterns),
                        )
                        total += cur.rowcount or 0
                conn.commit()
        except Exception as exc:
            raise ProviderError(
                f"failed to skip queued domains: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc
        return total

    def evict_oldest(self, limit: int) -> int:
        """Drop oldest queued rows past ``limit`` (oldest-first eviction).

        Single-statement eviction: one ``COUNT`` sizes the overflow, then
        one ``UPDATE ... WHERE url IN (SELECT ... ORDER BY ... LIMIT %s)``
        moves the victims. Victims match the old per-row loop exactly
        (oldest ``queued`` rows by ``discovered_at``).
        """
        table = self._qualified("frontier_queue")
        cap = max(0, limit)
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT COUNT(*) FROM {table}"  # noqa: S608
                        " WHERE status = 'queued'"
                    )
                    count_row = cur.fetchone()
                    count = int(count_row[0]) if count_row else 0
                    overflow = count - cap
                    if overflow <= 0:
                        conn.commit()
                        return 0
                    cur.execute(
                        f"UPDATE {table} SET status = 'skipped',"  # noqa: S608
                        " last_error = 'evicted: queue cap exceeded'"
                        f" WHERE url IN (SELECT url FROM {table}"  # noqa: S608
                        " WHERE status = 'queued'"
                        " ORDER BY discovered_at ASC LIMIT %s)",
                        (overflow,),
                    )
                    evicted = cur.rowcount or 0
                conn.commit()
                return int(evicted)
        except Exception as exc:
            raise ProviderError(
                f"failed to evict frontier URLs: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def requeue_active(self) -> int:
        table = self._qualified("frontier_queue")
        return int(
            self._write(
                f"UPDATE {table} SET status = 'queued' WHERE status = 'active'",  # noqa: S608
                (),
            )
            or 0
        )

    def requeue(self, url: str) -> bool:
        table = self._qualified("frontier_queue")
        return bool(
            self._write(
                f"UPDATE {table} SET status = 'queued'"  # noqa: S608
                " WHERE url = %s AND status IN ('active', 'failed')",
                (normalize_frontier_url(url),),
            )
        )

    def requeue_stale(self, max_age_days: int, limit: int) -> int:
        if (
            not isinstance(max_age_days, int)
            or isinstance(max_age_days, bool)
            or max_age_days < 0
            or not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit <= 0
        ):
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        table = self._qualified("frontier_queue")
        return int(
            self._write(
                f"UPDATE {table} SET status = 'queued',"  # noqa: S608
                " attempts = 0, owner = NULL, leased_at = NULL,"
                " heartbeat_at = NULL, last_error = NULL"
                " WHERE url IN (SELECT url FROM"
                f" {table}"  # noqa: S608
                " WHERE status = 'done'"
                " AND (fetched_at IS NULL OR fetched_at < %s)"
                " ORDER BY fetched_at ASC NULLS FIRST LIMIT %s)",
                (cutoff, limit),
            )
            or 0
        )

    def clear_queued(self) -> int:
        table = self._qualified("frontier_queue")
        return int(
            self._write(
                f"DELETE FROM {table} WHERE status = 'queued'",  # noqa: S608
                (),
            )
            or 0
        )

    # -- shared-queue coordination (Postgres) -----------------------------
    # ``SELECT ... FOR UPDATE SKIP LOCKED`` serializes claims across
    # processes: two workers never receive the same URL. Claim, heartbeat,
    # and reap each run in a single transaction.

    def claim(
        self,
        worker_id: str,
        *,
        lease_ttl_s: int = DEFAULT_LEASE_TTL_S,
    ) -> FrontierEntry | None:
        if not worker_id or not worker_id.strip():
            raise ProviderError(
                "claim requires a non-empty worker_id",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.INVALID_REQUEST,
            )
        owner = worker_id.strip()
        ttl = max(1, int(lease_ttl_s or DEFAULT_LEASE_TTL_S))
        table = self._qualified("frontier_queue")
        cols = (
            "url",
            "depth",
            "status",
            "parent_url",
            "attempts",
            "last_error",
            "etag",
            "last_modified",
            "priority",
            "owner",
            "leased_at",
            "heartbeat_at",
            "lease_ttl_s",
        )
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT {_CLAIM_COLUMNS} FROM {table}"  # noqa: S608
                        " WHERE status = 'queued'"
                        " ORDER BY priority DESC, depth ASC,"
                        " discovered_at ASC LIMIT 1"
                        " FOR UPDATE SKIP LOCKED"
                    )
                    row = cur.fetchone()
                    if row is None:
                        cur.execute(
                            f"SELECT {_CLAIM_COLUMNS} FROM {table}"  # noqa: S608
                            " WHERE status = 'active'"
                            " AND (heartbeat_at IS NULL"
                            " OR heartbeat_at < now() - make_interval(secs => %s))"
                            " ORDER BY priority DESC, depth ASC,"
                            " discovered_at ASC LIMIT 1"
                            " FOR UPDATE SKIP LOCKED",
                            (float(ttl),),
                        )
                        row = cur.fetchone()
                        if row is None:
                            conn.commit()
                            return None
                    entry = _entry_from_row(dict(zip(cols, row)))
                    cur.execute(
                        f"UPDATE {table} SET status = 'active',"  # noqa: S608
                        " owner = %s, leased_at = now(),"
                        " heartbeat_at = now(), lease_ttl_s = %s"
                        " WHERE url = %s",
                        (owner, ttl, entry.url),
                    )
                    entry.status = STATUS_ACTIVE
                    entry.owner = owner
                    entry.lease_ttl_s = ttl
                conn.commit()
                return entry
        except Exception as exc:
            raise ProviderError(
                f"failed to claim frontier URL: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def heartbeat(
        self,
        worker_id: str,
        *,
        lease_ttl_s: int = DEFAULT_LEASE_TTL_S,
    ) -> int:
        table = self._qualified("frontier_queue")
        return int(
            self._write(
                f"UPDATE {table} SET heartbeat_at = now(),"  # noqa: S608
                " lease_ttl_s = %s WHERE status = 'active' AND owner = %s",
                (max(1, int(lease_ttl_s or DEFAULT_LEASE_TTL_S)), worker_id),
            )
            or 0
        )

    def _expiry_predicate(
        self, max_age_s: float, now: float | None
    ) -> tuple[str, tuple[Any, ...]]:
        if now is None:
            return (
                "(heartbeat_at IS NULL"
                " OR heartbeat_at < now() - make_interval(secs => %s))",
                (float(max(0, max_age_s)),),
            )
        return (
            "(heartbeat_at IS NULL OR heartbeat_at < to_timestamp(%s))",
            (float(now - max(0, max_age_s)),),
        )

    def reap_expired_leases(
        self,
        *,
        max_age_s: float = DEFAULT_LEASE_TTL_S,
        now: float | None = None,
    ) -> int:
        table = self._qualified("frontier_queue")
        predicate, params = self._expiry_predicate(max_age_s, now)
        return int(
            self._write(
                f"UPDATE {table} SET status = 'queued',"  # noqa: S608
                f" owner = NULL WHERE status = 'active' AND {predicate}",
                params,
            )
            or 0
        )

    def requeue_owned(self, worker_id: str) -> int:
        table = self._qualified("frontier_queue")
        return int(
            self._write(
                f"UPDATE {table} SET status = 'queued',"  # noqa: S608
                " owner = NULL WHERE status = 'active' AND owner = %s",
                (worker_id,),
            )
            or 0
        )

    def owned(self, worker_id: str) -> list[FrontierEntry]:
        table = self._qualified("frontier_queue")
        cols = (
            "url",
            "depth",
            "status",
            "parent_url",
            "attempts",
            "last_error",
            "etag",
            "last_modified",
            "priority",
            "owner",
            "leased_at",
            "heartbeat_at",
            "lease_ttl_s",
        )
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT {_CLAIM_COLUMNS} FROM {table}"  # noqa: S608
                        " WHERE status = 'active' AND owner = %s"
                        " ORDER BY depth ASC, discovered_at ASC",
                        (worker_id,),
                    )
                    entries = [
                        _entry_from_row(dict(zip(cols, row))) for row in cur.fetchall()
                    ]
                conn.commit()
                return entries
        except Exception as exc:
            raise ProviderError(
                f"failed to list owned frontier URLs: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def reserve_politeness(
        self,
        host: str,
        delay_s: float,
        *,
        now: float | None = None,
    ) -> float:
        key = (host or "").strip().lower()
        if not key:
            return 0.0
        delay = max(0.0, float(delay_s or 0.0))
        moment = now if now is not None else time.time()
        table = self._qualified("domain_politeness")
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT next_fetch_at, delay_s FROM {table}"  # noqa: S608
                        " WHERE host = %s FOR UPDATE",
                        (key,),
                    )
                    row = cur.fetchone()
                    current = float(row[0] or 0.0) if row else 0.0
                    wait = max(0.0, current - moment)
                    cur.execute(
                        f"INSERT INTO {table}"  # noqa: S608
                        " (host, next_fetch_at, delay_s, updated_at)"
                        " VALUES (%s, %s, %s, now())"
                        " ON CONFLICT (host) DO UPDATE SET"
                        " next_fetch_at = EXCLUDED.next_fetch_at,"
                        " delay_s = EXCLUDED.delay_s,"
                        " updated_at = now()",
                        (key, max(moment, current) + delay, delay),
                    )
                conn.commit()
                return wait
        except Exception as exc:
            raise ProviderError(
                f"failed to reserve politeness slot: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def get_politeness_delay(self, host: str) -> float:
        key = (host or "").strip().lower()
        if not key:
            return 0.0
        table = self._qualified("domain_politeness")
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT delay_s FROM {table} WHERE host = %s",  # noqa: S608
                        (key,),
                    )
                    row = cur.fetchone()
                conn.commit()
                return float(row[0] or 0.0) if row else 0.0
        except Exception as exc:
            raise ProviderError(
                f"failed to read politeness delay: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def register_worker(
        self,
        worker_id: str,
        crawl_id: str = DEFAULT_CRAWL_ID,
    ) -> None:
        self.heartbeat_worker(worker_id, crawl_id, claimed=0)

    def heartbeat_worker(
        self,
        worker_id: str,
        crawl_id: str = DEFAULT_CRAWL_ID,
        *,
        claimed: int = 0,
    ) -> None:
        if not worker_id or not worker_id.strip():
            return
        table = self._qualified("crawl_workers")
        self._write(
            f"INSERT INTO {table}"  # noqa: S608
            " (worker_id, crawl_id, claimed, last_heartbeat, updated_at)"
            " VALUES (%s, %s, %s, now(), now())"
            " ON CONFLICT (worker_id) DO UPDATE SET"
            " crawl_id = EXCLUDED.crawl_id,"
            " claimed = EXCLUDED.claimed,"
            " last_heartbeat = now(), updated_at = now()",
            (worker_id.strip(), crawl_id, max(0, int(claimed or 0))),
        )

    def list_workers(self, crawl_id: str | None = None) -> list[dict[str, Any]]:
        table = self._qualified("crawl_workers")
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    if crawl_id is None:
                        cur.execute(
                            f"SELECT worker_id, crawl_id, claimed,"  # noqa: S608
                            f" last_heartbeat, updated_at FROM {table}"
                            " ORDER BY last_heartbeat DESC"
                        )
                    else:
                        cur.execute(
                            f"SELECT worker_id, crawl_id, claimed,"  # noqa: S608
                            f" last_heartbeat, updated_at FROM {table}"
                            " WHERE crawl_id = %s"
                            " ORDER BY last_heartbeat DESC",
                            (crawl_id,),
                        )
                    rows = cur.fetchall()
                conn.commit()
                return [
                    {
                        "worker_id": str(row[0]),
                        "crawl_id": str(row[1]),
                        "claimed": int(row[2] or 0),
                        "last_heartbeat": str(row[3] or ""),
                        "updated_at": str(row[4] or ""),
                    }
                    for row in rows
                ]
        except Exception as exc:
            raise ProviderError(
                f"failed to list workers: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def prune_workers(
        self,
        *,
        max_age_s: float = DEFAULT_LEASE_TTL_S * 2,
        now: float | None = None,
    ) -> int:
        table = self._qualified("crawl_workers")
        if now is None:
            return int(
                self._write(
                    f"DELETE FROM {table} WHERE last_heartbeat <"  # noqa: S608
                    " now() - make_interval(secs => %s)",
                    (float(max(0, max_age_s)),),
                )
                or 0
            )
        return int(
            self._write(
                f"DELETE FROM {table} WHERE last_heartbeat <"  # noqa: S608
                " to_timestamp(%s)",
                (float(now - max(0, max_age_s)),),
            )
            or 0
        )

    def increment_counters(
        self,
        crawl_id: str = DEFAULT_CRAWL_ID,
        *,
        pages_attempted: int = 0,
        pages_indexed: int = 0,
    ) -> CrawlCheckpoint:
        table = self._qualified("crawl_checkpoints")
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"INSERT INTO {table} (crawl_id, seeds, phase,"  # noqa: S608
                        " pages_attempted, pages_indexed, frontier_size,"
                        " stats, updated_at)"
                        " VALUES (%s, '[]'::jsonb, 'running', %s, %s, 0,"
                        " '{}'::jsonb, now())"
                        " ON CONFLICT (crawl_id) DO UPDATE SET"
                        f" pages_attempted = {table}.pages_attempted"
                        " + EXCLUDED.pages_attempted,"
                        f" pages_indexed = {table}.pages_indexed"
                        " + EXCLUDED.pages_indexed,"
                        " updated_at = now()"
                        " RETURNING crawl_id, seeds, phase, pages_attempted,"
                        " pages_indexed, frontier_size, stats, updated_at",
                        (
                            crawl_id,
                            max(0, int(pages_attempted or 0)),
                            max(0, int(pages_indexed or 0)),
                        ),
                    )
                    row = cur.fetchone()
                conn.commit()
                if row is None:
                    raise ProviderError(
                        "failed to increment crawl counters: missing row",
                        provider_name=_PROVIDER_NAME,
                        code=ProviderError.UNAVAILABLE,
                    )
                return _checkpoint_from_parts(
                    row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7]
                )
        except Exception as exc:
            raise ProviderError(
                f"failed to increment crawl counters: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def is_known(self, url: str) -> bool:
        table = self._qualified("frontier_queue")
        try:
            norm = normalize_frontier_url(url)
        except (AttributeError, ValueError):
            return False
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT 1 FROM {table} WHERE url = %s LIMIT 1",  # noqa: S608
                        (norm,),
                    )
                    found = cur.fetchone() is not None
                conn.commit()
                return found
        except Exception as exc:
            raise ProviderError(
                f"failed to look up frontier URL: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def status_of(self, url: str) -> str | None:
        table = self._qualified("frontier_queue")
        try:
            norm = normalize_frontier_url(url)
        except (AttributeError, ValueError):
            return None
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT status FROM {table} WHERE url = %s LIMIT 1",  # noqa: S608
                        (norm,),
                    )
                    row = cur.fetchone()
                conn.commit()
                return str(row[0]) if row is not None else None
        except Exception as exc:
            raise ProviderError(
                f"failed to look up frontier URL status: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def statuses_of(self, urls: Iterable[str]) -> dict[str, str]:
        table = self._qualified("frontier_queue")
        norms: list[str] = []
        for url in urls or []:
            try:
                norm = normalize_frontier_url(url)
            except (AttributeError, ValueError):
                continue
            if norm not in norms:
                norms.append(norm)
        out: dict[str, str] = {}
        if not norms:
            return out
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    for start in range(0, len(norms), 500):
                        chunk = norms[start : start + 500]
                        marks = ",".join(["%s"] * len(chunk))
                        cur.execute(
                            f"SELECT url, status FROM {table}"  # noqa: S608
                            f" WHERE url IN ({marks})",
                            tuple(chunk),
                        )
                        for url, status in cur.fetchall():
                            out[str(url)] = str(status)
                conn.commit()
                return out
        except Exception as exc:
            raise ProviderError(
                f"failed to look up frontier URL statuses: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def reseed(self, url: str) -> bool:
        table = self._qualified("frontier_queue")
        try:
            norm = normalize_frontier_url(url)
        except (AttributeError, ValueError):
            return False
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"UPDATE {table} SET status = 'queued',"  # noqa: S608
                        " attempts = 0, last_error = NULL"
                        " WHERE url = %s"
                        " AND status IN ('failed', 'skipped', 'done')",
                        (norm,),
                    )
                    revived = (cur.rowcount or 0) > 0
                conn.commit()
                return revived
        except Exception as exc:
            raise ProviderError(
                f"failed to reseed frontier URL: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def reseed_many(self, urls: Iterable[str]) -> int:
        table = self._qualified("frontier_queue")
        norms: list[str] = []
        for url in urls or []:
            try:
                norm = normalize_frontier_url(url)
            except (AttributeError, ValueError):
                continue
            if norm not in norms:
                norms.append(norm)
        if not norms:
            return 0
        try:
            revived = 0
            with self._connection() as conn:
                with conn.cursor() as cur:
                    for start in range(0, len(norms), 500):
                        chunk = norms[start : start + 500]
                        marks = ",".join(["%s"] * len(chunk))
                        cur.execute(
                            f"UPDATE {table} SET status = 'queued',"  # noqa: S608
                            " attempts = 0, last_error = NULL"
                            f" WHERE url IN ({marks})"
                            " AND status IN ('failed', 'skipped', 'done')",
                            tuple(chunk),
                        )
                        revived += cur.rowcount or 0
                conn.commit()
                return revived
        except Exception as exc:
            raise ProviderError(
                f"failed to reseed frontier URLs: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def pending(self, limit: int = 500, offset: int = 0) -> list[FrontierEntry]:
        table = self._qualified("frontier_queue")
        capped = max(0, limit)
        skipped = max(0, offset)
        want = capped + skipped
        cols = (
            "url",
            "depth",
            "status",
            "parent_url",
            "attempts",
            "last_error",
            "etag",
            "last_modified",
            "priority",
            "discovered_at",
        )
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    pages: list[list[tuple[tuple[Any, ...], FrontierEntry]]] = []
                    for status in ("queued", "active"):
                        cur.execute(
                            _pending_branch_sql(  # noqa: S608
                                table, "%s", status, extra_cols=", discovered_at"
                            ),
                            (want,),
                        )
                        branch: list[tuple[tuple[Any, ...], FrontierEntry]] = []
                        for row in cur.fetchall():
                            record = dict(zip(cols, row))
                            entry = _entry_from_row(record)
                            branch.append(
                                (
                                    (
                                        -(entry.priority or 0),
                                        entry.depth or 0,
                                        record["discovered_at"],
                                    ),
                                    entry,
                                )
                            )
                        pages.append(branch)
                conn.commit()
                return _merge_pending(pages, capped, skipped)
        except Exception as exc:
            raise ProviderError(
                f"failed to list pending frontier URLs: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def failed(self, limit: int = 100, offset: int = 0) -> list[FrontierEntry]:
        table = self._qualified("frontier_queue")
        capped = max(0, limit)
        skipped = max(0, offset)
        cols = (
            "url",
            "depth",
            "status",
            "parent_url",
            "attempts",
            "last_error",
            "etag",
            "last_modified",
            "priority",
        )
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT {', '.join(cols)} FROM {table}"  # noqa: S608
                        " WHERE status = 'failed'"
                        " ORDER BY attempts DESC, discovered_at DESC"
                        " LIMIT %s OFFSET %s",
                        (capped, skipped),
                    )
                    return [
                        _entry_from_row(dict(zip(cols, row))) for row in cur.fetchall()
                    ]
        except Exception as exc:
            raise ProviderError(
                f"failed to list failed frontier URLs: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def failed_domains(
        self, limit: int = 100, offset: int = 0
    ) -> list[tuple[str, int, str]]:
        """Aggregate ``failed`` rows by host with the top reason each.

        Postgres mirror of the SQLite implementation (``split_part``
        chains instead of ``substr``/``instr``); non-http(s) rows
        bucket as ``(invalid)``. The per-host reason is the mode of
        ``last_error`` (ties break alphabetically).
        """
        table = self._qualified("frontier_queue")
        capped = max(0, int(limit or 0))
        skipped = max(0, int(offset or 0))
        host_expr = FAILED_DOMAIN_HOST_EXPR_PG
        inner = (
            f"SELECT {host_expr} AS domain, COALESCE(last_error, '') AS reason"  # noqa: S608
            f" FROM {table} WHERE status = 'failed'"  # noqa: S608
        )
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT t.domain, t.failed, r.reason AS top_reason"  # noqa: S608
                        f" FROM (SELECT domain, COUNT(*) AS failed FROM ({inner})"
                        " AS totals GROUP BY domain) AS t JOIN (SELECT domain, reason,"
                        " ROW_NUMBER() OVER (PARTITION BY domain"
                        " ORDER BY COUNT(*) DESC, reason ASC) AS rn"
                        f" FROM ({inner}) AS reasons GROUP BY domain, reason) AS r"
                        " ON r.domain = t.domain AND r.rn = 1"
                        " ORDER BY t.failed DESC, t.domain ASC"
                        " LIMIT %s OFFSET %s",
                        (capped, skipped),
                    )
                    return [
                        (str(row[0]), int(row[1]), str(row[2] or ""))
                        for row in cur.fetchall()
                    ]
        except Exception as exc:
            raise ProviderError(
                f"failed to aggregate failed domains: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def failed_domain_count(self) -> int:
        table = self._qualified("frontier_queue")
        host_expr = FAILED_DOMAIN_HOST_EXPR_PG
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT COUNT(*) FROM (SELECT DISTINCT"  # noqa: S608
                        f" {host_expr} AS domain FROM {table}"
                        " WHERE status = 'failed') AS d"
                    )
                    row = cur.fetchone()
                    return int(row[0]) if row is not None else 0
        except Exception as exc:
            raise ProviderError(
                f"failed to count failed domains: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def requeue_failed_domains(self, domains: Any) -> int:
        """Revive ``failed`` rows from ``domains`` (exact+subdomain).

        Postgres mirror of the SQLite implementation: attempts reset
        to 0 and the error clears (reseed semantics), so the rows
        actually refetch.
        """
        targets = normalize_skip_domains(domains)
        if not targets:
            return 0
        table = self._qualified("frontier_queue")
        total = 0
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    for domain in targets:
                        patterns = _skip_domain_like_patterns(domain)
                        condition = " OR ".join(
                            ["url LIKE %s ESCAPE '\\'"] * len(patterns)
                        )
                        cur.execute(
                            f"UPDATE {table} SET status = 'queued',"  # noqa: S608
                            " attempts = 0, last_error = NULL"
                            " WHERE status = 'failed'"
                            f" AND ({condition})",
                            (*patterns,),
                        )
                        total += cur.rowcount or 0
                conn.commit()
        except Exception as exc:
            raise ProviderError(
                f"failed to requeue failed domains: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc
        return total

    def purge_failed_domains(self, domains: Any) -> int:
        """Delete ``failed`` rows from ``domains`` (exact+subdomain)."""
        targets = normalize_skip_domains(domains)
        if not targets:
            return 0
        table = self._qualified("frontier_queue")
        total = 0
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    for domain in targets:
                        patterns = _skip_domain_like_patterns(domain)
                        condition = " OR ".join(
                            ["url LIKE %s ESCAPE '\\'"] * len(patterns)
                        )
                        cur.execute(
                            f"DELETE FROM {table}"  # noqa: S608
                            f" WHERE status = 'failed' AND ({condition})",
                            (*patterns,),
                        )
                        total += cur.rowcount or 0
                conn.commit()
        except Exception as exc:
            raise ProviderError(
                f"failed to purge failed domains: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc
        return total

    def queued_domains(
        self, limit: int = 100, offset: int = 0
    ) -> list[tuple[str, int]]:
        """Aggregate ``queued`` rows by host (Postgres mirror of SQLite)."""
        table = self._qualified("frontier_queue")
        capped = max(0, int(limit or 0))
        skipped = max(0, int(offset or 0))
        host_expr = FAILED_DOMAIN_HOST_EXPR_PG
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT domain, COUNT(*) AS queued"  # noqa: S608
                        f" FROM (SELECT {host_expr} AS domain"
                        f" FROM {table} WHERE status = 'queued') AS q"
                        " GROUP BY domain ORDER BY queued DESC, domain ASC"
                        " LIMIT %s OFFSET %s",
                        (capped, skipped),
                    )
                    return [(str(row[0]), int(row[1])) for row in cur.fetchall()]
        except Exception as exc:
            raise ProviderError(
                f"failed to aggregate queued domains: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def queued_domain_count(self) -> int:
        table = self._qualified("frontier_queue")
        host_expr = FAILED_DOMAIN_HOST_EXPR_PG
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT COUNT(*) FROM (SELECT DISTINCT"  # noqa: S608
                        f" {host_expr} AS domain FROM {table}"
                        " WHERE status = 'queued') AS d"
                    )
                    row = cur.fetchone()
                    return int(row[0]) if row is not None else 0
        except Exception as exc:
            raise ProviderError(
                f"failed to count queued domains: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def bump_queued_priority(self, domains: Any, delta: int = 1) -> int:
        """Shift ``queued`` rows' priority by ``delta`` (exact+subdomain)."""
        targets = normalize_skip_domains(domains)
        step = int(delta or 0)
        if not targets or step == 0:
            return 0
        table = self._qualified("frontier_queue")
        total = 0
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    for domain in targets:
                        patterns = _skip_domain_like_patterns(domain)
                        condition = " OR ".join(
                            ["url LIKE %s ESCAPE '\\'"] * len(patterns)
                        )
                        cur.execute(
                            f"UPDATE {table} SET priority = priority + %s"  # noqa: S608
                            f" WHERE status = 'queued' AND ({condition})",
                            (step, *patterns),
                        )
                        total += cur.rowcount or 0
                conn.commit()
        except Exception as exc:
            raise ProviderError(
                f"failed to bump queued priority: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc
        return total

    def counts(self) -> dict[str, int]:
        # One indexed COUNT per known status instead of a GROUP BY full
        # scan: past ~25% queued selectivity the planner seq-scans the
        # whole backlog for every dashboard poll, WS push, and drain
        # size check. Exact because writers only emit _VALID_STATUSES;
        # zero classes are omitted, matching the GROUP BY shape.
        table = self._qualified("frontier_queue")
        out: dict[str, int] = {"total": 0}
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    for status in sorted(_VALID_STATUSES):
                        cur.execute(
                            f"SELECT COUNT(*) FROM {table}"  # noqa: S608
                            " WHERE status = %s",
                            (status,),
                        )
                        row = cur.fetchone()
                        n = int(row[0]) if row else 0
                        if n:
                            out[status] = n
                            out["total"] += n
                conn.commit()
                return out
        except Exception as exc:
            raise ProviderError(
                f"failed to count frontier URLs: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def submit_crawl_request(self, url: str) -> dict[str, Any]:
        if not isinstance(url, str) or not url.strip():
            raise ProviderError(
                "crawl request URL must not be blank",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.INVALID_REQUEST,
            )
        norm = normalize_frontier_url(url)
        table = self._qualified("crawl_requests")
        cols = ("id", "url", "status", "created_at", "decided_at")
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT id, url, status, created_at, decided_at"  # noqa: S608
                        f" FROM {table} WHERE url = %s AND status = 'pending'"
                        " ORDER BY id DESC LIMIT 1",
                        (norm,),
                    )
                    row = cur.fetchone()
                    if row is None:
                        cur.execute(
                            f"INSERT INTO {table} (url, status)"  # noqa: S608
                            " VALUES (%s, 'pending') RETURNING id",
                            (norm,),
                        )
                        new_id = cur.fetchone()[0]
                        cur.execute(
                            f"SELECT id, url, status, created_at, decided_at"  # noqa: S608
                            f" FROM {table} WHERE id = %s",
                            (new_id,),
                        )
                        row = cur.fetchone()
                    record = _request_from_row(dict(zip(cols, row)))
                conn.commit()
                return record
        except Exception as exc:
            raise ProviderError(
                f"failed to submit crawl request: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def list_crawl_requests(
        self, status: str | None = None, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        table = self._qualified("crawl_requests")
        cols = ("id", "url", "status", "created_at", "decided_at")
        where = "WHERE status = %s" if status is not None else ""
        params: tuple[Any, ...] = (
            (status, max(0, limit), max(0, offset))
            if status is not None
            else (max(0, limit), max(0, offset))
        )
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT id, url, status, created_at, decided_at"  # noqa: S608
                        f" FROM {table} {where}"
                        " ORDER BY id DESC LIMIT %s OFFSET %s",
                        params,
                    )
                    records = [
                        _request_from_row(dict(zip(cols, row)))
                        for row in cur.fetchall()
                    ]
                conn.commit()
                return records
        except Exception as exc:
            raise ProviderError(
                f"failed to list crawl requests: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def set_crawl_request_status(self, request_id: int, status: str) -> bool:
        if status not in VALID_CRAWL_REQUEST_DECISIONS:
            return False
        try:
            request_id = int(request_id)
        except (TypeError, ValueError):
            return False
        table = self._qualified("crawl_requests")
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"UPDATE {table} SET status = %s,"  # noqa: S608
                        " decided_at = now()"
                        " WHERE id = %s AND status = 'pending'",
                        (status, request_id),
                    )
                    changed = bool(cur.rowcount and cur.rowcount > 0)
                conn.commit()
                return changed
        except Exception as exc:
            raise ProviderError(
                f"failed to decide crawl request: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def validators(self) -> dict[str, dict[str, str]]:
        table = self._qualified("frontier_queue")
        out: dict[str, dict[str, str]] = {}
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT url, etag, last_modified FROM {table}"  # noqa: S608
                        " WHERE etag IS NOT NULL OR last_modified IS NOT NULL"
                    )
                    for url, etag, last_modified in cur.fetchall():
                        entry: dict[str, str] = {}
                        if etag:
                            entry["etag"] = str(etag)
                        if last_modified:
                            entry["last_modified"] = str(last_modified)
                        if entry:
                            out[str(url)] = entry
                conn.commit()
                return out
        except Exception as exc:
            raise ProviderError(
                f"failed to read frontier validators: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def save_checkpoint(self, checkpoint: CrawlCheckpoint) -> None:
        table = self._qualified("crawl_checkpoints")
        phase = checkpoint.phase if checkpoint.phase in _VALID_PHASES else PHASE_RUNNING
        self._write(
            f"INSERT INTO {table} (crawl_id, seeds, phase, pages_attempted,"  # noqa: S608
            " pages_indexed, frontier_size, stats, updated_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, now())"
            " ON CONFLICT (crawl_id) DO UPDATE SET seeds = EXCLUDED.seeds,"
            " phase = EXCLUDED.phase,"
            " pages_attempted = EXCLUDED.pages_attempted,"
            " pages_indexed = EXCLUDED.pages_indexed,"
            " frontier_size = EXCLUDED.frontier_size,"
            " stats = EXCLUDED.stats, updated_at = now()",
            (
                checkpoint.crawl_id,
                json.dumps(list(checkpoint.seeds)),
                phase,
                checkpoint.pages_attempted,
                checkpoint.pages_indexed,
                checkpoint.frontier_size,
                json.dumps(dict(checkpoint.stats)),
            ),
        )

    def load_checkpoint(
        self, crawl_id: str = DEFAULT_CRAWL_ID
    ) -> CrawlCheckpoint | None:
        table = self._qualified("crawl_checkpoints")
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT crawl_id, seeds, phase, pages_attempted,"  # noqa: S608
                        f" pages_indexed, frontier_size, stats, updated_at"
                        f" FROM {table} WHERE crawl_id = %s",
                        (crawl_id,),
                    )
                    row = cur.fetchone()
                conn.commit()
                if row is None:
                    return None
                seeds = row[1]
                if not isinstance(seeds, list):
                    try:
                        seeds = json.loads(seeds)
                    except (ValueError, TypeError):
                        seeds = []
                stats = row[6]
                if not isinstance(stats, dict):
                    try:
                        stats = json.loads(stats)
                    except (ValueError, TypeError):
                        stats = {}
                return _checkpoint_from_parts(
                    row[0], seeds, row[2], row[3], row[4], row[5], stats, row[7]
                )
        except Exception as exc:
            raise ProviderError(
                f"failed to load crawl checkpoint: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def get_settings(self, settings_id: str = DEFAULT_SETTINGS_ID) -> dict[str, Any]:
        table = self._qualified("frontier_settings")
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT payload FROM {table} WHERE id = %s",  # noqa: S608
                        (settings_id,),
                    )
                    row = cur.fetchone()
                conn.commit()
                if row is None:
                    return {}
                payload = row[0]
                if isinstance(payload, dict):
                    return dict(payload)
                try:
                    parsed = json.loads(payload or "{}")
                except (ValueError, TypeError):
                    return {}
                return dict(parsed) if isinstance(parsed, dict) else {}
        except Exception as exc:
            raise ProviderError(
                f"failed to read frontier settings: {exc}",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def save_settings(
        self, payload: Mapping[str, Any], settings_id: str = DEFAULT_SETTINGS_ID
    ) -> None:
        table = self._qualified("frontier_settings")
        self._write(
            f"INSERT INTO {table} (id, payload, updated_at)"  # noqa: S608
            " VALUES (%s, %s, now())"
            " ON CONFLICT (id) DO UPDATE SET payload = EXCLUDED.payload,"
            " updated_at = now()",
            (settings_id, json.dumps(dict(payload))),
        )

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        self._drain()


class WorkerLease:
    """Owns one worker's coordination lifecycle on a shared queue.

    Registers the worker, heartbeats owned URL leases every ``ttl/3``
    seconds on a daemon thread, and on :meth:`shutdown` stops the thread
    and requeues still-owned rows so other workers pick them up
    (graceful-shutdown requeue). Callers may also install SIGTERM/SIGINT
    handling via :meth:`install_signal_handlers` so an orchestrator stop
    releases in-flight rows instead of waiting for lease expiry.
    """

    def __init__(
        self,
        store: FrontierStore,
        worker_id: str | None = None,
        crawl_id: str = DEFAULT_CRAWL_ID,
        *,
        lease_ttl_s: int = DEFAULT_LEASE_TTL_S,
        heartbeat_interval_s: float | None = None,
    ) -> None:
        self._store = store
        self.worker_id = resolve_worker_id(worker_id)
        self.crawl_id = crawl_id
        self.lease_ttl_s = max(1, int(lease_ttl_s or DEFAULT_LEASE_TTL_S))
        self.heartbeat_interval_s = (
            heartbeat_interval_s
            if heartbeat_interval_s is not None
            else self.lease_ttl_s / 3.0
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._claimed_total = 0

    @property
    def claimed_total(self) -> int:
        """URLs claimed through this lease so far (for worker registry)."""
        return self._claimed_total

    def start(self) -> "WorkerLease":
        """Register the worker and start the heartbeat thread (idempotent)."""
        if self._thread is not None and self._thread.is_alive():
            return self
        with contextlib.suppress(Exception):
            self._store.register_worker(self.worker_id, self.crawl_id)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"frontier-lease-{self.worker_id}", daemon=True
        )
        self._thread.start()
        return self

    def claim(self) -> FrontierEntry | None:
        """Claim one URL for this worker, counting it for the registry."""
        entry = self._store.claim(self.worker_id, lease_ttl_s=self.lease_ttl_s)
        if entry is not None:
            self._claimed_total += 1
        return entry

    def _run(self) -> None:
        while not self._stop.wait(self.heartbeat_interval_s):
            with contextlib.suppress(Exception):
                self._store.heartbeat(self.worker_id, lease_ttl_s=self.lease_ttl_s)
            with contextlib.suppress(Exception):
                self._store.heartbeat_worker(
                    self.worker_id, self.crawl_id, claimed=self._claimed_total
                )

    def install_signal_handlers(self) -> "WorkerLease":
        """Route SIGTERM/SIGINT to :meth:`shutdown` (main-thread only)."""
        for signum in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(Exception, ValueError):
                signal.signal(signum, self._handle_signal)
        return self

    def _handle_signal(self, signum: int, _frame: Any) -> None:
        _ = signum
        self.shutdown()

    def shutdown(self) -> int:
        """Stop heartbeats and requeue owned rows. Returns rows released."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            with contextlib.suppress(Exception):
                thread.join(timeout=self.heartbeat_interval_s + 5.0)
        with contextlib.suppress(Exception):
            self._store.heartbeat_worker(
                self.worker_id, self.crawl_id, claimed=self._claimed_total
            )
        try:
            return self._store.requeue_owned(self.worker_id)
        except Exception:
            return 0

    def __enter__(self) -> "WorkerLease":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.shutdown()


def open_frontier_store(config: Mapping[str, Any]) -> FrontierStore:
    """Build the configured frontier store (SQLite default, like documents).

    ``config`` is the top-level app config (reads ``storage:``); a
    missing/``sqlite`` backend opens :class:`SqliteFrontierStore` on the
    resolved SQLite path, ``postgres`` opens :class:`PostgresFrontierStore`.
    """
    if not isinstance(config, dict):
        raise ProviderError(
            "frontier store config must be a mapping",
            provider_name=_PROVIDER_NAME,
            code=ProviderError.UNAVAILABLE,
        )
    storage = config.get("storage", {})
    if storage is None:
        storage = {}
    if not isinstance(storage, dict):
        raise ProviderError(
            "Config section 'storage' must be a mapping.",
            provider_name=_PROVIDER_NAME,
            code=ProviderError.UNAVAILABLE,
        )
    backend = storage.get("backend", "sqlite")
    if backend == "postgres":
        section = storage.get("postgres", {}) or {}
        return PostgresFrontierStore.from_config(section)
    sqlite_cfg = storage.get("sqlite", {}) or {}
    db_path = sqlite_cfg.get("db_path", "data/openlens.db")
    if not isinstance(db_path, str) or not db_path:
        db_path = ":memory:"
    return SqliteFrontierStore(db_path)

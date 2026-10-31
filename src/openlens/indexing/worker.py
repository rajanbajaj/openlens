"""Distributed-crawl worker entrypoint: identity + API heartbeats.

Runs as the ``crawler`` service in ``deploy/local/docker-compose.yml``
(scale with ``docker compose up --scale crawler=N``). Each replica
resolves its identity via
:data:`openlens.storage.frontier.WORKER_ID_ENV` (``OPENLENS_WORKER_ID``;
compose leaves it empty so replicas auto-resolve to
``{hostname}-{pid}-{rand}`` — hostnames are unique per replica) and
POSTs a heartbeat to the backend's
``/api/v1/frontier/workers/heartbeat`` every
``OPENLENS_HEARTBEAT_INTERVAL_S`` seconds (default ``40``, i.e. TTL/3
for the ``120`` s lease window in ``storage.frontier``).

The heartbeat carries the shared-queue ``crawl_id`` so the dashboard's
Workers view can attribute liveness per crawl. URL claiming itself
stays in the frontier store (Postgres ``SELECT ... FOR UPDATE SKIP
LOCKED``; SQLite is single-process only and must run with exactly one
crawler replica). This process never invents crawl progress: it reports
liveness only.

Reads/writes no secrets: the admin token travels in the
``X-Admin-Token`` header from ``OPENLENS_FRONTIER_ADMIN_TOKEN`` (empty
in local dev, where the gate stays open).
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from openlens.core.interfaces import ProviderError
from openlens.storage.frontier import (
    DEFAULT_CRAWL_ID,
    DEFAULT_HEARTBEAT_INTERVAL_S,
    DEFAULT_LEASE_TTL_S,
    WORKER_ID_ENV,
    normalize_skip_domains,
    resolve_worker_id,
    skip_url_matches,
)

API_URL_ENV = "OPENLENS_API_URL"
ADMIN_TOKEN_ENV = "OPENLENS_FRONTIER_ADMIN_TOKEN"  # noqa: S105 (env var name, not a credential)
HEARTBEAT_INTERVAL_ENV = "OPENLENS_HEARTBEAT_INTERVAL_S"
CRAWL_ID_ENV = "OPENLENS_CRAWL_ID"


def heartbeat_once(
    api_url: str,
    worker_id: str,
    *,
    crawl_id: str = DEFAULT_CRAWL_ID,
    claimed: int = 0,
    status: str = "alive",
    admin_token: str = "",
    timeout_s: float = 10.0,
) -> dict[str, object]:
    """POST one heartbeat; return the decoded worker list payload."""
    url = api_url.rstrip("/") + "/api/v1/frontier/workers/heartbeat"
    scheme = urllib.parse.urlparse(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"refusing to heartbeat non-HTTP(S) URL: {scheme or '?'}")
    payload = json.dumps(
        {
            "worker_id": worker_id,
            "crawl_id": crawl_id,
            "claimed": claimed,
            "status": status,
        }
    ).encode()
    headers = {"Content-Type": "application/json"}
    if admin_token.strip():
        headers["X-Admin-Token"] = admin_token.strip()
    # Scheme allowlisted to http/https above; noqa for the URL-audit rule.
    request = urllib.request.Request(url, data=payload, headers=headers)  # noqa: S310
    with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310
        decoded: dict[str, object] = json.loads(response.read().decode() or "{}")
        return decoded


def main() -> int:
    """Resolve identity, heartbeat until killed; 0 on clean shutdown."""
    api_url = os.environ.get(API_URL_ENV, "http://localhost:8000").strip()
    worker_id = resolve_worker_id()
    crawl_id = (
        os.environ.get(CRAWL_ID_ENV, DEFAULT_CRAWL_ID).strip() or DEFAULT_CRAWL_ID
    )
    admin_token = os.environ.get(ADMIN_TOKEN_ENV, "")
    try:
        interval = float(os.environ.get(HEARTBEAT_INTERVAL_ENV, ""))
    except ValueError:
        interval = DEFAULT_HEARTBEAT_INTERVAL_S
    if interval <= 0:
        interval = DEFAULT_HEARTBEAT_INTERVAL_S
    print(
        f"[worker {worker_id}] crawl={crawl_id} api={api_url} "
        f"interval={interval}s ({WORKER_ID_ENV} override supported)",
        flush=True,
    )
    while True:
        try:
            heartbeat_once(
                api_url,
                worker_id,
                crawl_id=crawl_id,
                admin_token=admin_token,
            )
        except KeyboardInterrupt:
            print(f"[worker {worker_id}] stopped", flush=True)
            return 0
        except Exception as exc:  # fail-open: a missed beat only ages liveness
            print(f"[worker {worker_id}] heartbeat failed: {exc}", flush=True)
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            print(f"[worker {worker_id}] stopped", flush=True)
            return 0


if __name__ == "__main__":
    raise SystemExit(main())


# -- standalone crawl loop (claim -> fetch -> mark done) --------------------
#
# The reporter above only posts liveness. The loop below does the actual
# crawling work: it drives :func:`openlens.indexing.run_index` (which runs
# :meth:`PoliteCrawler.crawl` against the shared frontier store — robots,
# politeness, conditional re-requests, and link extraction are reused,
# never reimplemented), and owns identity, lease heartbeats, stop
# signals, the global pause flag, checkpoint continuity, and the
# out-of-band enqueue path.


def new_worker_id(explicit: str | None = None) -> str:
    """Resolve the worker identity (thin alias of ``resolve_worker_id``).

    Precedence: explicit argument, ``OPENLENS_WORKER_ID`` env, then
    ``{hostname}-{pid}-{8 hex}``. Kept under this name for the CLI and
    tests; the canonical implementation lives in ``storage.frontier``.
    """
    return resolve_worker_id(explicit)


#: Settings key in the persistent frontier-settings row acting as the
#: global pause flag for every worker sharing the store.
PAUSE_SETTINGS_KEY = "paused"


def _live_crawl_limits(settings: dict[str, Any]) -> dict[str, Any]:
    """Validate dashboard crawl limits; invalid/missing keys become None.

    Only keys present with sane values are applied — anything else keeps
    the boot configuration, so a corrupt settings row can never zero out
    or uncap a live crawl.
    """
    depth = settings.get("default_max_depth")
    queued = settings.get("max_queued_urls")
    pages = settings.get("max_pages")
    delay = settings.get("crawl_delay_s")
    robots = settings.get("respect_robots")
    agent = settings.get("user_agent")
    agent = agent.strip() if isinstance(agent, str) else ""
    skip = normalize_skip_domains(settings.get("skip_domains"))
    return {
        "default_max_depth": (
            depth
            if isinstance(depth, int) and not isinstance(depth, bool) and depth >= 0
            else None
        ),
        "max_queued_urls": (
            queued
            if isinstance(queued, int) and not isinstance(queued, bool) and queued > 0
            else None
        ),
        "max_pages": (
            pages
            if isinstance(pages, int) and not isinstance(pages, bool) and pages > 0
            else None
        ),
        "crawl_delay_s": (
            float(delay)
            if isinstance(delay, (int, float))
            and not isinstance(delay, bool)
            and 0 <= delay <= 600
            else None
        ),
        "respect_robots": robots if isinstance(robots, bool) else None,
        "user_agent": agent if 1 <= len(agent) <= 256 else None,
        "skip_domains": skip,
    }


#: Bound on consecutive skip-listed takes per guarded dequeue/claim: the
#: worker marks each skipped row and takes the next one, giving up (for
#: this pass) after this many so a queue full of listed domains cannot
#: starve the loop.
MAX_SKIPS_PER_CLAIM = 100


class _SkipGuardFrontier:
    """Transparent frontier-store view enforcing ``skip_domains`` at take.

    Delegates every attribute to the wrapped store except ``dequeue``
    and ``claim``: a taken URL whose host matches the current skip list
    is marked skipped (never fetched) and the next row is taken instead,
    bounded by :data:`MAX_SKIPS_PER_CLAIM`. New enqueues from listed
    domains are still stored (never silently dropped); they settle here
    or at bulk-skip time, so counts stay auditable.
    """

    def __init__(self, store: Any, domains_fn: Callable[[], list[str]]) -> None:
        self._guard_store = store
        self._guard_domains_fn = domains_fn

    def __getattr__(self, name: str) -> Any:
        return getattr(self._guard_store, name)

    def dequeue(self) -> Any:
        """Guarded dequeue: skip-listed URLs settle as skipped."""
        return self._guarded(self._guard_store.dequeue)

    def claim(self, worker_id: str, **kwargs: Any) -> Any:
        """Guarded lease claim: skip-listed URLs settle as skipped."""
        return self._guarded(lambda: self._guard_store.claim(worker_id, **kwargs))

    def _guarded(self, take: Callable[[], Any]) -> Any:
        try:
            domains = list(self._guard_domains_fn() or [])
        except Exception:
            domains = []
        if not domains:
            return take()
        for _ in range(MAX_SKIPS_PER_CLAIM + 1):
            entry = take()
            if entry is None:
                return None
            url = getattr(entry, "url", "")
            if not skip_url_matches(url, domains):
                return entry
            with contextlib.suppress(Exception):
                self._guard_store.mark_skipped(url)
        return None


@dataclass
class CrawlerWorker:
    """One crawl loop over a shared frontier store.

    Fetch/parse/robots logic is never duplicated here: every URL is
    claimed and fetched inside :meth:`PoliteCrawler.crawl` (via
    :func:`run_index`), which marks rows done/failed and enqueues newly
    discovered links itself. This class only owns identity, lease
    heartbeats, stop signals, the global pause flag, and the extra-URL
    enqueue path.
    """

    crawler: Any = None
    repository: Any = None
    frontier_store: Any = None
    worker_id: str = ""
    crawl_id: str = DEFAULT_CRAWL_ID
    indexing_config: dict[str, Any] = field(default_factory=dict)
    chunk_size: int = 500
    heartbeat_interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S
    lease_ttl_s: int = DEFAULT_LEASE_TTL_S
    #: Validated ``skip_domains`` list from the last :meth:`refresh_limits`
    #: (normalized bare hostnames; [] when unset/invalid). Drives both
    #: the refresh bulk-skip and the claim-time guard.
    skip_domains: list[str] = field(default_factory=list)
    #: Rows bulk-skipped by the last :meth:`refresh_limits` (diagnostics).
    last_skip_bulk_count: int = 0

    def __post_init__(self) -> None:
        if not self.worker_id:
            self.worker_id = resolve_worker_id()
        self._stop_requested = threading.Event()
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None

    # -- stop signals --------------------------------------------------------

    def request_stop(self) -> None:
        """Ask the loop to finish the in-flight fetch, then stop."""
        self._stop_requested.set()

    def should_stop(self) -> bool:
        """Stop predicate handed to ``crawl``/``run_index``."""
        if self._stop_requested.is_set():
            return True
        return self._paused_via_store()

    def _paused_via_store(self) -> bool:
        """True when the shared settings row carries the pause flag."""
        store = self.frontier_store
        if store is None:
            return False
        try:
            settings = store.get_settings()
        except Exception:
            return False
        return bool(
            isinstance(settings, dict) and settings.get(PAUSE_SETTINGS_KEY) is True
        )

    def refresh_limits(self) -> None:
        """Refresh live crawl limits from the shared settings row.

        The admin dashboard persists settings (e.g. ``default_max_depth``)
        to the frontier store; the boot file config only seeds them.
        Without this refresh the crawlers would enforce stale boot values
        forever (unlimited depth when the file omits the cap). Best
        effort: any failure keeps the current crawler configuration.
        """
        store = self.frontier_store
        crawler = self.crawler
        if store is None or crawler is None:
            return
        try:
            settings = store.get_settings()
        except Exception:
            return
        if not isinstance(settings, dict):
            return
        live = _live_crawl_limits(settings)
        with contextlib.suppress(Exception):
            if live["default_max_depth"] is not None:
                crawler.default_max_depth = live["default_max_depth"]
        with contextlib.suppress(Exception):
            if live["max_queued_urls"] is not None:
                crawler.max_queued_urls = live["max_queued_urls"]
        with contextlib.suppress(Exception):
            if live["max_pages"] is not None:
                crawler.max_pages = live["max_pages"]
        with contextlib.suppress(Exception):
            if live["crawl_delay_s"] is not None:
                crawler.crawl_delay_s = live["crawl_delay_s"]
        with contextlib.suppress(Exception):
            if live["respect_robots"] is not None:
                crawler.respect_robots = live["respect_robots"]
        with contextlib.suppress(Exception):
            if live["user_agent"] is not None:
                crawler.user_agent = live["user_agent"]
        with contextlib.suppress(Exception):
            skip = live.get("skip_domains") or []
            self.skip_domains = list(skip)
            # Bulk-skip every refresh: already-queued rows from listed
            # domains move to 'skipped' with a reason, so the in-flight
            # queue stops without waiting for its turn. Stores without
            # bulk-skip support (older doubles) simply skip nothing.
            bulk = store.skip_queued_domains(list(skip)) if skip else 0
            self.last_skip_bulk_count = int(bulk or 0)

    def guarded_store(self) -> Any:
        """Frontier store view enforcing the refreshed skip list at take.

        Wraps :attr:`frontier_store` so every ``dequeue``/``claim`` on
        the crawl path marks skip-listed URLs skipped (never fetched)
        and takes the next row instead. Returns the raw store when it is
        None. The guard reads the live :attr:`skip_domains`, so a later
        :meth:`refresh_limits` applies without rewrapping.
        """
        store = self.frontier_store
        if store is None:
            return None
        return _SkipGuardFrontier(store, lambda: list(self.skip_domains))

    # -- enqueue ----------------------------------------------------------

    def enqueue_new_urls(self, urls: list[str]) -> int:
        """Queue extra URLs discovered out-of-band; return newly queued count.

        Dedup is the store's (normalized-URL PK); already-known URLs
        report False and are not counted.
        """
        store = self.frontier_store
        if store is None:
            return 0
        added = 0
        for url in urls:
            if not isinstance(url, str) or not url.strip():
                continue
            with contextlib.suppress(Exception):
                if store.enqueue(url.strip(), 0):
                    added += 1
        return added

    # -- run ---------------------------------------------------------------

    def _installsignal_handlers(self) -> None:
        """Trap SIGTERM/SIGINT to :meth:`request_stop` (main thread only)."""
        try:
            signal.signal(signal.SIGTERM, lambda _s, _f: self.request_stop())
            signal.signal(signal.SIGINT, lambda _s, _f: self.request_stop())
        except (ValueError, OSError, RuntimeError):
            # Not the main thread, or signals unavailable: the loop still
            # polls should_stop(); only the OS trap is skipped.
            pass

    def _beat(self, claimed: int = 0) -> None:
        """Refresh worker liveness + owned-row leases (fail-open)."""
        store = self.frontier_store
        if store is None:
            return
        with contextlib.suppress(Exception):
            store.heartbeat_worker(self.worker_id, self.crawl_id, claimed=claimed)
        with contextlib.suppress(Exception):
            store.heartbeat(self.worker_id, lease_ttl_s=self.lease_ttl_s)

    def _heartbeat_loop(self) -> None:
        while not self._heartbeat_stop.wait(self.heartbeat_interval_s):
            self._beat()

    def _claimed_count(self) -> int:
        stats = getattr(self.crawler, "last_stats", None)
        fetched = getattr(stats, "fetched", 0)
        if isinstance(fetched, bool) or not isinstance(fetched, int):
            return 0
        return max(0, fetched)

    def run(self, seed_urls: list[str]) -> Any:
        """Claim -> fetch -> mark-done until exhausted, paused, or stopped.

        Returns the :func:`run_index` stats. The crawl checkpoint left in
        the store carries phase ``done`` (or ``paused`` when stopped), so
        a later worker with the same ``crawl_id`` resumes without
        refetching. On exit the worker releases its owned rows for other
        workers via ``requeue_owned``.
        """
        from openlens.indexing.indexer import run_index

        self._installsignal_handlers()
        store = self.frontier_store
        if store is not None:
            with contextlib.suppress(Exception):
                store.register_worker(self.worker_id, self.crawl_id)
        self._beat()
        self.refresh_limits()
        self._heartbeat_stop.clear()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, daemon=True
        )
        self._heartbeat_thread.start()
        try:
            return run_index(
                list(seed_urls),
                self.repository,
                dict(self.indexing_config),
                self.chunk_size,
                crawler=self.crawler,
                frontier_store=self.guarded_store(),
                should_stop=self.should_stop,
                crawl_id=self.crawl_id,
            )
        finally:
            self._heartbeat_stop.set()
            self._beat(claimed=self._claimed_count())
            if store is not None:
                with contextlib.suppress(Exception):
                    store.requeue_owned(self.worker_id)

    @property
    def stop_requested(self) -> bool:
        """True once SIGTERM/SIGINT (or a test) asked the loop to stop."""
        return self._stop_requested.is_set()

    def paused(self) -> bool:
        """True when the shared settings row carries the pause flag."""
        return self._paused_via_store()

    @property
    def last_checkpoint(self) -> dict[str, Any] | None:
        """Latest checkpoint row for this worker's ``crawl_id`` (if any)."""
        store = self.frontier_store
        if store is None:
            return None
        try:
            checkpoint = store.load_checkpoint(self.crawl_id)
        except Exception:
            return None
        if checkpoint is None:
            return None
        return {
            "crawl_id": checkpoint.crawl_id,
            "phase": checkpoint.phase,
            "pages_attempted": checkpoint.pages_attempted,
            "pages_indexed": checkpoint.pages_indexed,
        }


def _build_worker(
    app_config: dict[str, Any],
    *,
    worker_id: str | None = None,
    crawl_id: str = DEFAULT_CRAWL_ID,
    chunk_size: int = 500,
    crawler_factory: Callable[[], Any] | None = None,
) -> tuple[CrawlerWorker, Any, Any, dict[str, Any]]:
    """Shared construction for the standalone and Kafka worker loops."""
    from openlens.indexing.crawler import PoliteCrawler
    from openlens.storage.frontier import open_frontier_store
    from openlens.storage.repository import DocumentRepository

    indexing_config = app_config.get("indexing", {})
    if not isinstance(indexing_config, dict):
        indexing_config = {}
    storage = app_config.get("storage", {})
    if not isinstance(storage, dict):
        storage = {}
    repository: Any = None
    if storage.get("backend") == "postgres":
        # The searched index lives in Postgres: crawl into it, not into
        # the SQLite sidecar (whose rows search would never see).
        from openlens.storage.postgres_backend import PostgresDocumentStore

        repository = PostgresDocumentStore.from_config(
            storage.get("postgres", {}) or {}
        )
    else:
        repository = DocumentRepository(":memory:")
        providers = app_config.get("providers")
        if isinstance(providers, dict):
            for entry in providers.values():
                if isinstance(entry, dict):
                    candidate = entry.get("db_path")
                    if isinstance(candidate, str) and candidate:
                        if candidate != ":memory:":
                            # Bare runs (docker or laptop) may lack the
                            # parent dir (e.g. no shared volume mounted).
                            parent = os.path.dirname(os.path.abspath(candidate))
                            if parent:
                                with contextlib.suppress(OSError):
                                    os.makedirs(parent, exist_ok=True)
                        repository = DocumentRepository(candidate)
                        break
    frontier_store = open_frontier_store(app_config)
    crawler = crawler_factory() if crawler_factory is not None else PoliteCrawler()
    if crawler_factory is None:
        crawler.configure(dict(indexing_config))
    worker = CrawlerWorker(
        crawler=crawler,
        repository=repository,
        frontier_store=frontier_store,
        worker_id=resolve_worker_id(worker_id),
        crawl_id=crawl_id,
        indexing_config=dict(indexing_config),
        chunk_size=chunk_size,
    )
    return worker, repository, frontier_store, dict(indexing_config)


def run_standalone_worker(
    seed_urls: list[str],
    app_config: dict[str, Any],
    *,
    worker_id: str | None = None,
    crawl_id: str = DEFAULT_CRAWL_ID,
    chunk_size: int = 500,
    crawler_factory: Callable[[], Any] | None = None,
) -> Any:
    """Build a worker from app config and run it (CLI entry helper).

    Opens the repository (SQLite ``:memory:`` fallback like the CLI) and
    the configured frontier store (SQLite default, Postgres when
    ``storage.backend: postgres``), configures a :class:`PoliteCrawler`
    from the ``indexing:`` section, and runs claim -> fetch -> mark-done.
    """
    worker, repository, frontier_store, _ = _build_worker(
        app_config,
        worker_id=worker_id,
        crawl_id=crawl_id,
        chunk_size=chunk_size,
        crawler_factory=crawler_factory,
    )
    try:
        return worker.run(list(seed_urls))
    finally:
        with contextlib.suppress(Exception):
            frontier_store.close()
        with contextlib.suppress(Exception):
            repository.close()


#: Idle polls between opportunistic store drains. The poll loop only
#: drains the persistent store when a Kafka message arrives, so seeds
#: that landed in the store after startup (fallback-enqueued during a
#: broker outage, crash leftovers) would otherwise wait behind an empty
#: topic forever. Draining every Nth idle poll (poll blocks ~1s, so ~30s
#: cadence) picks those up; the drain itself is pause-aware and cheap
#: when the frontier is empty.
_IDLE_DRAIN_EVERY_POLLS = 30


def run_kafka_worker(
    seed_urls: list[str],
    app_config: dict[str, Any],
    *,
    worker_id: str | None = None,
    crawl_id: str = DEFAULT_CRAWL_ID,
    chunk_size: int = 500,
    crawler_factory: Callable[[], Any] | None = None,
    queue: Any | None = None,
    stop_event: Any | None = None,
    max_iterations: int | None = None,
    idle_sleep_s: float = 5.0,
) -> dict[str, int]:
    """Consume the Kafka URL queue until stopped (distributed crawl loop).

    Every replica joins the same consumer group, so Kafka assigns each
    URL to exactly one live worker. Per message: enqueue into the
    persistent store at the published depth (the normalized-URL primary
    key makes redeliveries no-ops), drain via :func:`run_index` (robots,
    politeness, conditional re-fetch, checkpoints, and link extraction
    are reused, never reimplemented), publish newly discovered links
    back to the topic, then commit the offset. At-least-once delivery
    with an idempotent sink: a crash replays from the last commit and
    the store dedup absorbs the replay.

    ``queue`` injects a test fake; ``stop_event``/``max_iterations`` are
    test hooks (production stops on SIGTERM/SIGINT). Raises
    ``ProviderError(UNAVAILABLE)`` when the broker is unreachable instead
    of busy-spinning on nothing.
    """
    from openlens.indexing.indexer import run_index
    from openlens.indexing.kafka_queue import open_kafka_queue

    worker, repository, frontier_store, indexing_config = _build_worker(
        app_config,
        worker_id=worker_id,
        crawl_id=crawl_id,
        chunk_size=chunk_size,
        crawler_factory=crawler_factory,
    )
    kafka = queue if queue is not None else open_kafka_queue(app_config)
    if not kafka.check_available():
        raise ProviderError(
            "Kafka broker unreachable; cannot start the Kafka crawl loop",
            provider_name="kafka_queue",
            code=ProviderError.UNAVAILABLE,
        )

    def _handle_stop(_signum: object, _frame: object) -> None:
        worker.request_stop()
        if stop_event is not None:
            with contextlib.suppress(Exception):
                stop_event.set()

    try:
        signal.signal(signal.SIGTERM, _handle_stop)  # type: ignore[arg-type]
        signal.signal(signal.SIGINT, _handle_stop)  # type: ignore[arg-type]
    except (ValueError, OSError, RuntimeError):
        pass
    counts = {"received": 0, "added": 0, "updated": 0}

    def _publish_link(url: str, depth: int) -> None:
        with contextlib.suppress(Exception):
            kafka.publish(url, depth)

    def _drain_once() -> None:
        worker.refresh_limits()
        drain_stats = run_index(
            [],
            repository,
            dict(indexing_config),
            chunk_size,
            crawler=worker.crawler,
            frontier_store=worker.guarded_store(),
            should_stop=worker.should_stop,
            crawl_id=crawl_id,
            on_link=_publish_link,
        )
        counts["added"] += int(getattr(drain_stats, "added", 0) or 0)
        counts["updated"] += int(getattr(drain_stats, "updated", 0) or 0)

    try:
        for seed in seed_urls:
            if isinstance(seed, str) and seed.strip():
                kafka.publish(seed.strip(), 0)
        # Drain any backlog the store already holds (fallback-enqueued
        # seeds from a broker outage, crash leftovers) before entering
        # the poll loop, so no queued URL waits for the next message.
        if not worker.paused():
            _drain_once()
        iteration = 0
        idle_polls = 0
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            if worker.stop_requested:
                break
            if max_iterations is not None and iteration >= max_iterations:
                break
            iteration += 1
            if worker.paused():
                if stop_event is not None:
                    if stop_event.wait(idle_sleep_s):
                        break
                else:
                    time.sleep(idle_sleep_s)
                continue
            message = kafka.poll()
            if message is None:
                idle_polls += 1
                if idle_polls % _IDLE_DRAIN_EVERY_POLLS == 0 and not worker.paused():
                    # Opportunistic backlog drain (resilient: a transient
                    # store error must not kill the poll loop; the next
                    # cadence retries). Startup misconfiguration still
                    # fails loud via the initial _drain_once above.
                    with contextlib.suppress(Exception):
                        _drain_once()
                continue
            idle_polls = 0
            counts["received"] += 1
            with contextlib.suppress(Exception):
                frontier_store.enqueue(
                    message.url,
                    message.depth,
                    priority=getattr(message, "priority", None),
                )
            worker.refresh_limits()
            stats = run_index(
                [],
                repository,
                dict(indexing_config),
                chunk_size,
                crawler=worker.crawler,
                frontier_store=worker.guarded_store(),
                should_stop=worker.should_stop,
                crawl_id=crawl_id,
                on_link=_publish_link,
            )
            counts["added"] += int(getattr(stats, "added", 0) or 0)
            counts["updated"] += int(getattr(stats, "updated", 0) or 0)
            kafka.commit()
    finally:
        with contextlib.suppress(Exception):
            kafka.close()
        with contextlib.suppress(Exception):
            frontier_store.close()
        with contextlib.suppress(Exception):
            repository.close()
    return counts

"""Unit tests for the Redis frontier backend (PASS-1 store hot path).

Offline only: live tests run against a real Redis ONLY when
``OPENLENS_TEST_REDIS_URL`` is set (same convention as
``tests/integration/test_cache_redis.py``); otherwise they skip. The
offline tests assert the fail-closed factory contracts without any
network or services.
"""

from __future__ import annotations

import itertools
import os
import threading
import time
import uuid

import pytest

from openlens.core.interfaces import ConfigError, ProviderError
from openlens.storage.frontier import (
    SqliteFrontierStore,
    WorkerLease,
)
from openlens.storage.frontier_redis import (
    RedisFrontierStore,
    build_redis_frontier_store,
    open_frontier_store_for_crawl,
    validate_frontier_section,
)

REDIS_URL = os.environ.get("OPENLENS_TEST_REDIS_URL", "").strip()

needs_redis = pytest.mark.skipif(
    not REDIS_URL, reason="OPENLENS_TEST_REDIS_URL is not set"
)

_counter = itertools.count()


def _make_store() -> RedisFrontierStore:
    prefix = f"fw-test-{os.getpid()}-{next(_counter)}-{uuid.uuid4().hex[:8]}"
    try:
        return RedisFrontierStore(REDIS_URL, key_prefix=prefix)
    except ProviderError as exc:
        pytest.skip(f"redis unavailable: {exc}")
        raise AssertionError("unreachable")


@pytest.fixture
def store() -> RedisFrontierStore:
    import contextlib

    client_store = _make_store()
    yield client_store
    with contextlib.suppress(Exception):
        keys = list(client_store._client.scan_iter(f"{client_store._prefix}:*"))
        if keys:
            client_store._client.delete(*keys)
    with contextlib.suppress(Exception):
        client_store.close(flush=False)


# --- offline factory contracts (no network/services) -------------------------


def test_unknown_frontier_backend_is_config_error() -> None:
    with pytest.raises(ConfigError):
        open_frontier_store_for_crawl({"frontier": {"backend": "mysql"}})


def test_crawler_rejects_bad_frontier_config() -> None:
    with pytest.raises(ConfigError):
        validate_frontier_section({"backend": "bogus-backend"})


def test_frontier_section_must_be_mapping() -> None:
    with pytest.raises(ConfigError):
        validate_frontier_section("redis")  # type: ignore[arg-type]
    assert validate_frontier_section(None) == {}


def test_frontier_redis_empty_url_is_unavailable() -> None:
    with pytest.raises(ProviderError) as excinfo:
        build_redis_frontier_store({"backend": "redis", "redis": {"url": ""}})
    assert excinfo.value.code == ProviderError.UNAVAILABLE
    with pytest.raises(ProviderError) as excinfo2:
        build_redis_frontier_store({"backend": "redis", "redis": {"url": 123}})
    assert excinfo2.value.code == ProviderError.UNAVAILABLE


def test_frontier_redis_string_numerics_coerce() -> None:
    # Env-interpolated YAML values arrive as strings; they must coerce,
    # so validation reaches the (unreachable here) connection gate.
    with pytest.raises(ProviderError) as excinfo:
        build_redis_frontier_store(
            {
                "backend": "redis",
                "redis": {
                    "url": "redis://127.0.0.1:6399/1",
                    "flush_interval_s": "5",
                    "lease_ttl_s": "120",
                    "heartbeat_interval_s": "40",
                },
            }
        )
    assert excinfo.value.code == ProviderError.UNAVAILABLE


def test_frontier_redis_bad_intervals_are_config_errors() -> None:
    with pytest.raises(ConfigError):
        build_redis_frontier_store(
            {"backend": "redis", "redis": {"flush_interval_s": -1}}
        )
    with pytest.raises(ConfigError):
        build_redis_frontier_store({"backend": "redis", "redis": {"lease_ttl_s": 0}})
    with pytest.raises(ConfigError):
        build_redis_frontier_store({"backend": "redis", "redis": {"key_prefix": ""}})


def test_non_redis_backend_falls_through_to_storage() -> None:
    config: dict[str, object] = {
        "storage": {"backend": "sqlite", "sqlite": {"db_path": ":memory:"}}
    }
    assert isinstance(open_frontier_store_for_crawl(config), SqliteFrontierStore)
    assert isinstance(
        open_frontier_store_for_crawl({**config, "frontier": {"backend": "sqlite"}}),
        SqliteFrontierStore,
    )
    assert isinstance(
        open_frontier_store_for_crawl(
            {**config, "frontier": {"backend": "postgres", "redis": {}}}
        ),
        SqliteFrontierStore,
    )


def test_redis_unreachable_is_unavailable() -> None:
    with pytest.raises(ProviderError) as excinfo:
        RedisFrontierStore(
            "redis://127.0.0.1:6399/1",
            key_prefix="fw-test-down",
            socket_timeout_s=0.5,
        )
    assert excinfo.value.code == ProviderError.UNAVAILABLE


def test_open_frontier_store_for_crawl_rejects_non_mapping() -> None:
    with pytest.raises(ProviderError):
        open_frontier_store_for_crawl("nope")  # type: ignore[arg-type]


# --- live matrix (skip without OPENLENS_TEST_REDIS_URL) -----------------------


@needs_redis
def test_enqueue_dedups_normalized_urls(store: RedisFrontierStore) -> None:
    assert store.enqueue("https://example.com/Page", 0) is True
    assert store.enqueue("https://example.com/page#frag", 0) is False
    assert store.enqueue("https://example.com/page/", 0) is False
    assert store.enqueue("", 0) is False
    assert store.enqueue("   ", 0) is False
    counts = store.counts()
    assert counts.get("queued") == 1
    assert counts.get("total") == 1


@needs_redis
def test_enqueue_priority_orders_dequeue_before_depth(
    store: RedisFrontierStore,
) -> None:
    store.enqueue("https://example.com/deep", 2, priority=10)
    store.enqueue("https://example.com/shallow", 0, priority=1)
    first = store.dequeue()
    assert first is not None and first.url.endswith("/deep")
    assert first.status == "active"


@needs_redis
def test_enqueue_priority_none_keeps_explicit_overwrites(
    store: RedisFrontierStore,
) -> None:
    store.enqueue("https://example.com/a", 0, priority=5)
    store.enqueue("https://example.com/a", 0, priority=None)
    assert store.entry_of("https://example.com/a") is not None
    assert store.entry_of("https://example.com/a").priority == 5  # type: ignore[union-attr]
    store.enqueue("https://example.com/a", 0, priority=9)
    assert store.entry_of("https://example.com/a").priority == 9  # type: ignore[union-attr]


@needs_redis
def test_frontier_priority_index_covers_claim_ordering(
    store: RedisFrontierStore,
) -> None:
    store.enqueue("https://example.com/p1-deep", 3, priority=1)
    store.enqueue("https://example.com/p5-a", 1, priority=5)
    store.enqueue("https://example.com/p5-b", 0, priority=5)
    store.enqueue("https://example.com/p1-first", 0, priority=1)
    got = []
    for _ in range(4):
        entry = store.claim("w-order")
        assert entry is not None
        got.append(entry.url)
    assert got == [
        "https://example.com/p5-b",
        "https://example.com/p5-a",
        "https://example.com/p1-first",
        "https://example.com/p1-deep",
    ]


@needs_redis
def test_claim_records_owner_and_lease(store: RedisFrontierStore) -> None:
    store.enqueue("https://example.com/a", 0)
    entry = store.claim("worker-1")
    assert entry is not None
    assert entry.owner == "worker-1"
    assert entry.status == "active"
    assert entry.leased_at
    assert entry.heartbeat_at
    assert [row.url for row in store.owned("worker-1")] == ["https://example.com/a"]
    assert store.owned("other") == []


@needs_redis
def test_claim_rejects_empty_worker_id(store: RedisFrontierStore) -> None:
    store.enqueue("https://example.com/a", 0)
    with pytest.raises(ProviderError) as excinfo:
        store.claim("  ")
    assert excinfo.value.code == ProviderError.INVALID_REQUEST


@needs_redis
def test_no_double_claim_under_thread_concurrency(
    store: RedisFrontierStore,
) -> None:
    for i in range(10):
        store.enqueue(f"https://example.com/{i}", 0)
    won: list[str] = []
    guard = threading.Lock()

    def _grab(worker: str) -> None:
        for _ in range(10):
            entry = store.claim(worker)
            if entry is None:
                return
            with guard:
                won.append(entry.url)

    threads = [threading.Thread(target=_grab, args=(f"w-{i}",)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(won) == sorted(f"https://example.com/{i}" for i in range(10))


@needs_redis
def test_two_workers_claim_distinct_urls_without_collision(
    store: RedisFrontierStore,
) -> None:
    store.enqueue("https://example.com/a", 0)
    store.enqueue("https://example.com/b", 0)
    first = store.claim("w-1")
    second = store.claim("w-2")
    assert first is not None and second is not None
    assert first.url != second.url
    assert store.claim("w-3") is None


@needs_redis
def test_heartbeat_refreshes_owned_rows_only(store: RedisFrontierStore) -> None:
    store.enqueue("https://example.com/owned", 0)
    store.enqueue("https://example.com/loose", 0)
    store.claim("w-1")
    store.dequeue()
    assert store.heartbeat("w-1") == 1
    assert store.heartbeat("nobody") == 0


@needs_redis
def test_reap_recovers_expired_but_never_steals_live(
    store: RedisFrontierStore,
) -> None:
    store.enqueue("https://example.com/a", 0)
    store.enqueue("https://example.com/b", 0)
    store.claim("w-1")
    store.claim("w-1")
    assert store.reap_expired_leases(max_age_s=3600) == 0
    assert store.reap_expired_leases(max_age_s=0) == 2
    assert store.counts().get("queued") == 2


@needs_redis
def test_reap_recovers_legacy_ownerless_rows(store: RedisFrontierStore) -> None:
    store.enqueue("https://example.com/a", 0)
    assert store.dequeue() is not None
    assert store.reap_expired_leases(max_age_s=3600) == 1
    assert store.counts().get("queued") == 1


@needs_redis
def test_pause_reaps_expired_leases_but_keeps_live_ones(
    store: RedisFrontierStore,
) -> None:
    store.enqueue("https://example.com/a", 0)
    store.enqueue("https://example.com/b", 0)
    store.claim("w-1", lease_ttl_s=100)
    store.claim("w-1", lease_ttl_s=100)
    assert store.reap_expired_leases(max_age_s=50, now=time.time()) == 0
    assert store.reap_expired_leases(max_age_s=50, now=time.time() + 200) == 2


@needs_redis
def test_requeue_owned_releases_only_that_worker(
    store: RedisFrontierStore,
) -> None:
    store.enqueue("https://example.com/a", 0)
    store.enqueue("https://example.com/b", 0)
    store.claim("w-1")
    store.claim("w-2")
    assert store.requeue_owned("w-1") == 1
    assert [row.url for row in store.owned("w-2")] == ["https://example.com/b"]


@needs_redis
def test_worker_lease_shutdown_releases_in_flight(
    store: RedisFrontierStore,
) -> None:
    store.enqueue("https://example.com/a", 0)
    with WorkerLease(store, worker_id="w-lease", heartbeat_interval_s=60.0) as lease:
        assert lease.start() is lease
        assert store.claim("w-lease") is not None
        assert len(store.owned("w-lease")) == 1
    assert store.owned("w-lease") == []
    assert store.counts().get("queued") == 1


@needs_redis
def test_worker_lease_context_manager(store: RedisFrontierStore) -> None:
    store.enqueue("https://example.com/a", 0)
    lease = WorkerLease(store, worker_id="w-ctx", heartbeat_interval_s=60.0)
    with lease:
        claimed = store.claim("w-ctx")
        assert claimed is not None
    assert store.counts().get("queued") == 1


@needs_redis
def test_failed_lists_reasons_most_retried_first(
    store: RedisFrontierStore,
) -> None:
    for url in ("https://example.com/a", "https://example.com/b"):
        assert store.enqueue(url, 0) is True
    store.mark_failed("https://example.com/a", "fetch timeout")
    store.mark_failed("https://example.com/a", "fetch timeout")
    store.mark_failed("https://example.com/b", "denied by deny_domains")
    rows = store.failed(limit=10)
    assert [(row.url, row.attempts, row.last_error) for row in rows] == [
        ("https://example.com/a", 2, "fetch timeout"),
        ("https://example.com/b", 1, "denied by deny_domains"),
    ]
    assert [row.url for row in store.failed(limit=1, offset=1)] == [
        "https://example.com/b"
    ]


@needs_redis
def test_domains_requeue_revives_with_reset_attempts(
    store: RedisFrontierStore,
) -> None:
    store.enqueue("https://example.com/a", 0)
    store.enqueue("https://other.test/b", 0)
    store.mark_failed("https://example.com/a", "boom")
    store.mark_failed("https://other.test/b", "boom")
    assert store.requeue_failed_domains(["example.com"]) == 1
    entry = store.entry_of("https://example.com/a")
    assert entry is not None and entry.status == "queued" and entry.attempts == 0
    assert store.failed() == [] or all(
        row.url != "https://example.com/a" for row in store.failed()
    )


@needs_redis
def test_backoff_rows_hide_from_main_dequeue(store: RedisFrontierStore) -> None:
    store.enqueue("https://example.com/a", 0)
    store.mark_backoff("https://example.com/a", "429 slow down")
    assert store.dequeue() is None
    assert store.claim("w-1") is None
    entry = store.dequeue_backoff()
    assert entry is not None and entry.url == "https://example.com/a"
    assert entry.status == "active"
    assert entry.attempts == 1


@needs_redis
def test_backoff_frontier_view_serves_only_backoff_rows(
    store: RedisFrontierStore,
) -> None:
    store.enqueue("https://example.com/a", 0)
    store.enqueue("https://example.com/b", 0)
    store.mark_backoff("https://example.com/a", "429")
    assert [row.url for row in store.pending()] == ["https://example.com/b"]
    assert store.dequeue_backoff() is not None
    assert store.dequeue_backoff() is None


@needs_redis
def test_worker_enqueue_new_urls_dedups(store: RedisFrontierStore) -> None:
    assert store.enqueue("https://example.com/a", 0) is True
    assert store.enqueue("https://example.com/a", 1) is False
    assert store.is_known("https://example.com/a") is True
    assert store.is_known("https://example.com/missing") is False


@needs_redis
def test_pause_then_resume_avoids_refetch(store: RedisFrontierStore) -> None:
    store.enqueue("https://example.com/a", 0)
    entry = store.dequeue()
    assert entry is not None
    store.mark_done(entry.url, etag='"abc"')
    assert store.requeue_active() == 0
    assert store.dequeue() is None
    assert store.is_known("https://example.com/a") is True
    assert store.validators()["https://example.com/a"] == {"etag": '"abc"'}


@needs_redis
def test_crawl_through_store_marks_done_and_checkpoints(
    store: RedisFrontierStore,
) -> None:
    from openlens.storage.frontier import CrawlCheckpoint

    store.enqueue("https://example.com/a", 0)
    entry = store.dequeue()
    assert entry is not None
    store.mark_done(entry.url)
    store.save_checkpoint(
        CrawlCheckpoint(
            crawl_id="c1",
            seeds=["https://example.com/a"],
            pages_attempted=1,
            pages_indexed=1,
        )
    )
    loaded = store.load_checkpoint("c1")
    assert loaded is not None and loaded.pages_attempted == 1
    assert store.load_checkpoint("missing") is None
    assert store.counts().get("done") == 1


@needs_redis
def test_enqueue_seeds_repeat_pens_known_url(store: RedisFrontierStore) -> None:
    assert store.enqueue("https://example.com/a", 0, parent_url=None) is True
    assert store.enqueue("https://example.com/a", 0, parent_url=None) is False
    entry = store.entry_of("https://example.com/a")
    assert entry is not None and entry.depth == 0


@needs_redis
def test_checkpoint_and_settings_round_trip(store: RedisFrontierStore) -> None:
    from openlens.storage.frontier import CrawlCheckpoint

    assert store.get_settings() == {}
    store.save_settings({"max_pages": 10})
    assert store.get_settings() == {"max_pages": 10}
    store.save_checkpoint(CrawlCheckpoint(crawl_id="c1", phase="paused"))
    assert store.load_checkpoint("c1") is not None
    assert store.load_checkpoint("c1").phase == "paused"  # type: ignore[union-attr]


@needs_redis
def test_discovery_pass_enqueues_only_new_urls(store: RedisFrontierStore) -> None:
    for url in ("https://example.com/a", "https://example.com/b"):
        store.enqueue(url, 0)
    store.mark_done("https://example.com/a")
    assert store.enqueue("https://example.com/a", 0) is False
    assert store.enqueue("https://example.com/c", 0) is True
    assert store.counts().get("total") == 3


@needs_redis
def test_discovery_pass_handles_sitemaps(store: RedisFrontierStore) -> None:
    store.enqueue(
        "https://example.com/sitemap.xml", 0, parent_url="https://example.com/"
    )
    entry = store.entry_of("https://example.com/sitemap.xml")
    assert entry is not None
    assert entry.parent_url == "https://example.com/"


@needs_redis
def test_discovery_pass_refreshes_stale_done(store: RedisFrontierStore) -> None:
    store.enqueue("https://example.com/a", 0)
    store.mark_done("https://example.com/a")
    assert store.requeue_stale(30, 10) == 0
    # fetched_at/cutoff are second-precision: cross a second boundary so
    # max_age_days=0 deterministically counts the row as stale.
    time.sleep(1.1)
    assert store.requeue_stale(0, 10) == 1
    assert store.requeue_stale(-1, 10) == 0
    assert store.requeue_stale(30, 0) == 0
    assert store.status_of("https://example.com/a") == "queued"


@needs_redis
def test_failed_domain_rollup_and_drilldown(store: RedisFrontierStore) -> None:
    store.enqueue("https://example.com/a", 0)
    store.enqueue("https://example.com/b", 0)
    store.enqueue("https://other.test/c", 0)
    store.mark_failed("https://example.com/a", "timeout")
    store.mark_failed("https://example.com/b", "timeout")
    store.mark_failed("https://other.test/c", "denied")
    assert store.failed_domain_count() == 2
    assert store.failed_domains(limit=10) == [
        ("example.com", 2, "timeout"),
        ("other.test", 1, "denied"),
    ]
    assert [row.url for row in store.failed_domain_urls("example.com")] == [
        "https://example.com/a",
        "https://example.com/b",
    ]
    assert store.failed_domain_url_count("example.com") == 2
    assert store.failed_domain_urls("not a domain!!") == []
    assert store.failed_domain_url_count("not a domain!!") == 0


@needs_redis
def test_queued_domain_views_and_counts(store: RedisFrontierStore) -> None:
    store.enqueue("https://example.com/a", 0, priority=3)
    store.enqueue("https://example.com/b", 0, priority=1)
    store.enqueue("https://other.test/c", 0, priority=9)
    assert store.queued_domain_count() == 2
    assert store.queued_domains(limit=10) == [
        ("other.test", 1, 9),
        ("example.com", 2, 3),
    ]
    assert [row.url for row in store.queued_domain_urls("example.com")] == [
        "https://example.com/a",
        "https://example.com/b",
    ]
    assert store.queued_domain_url_count("example.com") == 2
    assert store.queued_domain_urls("not a domain!!") == []
    assert store.queued_domain_url_count("not a domain!!") == 0


@needs_redis
def test_domains_priority_bumps_only_queued(store: RedisFrontierStore) -> None:
    store.enqueue("https://example.com/a", 0, priority=1)
    store.enqueue("https://example.com/b", 0, priority=1)
    store.enqueue("https://other.test/c", 0, priority=1)
    assert store.dequeue() is not None
    assert store.bump_queued_priority(["example.com"], 10) == 1
    assert store.entry_of("https://example.com/b") is not None
    assert store.entry_of("https://example.com/b").priority == 11  # type: ignore[union-attr]
    assert store.bump_queued_priority(["example.com"], 0) == 0
    assert store.bump_queued_priority([], 5) == 0


@needs_redis
def test_purge_failed_domains_keeps_history(store: RedisFrontierStore) -> None:
    store.enqueue("https://example.com/a", 0)
    store.enqueue("https://example.com/b", 0)
    store.mark_failed("https://example.com/a", "timeout")
    store.mark_done("https://example.com/b")
    assert store.purge_failed_domains(["example.com"]) == 1
    assert store.failed() == []
    assert store.is_known("https://example.com/a") is False
    assert store.is_known("https://example.com/b") is True


@needs_redis
def test_skip_queued_domains_and_clear_queued(store: RedisFrontierStore) -> None:
    store.enqueue("https://example.com/a", 0)
    store.enqueue("https://sub.example.com/b", 0)
    store.enqueue("https://other.test/c", 0)
    assert store.skip_queued_domains(["example.com"]) == 2
    assert store.clear_queued() == 1
    assert store.counts().get("skipped") == 2
    assert store.skip_queued_domains("not-a-list!!") == 0


@needs_redis
def test_requeue_and_reseed(store: RedisFrontierStore) -> None:
    store.enqueue("https://example.com/a", 0)
    store.enqueue("https://example.com/b", 0)
    store.mark_failed("https://example.com/a", "boom")
    assert store.requeue("https://example.com/a") is True
    assert store.requeue("https://example.com/b") is False
    store.mark_failed("https://example.com/a", "boom")
    assert store.reseed("https://example.com/a") is True
    entry = store.entry_of("https://example.com/a")
    assert entry is not None and entry.attempts == 0 and entry.status == "queued"
    assert store.reseed("https://example.com/a") is False
    assert store.reseed_many(["https://example.com/a", "bad::url"]) == 0
    store.mark_failed("https://example.com/a", "boom")
    store.mark_failed("https://example.com/b", "boom")
    assert store.reseed_many(["https://example.com/a", "https://example.com/b"]) == 2


@needs_redis
def test_status_views_fail_closed(store: RedisFrontierStore) -> None:
    store.enqueue("https://example.com/a", 0)
    assert store.status_of("https://example.com/a") == "queued"
    assert store.status_of("https://example.com/missing") is None
    assert store.status_of(None) is None  # type: ignore[arg-type]
    assert store.statuses_of(
        ["https://example.com/a", "https://example.com/missing"]
    ) == {"https://example.com/a": "queued"}
    assert store.entry_of("https://example.com/missing") is None
    assert store.entry_of(None) is None  # type: ignore[arg-type]


@needs_redis
def test_crawl_requests_round_trip(store: RedisFrontierStore) -> None:
    record = store.submit_crawl_request("https://example.com/new")
    assert record["status"] == "pending" and record["id"] is not None
    same = store.submit_crawl_request("https://example.com/new")
    assert same["id"] == record["id"]
    assert len(store.list_crawl_requests(status="pending")) == 1
    assert store.set_crawl_request_status(record["id"], "bogus") is False
    assert store.set_crawl_request_status(999999, "approved") is False
    assert store.set_crawl_request_status(record["id"], "approved") is True
    assert store.list_crawl_requests(status="pending") == []
    with pytest.raises(ProviderError) as excinfo:
        store.submit_crawl_request("   ")
    assert excinfo.value.code == ProviderError.INVALID_REQUEST


@needs_redis
def test_workers_and_politeness(store: RedisFrontierStore) -> None:
    store.register_worker("w-1")
    store.heartbeat_worker("w-1", claimed=2)
    workers = store.list_workers()
    assert [w["worker_id"] for w in workers] == ["w-1"]
    assert workers[0]["claimed"] == 2
    store.register_worker("w-2", crawl_id="other")
    assert len(store.list_workers(crawl_id="default")) == 1
    store.heartbeat_worker("ghost", crawl_id="default")
    assert store.prune_workers(max_age_s=0, now=time.time() + 10000) >= 1
    assert store.reserve_politeness("example.com", 5.0, now=1000.0) == 0.0
    assert store.reserve_politeness("example.com", 5.0, now=1001.0) == 4.0
    assert store.get_politeness_delay("example.com") == 5.0
    assert store.get_politeness_delay("unknown.test") == 0.0
    assert store.reserve_politeness("", 5.0) == 0.0


@needs_redis
def test_increment_counters_merges(store: RedisFrontierStore) -> None:
    first = store.increment_counters("c1", pages_attempted=2, pages_indexed=1)
    second = store.increment_counters("c1", pages_attempted=3)
    assert (first.pages_attempted, first.pages_indexed) == (2, 1)
    assert (second.pages_attempted, second.pages_indexed) == (5, 1)


@needs_redis
def test_flusher_seam_buffers_and_stats(store: RedisFrontierStore) -> None:
    store.enqueue("https://example.com/a", 0)
    store.enqueue("https://example.com/b", 0)
    store.mark_done("https://example.com/a")
    store.mark_failed("https://example.com/b", "boom")
    first = store.drain_pending_flush(3)
    assert [record.kind for record in first] == ["enqueued", "enqueued", "done"]
    rest = store.drain_pending_flush()
    assert [record.kind for record in rest] == ["failed"]
    assert store.drain_pending_flush() == []
    stats = store.snapshot_stats()
    assert stats == {
        "enqueued": 2,
        "claimed": 0,
        "done": 1,
        "failed": 1,
        "skipped": 0,
        "backoff": 0,
    }


@needs_redis
def test_close_flushes_and_is_idempotent(store: RedisFrontierStore) -> None:
    store.enqueue("https://example.com/a", 0)
    assert len(store.drain_pending_flush(0)) == 0
    store.close()
    store.close()
    assert store.drain_pending_flush() == []


# --- PASS-2 flusher tests (offline: SQLite mirror + stub source) --------------
#
# The write-behind contract does not need a live Redis: the flusher only
# touches the source through ``drain_pending_flush`` / ``load_checkpoint``
# / ``get_settings`` / ``requeue_flush`` and the mirror through the
# ``FrontierStore`` ABC, so a SQLite mirror plus a stub source exercise
# the whole path with no services.


class _StubSource:
    """In-memory stand-in for the Redis hot path's flusher seam."""

    def __init__(self, records: list | None = None) -> None:
        from collections import deque

        self._pending: deque = deque(records or [])
        self._checkpoints: dict[str, object] = {}
        self._settings: dict[str, dict] = {}

    def drain_pending_flush(self, batch_size: int = 1000) -> list:
        out = []
        while self._pending and len(out) < max(0, int(batch_size)):
            out.append(self._pending.popleft())
        return out

    def requeue_flush(self, records: object) -> None:
        for record in reversed(list(records or [])):  # type: ignore[arg-type]
            self._pending.appendleft(record)

    def load_checkpoint(self, crawl_id: str = "default"):  # type: ignore[no-untyped-def]
        return self._checkpoints.get(crawl_id)

    def get_settings(self, settings_id: str = "default") -> dict:
        return dict(self._settings.get(settings_id, {}))


def _flusher_records() -> list:
    from openlens.storage.frontier_redis import FlushRecord

    return [
        FlushRecord(kind="enqueued", url="https://example.com/a", depth=0, priority=5),
        FlushRecord(kind="enqueued", url="https://example.com/b", depth=1, priority=3),
        FlushRecord(
            kind="done",
            url="https://example.com/a",
            depth=0,
            priority=5,
            etag="e1",
        ),
        FlushRecord(
            kind="failed",
            url="https://example.com/b",
            depth=1,
            priority=3,
            attempts=1,
            last_error="500 boom",
        ),
        FlushRecord(kind="enqueued", url="https://example.com/c", depth=0),
        FlushRecord(kind="skipped", url="https://example.com/c", depth=0),
    ]


def _sqlite_mirror() -> SqliteFrontierStore:
    return SqliteFrontierStore(":memory:")


def test_flusher_batches_terminal_transitions() -> None:
    from openlens.storage.frontier_flush import FrontierMirrorFlusher

    mirror = _sqlite_mirror()
    flusher = FrontierMirrorFlusher(_StubSource(_flusher_records()), mirror, 5.0)
    assert flusher.flush_once() == 6
    assert flusher.flush_once() == 0
    assert mirror.status_of("https://example.com/a") == "done"
    assert mirror.status_of("https://example.com/b") == "failed"
    assert mirror.status_of("https://example.com/c") == "skipped"
    assert mirror.counts()["done"] == 1
    assert mirror.counts()["failed"] == 1
    entry = mirror.entry_of("https://example.com/a")
    assert entry is not None and entry.etag == "e1"
    failed = mirror.failed(limit=10)
    assert [row.url for row in failed] == ["https://example.com/b"]
    mirror.close()


def test_flusher_replays_checkpoints_and_settings() -> None:
    from openlens.storage.frontier import CrawlCheckpoint
    from openlens.storage.frontier_flush import FrontierMirrorFlusher
    from openlens.storage.frontier_redis import FlushRecord

    source = _StubSource(
        [
            FlushRecord(kind="checkpoint", url="default"),
            FlushRecord(kind="settings", url="default"),
        ]
    )
    source._checkpoints["default"] = CrawlCheckpoint(
        crawl_id="default", pages_attempted=7, pages_indexed=5
    )
    source._settings["default"] = {"paused": True}
    mirror = _sqlite_mirror()
    assert FrontierMirrorFlusher(source, mirror, 5.0).flush_once() == 2
    checkpoint = mirror.load_checkpoint("default")
    assert checkpoint is not None and checkpoint.pages_attempted == 7
    assert mirror.get_settings("default") == {"paused": True}
    mirror.close()


def test_flush_on_shutdown_drains_all() -> None:
    from openlens.storage.frontier_flush import (
        FrontierMirrorFlusher,
        stop_and_flush_mirror,
    )

    assert stop_and_flush_mirror(None) == 0
    mirror = _sqlite_mirror()
    flusher = FrontierMirrorFlusher(_StubSource(_flusher_records()), mirror, 5.0)
    assert stop_and_flush_mirror(flusher) == 6
    assert mirror.counts()["done"] == 1
    assert mirror.status_of("https://example.com/b") == "failed"
    mirror.close()


def test_mirror_lag_metric_under_sla() -> None:
    import math

    from openlens.storage.frontier_flush import (
        FRONTIER_MIRROR_LAG_GAUGE,
        FrontierMirrorFlusher,
        frontier_mirror_lag_seconds,
    )

    assert FRONTIER_MIRROR_LAG_GAUGE == "frontier_mirror_lag_seconds"
    mirror = _sqlite_mirror()
    flusher = FrontierMirrorFlusher(_StubSource(_flusher_records()), mirror, 5.0)
    assert math.isinf(flusher.mirror_lag_s)
    flusher.flush_once()
    # Staleness bound: one successful pass resets the gauge well under SLA.
    assert flusher.mirror_lag_s <= flusher.interval_s
    assert frontier_mirror_lag_seconds() <= 5.0
    mirror.close()


def test_ops_reads_serve_from_mirror_with_documented_lag() -> None:
    from openlens.storage.frontier_flush import FrontierMirrorFlusher

    mirror = _sqlite_mirror()
    flusher = FrontierMirrorFlusher(_StubSource(_flusher_records()), mirror, 5.0)
    flusher.flush_once()
    # Every ops read the dashboard serves stays on the mirror: failed
    # rollups, queued-domain views, requeue/purge convergence, status.
    domains = mirror.failed_domains(limit=10)
    assert [(host, count) for host, count, _ in domains] == [("example.com", 1)]
    assert mirror.failed_domain_count() == 1
    assert mirror.queued_domain_count() == 0
    assert mirror.counts()["done"] == 1
    assert flusher.mirror_lag_s <= flusher.interval_s
    mirror.close()


def test_flusher_requeues_unapplied_tail_on_mirror_error() -> None:
    from openlens.storage.frontier_flush import FrontierMirrorFlusher

    class _FlakyMirror(SqliteFrontierStore):
        def __init__(self) -> None:
            super().__init__(":memory:")
            self.calls = 0

        def mark_done(self, url: str, **kwargs) -> None:  # type: ignore[no-untyped-def]
            self.calls += 1
            if self.calls == 1:
                raise ProviderError(
                    "mirror down",
                    provider_name="custom_index",
                    code=ProviderError.UNAVAILABLE,
                )
            super().mark_done(url, **kwargs)

    source = _StubSource(_flusher_records())
    mirror = _FlakyMirror()
    flusher = FrontierMirrorFlusher(source, mirror, 5.0)
    with pytest.raises(ProviderError):
        flusher.flush_once()
    # The failed record and everything after it went back to the head.
    kinds = [record.kind for record in source.drain_pending_flush(100)]
    assert kinds[0] == "done"
    assert len(kinds) == 4  # done + failed + enqueued(c) + skipped(c)
    mirror.close()


def test_open_mirror_flusher_none_for_default_backend() -> None:
    from openlens.storage.frontier_flush import open_mirror_flusher

    assert open_mirror_flusher({}) is None
    assert open_mirror_flusher({"frontier": {"backend": "postgres"}}) is None
    assert open_mirror_flusher({"frontier": {"backend": "sqlite"}}) is None
    assert open_mirror_flusher("bogus") is None  # type: ignore[arg-type]


class _StubHotStore:
    """In-memory stand-in for the Redis hot side of ``backfill_once``.

    Mirrors the real dedup (first enqueue wins) and the claim/done
    marker contract so the offline tests exercise the replay logic
    with a real SQLite mirror and no services.
    """

    def __init__(self) -> None:
        self.enqueued: list[tuple] = []
        self._known: set[str] = set()
        self._locked = False
        self._done = False

    def backfill_done(self) -> bool:
        return self._done

    def try_claim_backfill(self, ttl_s: float = 600.0) -> bool:
        del ttl_s
        if self._locked or self._done:
            return False
        self._locked = True
        return True

    def mark_backfill_done(self, ttl_s: float = 30.0 * 86400.0) -> None:
        del ttl_s
        self._done = True
        self._locked = False

    def release_backfill_lock(self) -> None:
        """Test-only TTL expiry: the claim lapses, done stays unset."""

        self._locked = False

    def enqueue(
        self,
        url: str,
        depth: int = 0,
        parent_url: str | None = None,
        priority: int | None = None,
    ) -> bool:
        from openlens.storage.frontier import normalize_frontier_url

        norm = normalize_frontier_url(url)
        if norm in self._known:
            return False
        self._known.add(norm)
        self.enqueued.append((norm, depth, parent_url, priority))
        return True


def _queued_mirror(urls: list[tuple[str, int, int]]) -> SqliteFrontierStore:
    """SQLite mirror preloaded with queued rows: (url, depth, priority)."""
    from openlens.storage.frontier import SqliteFrontierStore

    mirror = SqliteFrontierStore(":memory:")
    for url, depth, priority in urls:
        assert mirror.enqueue(url, depth, None, priority) is True
    return mirror


def test_backfill_replays_mirror_queued_rows_once() -> None:
    from openlens.storage.frontier_flush import FrontierMirrorFlusher

    mirror = _queued_mirror(
        [
            ("https://example.com/a", 0, 5),
            ("https://example.com/b", 2, 3),
            ("https://example.com/c", 1, 0),
        ]
    )
    try:
        hot = _StubHotStore()
        flusher = FrontierMirrorFlusher(hot, mirror, 5.0)
        assert flusher.backfill_once() == 3
        by_url = {url: (depth, prio) for url, depth, _, prio in hot.enqueued}
        assert by_url["https://example.com/a"] == (0, 5)
        assert by_url["https://example.com/b"] == (2, 3)
        assert by_url["https://example.com/c"] == (1, 0)
        # Sealed: a second boot is a no-op.
        assert flusher.backfill_once() == 0
        assert len(hot.enqueued) == 3
    finally:
        mirror.close()


def test_backfill_loser_skips_when_lock_held() -> None:
    from openlens.storage.frontier_flush import FrontierMirrorFlusher

    mirror = _queued_mirror([("https://example.com/a", 0, 1)])
    try:
        hot = _StubHotStore()
        assert hot.try_claim_backfill() is True  # another worker owns it
        flusher = FrontierMirrorFlusher(hot, mirror, 5.0)
        assert flusher.backfill_once() == 0
        assert hot.enqueued == []
    finally:
        mirror.close()


def test_backfill_cap_leaves_remainder_unsealed() -> None:
    from openlens.storage.frontier_flush import FrontierMirrorFlusher

    mirror = _queued_mirror([(f"https://example.com/{i}", 0, 0) for i in range(5)])
    try:
        hot = _StubHotStore()
        flusher = FrontierMirrorFlusher(hot, mirror, 5.0)
        assert flusher.backfill_once(batch_size=100, max_urls=2) == 2
        assert hot.backfill_done() is False
        # Lock expiry lets a later boot resume the remainder; the two
        # already-replayed rows dedupe instead of double-counting.
        hot.release_backfill_lock()
        assert flusher.backfill_once(batch_size=100, max_urls=100) == 3
        assert hot.backfill_done() is True
        assert len({url for url, _, _, _ in hot.enqueued}) == 5
    finally:
        mirror.close()


def test_backfill_pages_past_first_batch() -> None:
    """Offset paging must drain the whole backlog, not the first page.

    Regression: offset-0 repaging re-read the same first page, so a
    62k-row mirror sealed after 1000 URLs with the rest stranded.
    """
    from openlens.storage.frontier_flush import FrontierMirrorFlusher

    mirror = _queued_mirror([(f"https://example.com/{i}", i % 3, 0) for i in range(7)])
    try:
        hot = _StubHotStore()
        flusher = FrontierMirrorFlusher(hot, mirror, 5.0)
        assert flusher.backfill_once(batch_size=2) == 7
        assert hot.backfill_done() is True
        assert len({url for url, _, _, _ in hot.enqueued}) == 7
    finally:
        mirror.close()


def test_backfill_noop_without_source_seam() -> None:
    from openlens.storage.frontier_flush import FrontierMirrorFlusher

    mirror = _queued_mirror([("https://example.com/a", 0, 0)])
    try:
        flusher = FrontierMirrorFlusher(object(), mirror, 5.0)
        assert flusher.backfill_once() == 0
    finally:
        mirror.close()


def test_backfill_never_raises_on_unreadable_mirror() -> None:
    from openlens.storage.frontier_flush import FrontierMirrorFlusher

    class _BrokenMirror:
        def pending(self, limit: int = 500, offset: int = 0):  # type: ignore[no-untyped-def]
            raise RuntimeError("mirror down")

    hot = _StubHotStore()
    flusher = FrontierMirrorFlusher(hot, _BrokenMirror(), 5.0)
    assert flusher.backfill_once() == 0
    assert hot.enqueued == []


@needs_redis
def test_queue_depth_counts_ready_plus_active(
    store: RedisFrontierStore,
) -> None:
    """queue_depth tracks queued+active without the full-scan counts."""
    assert store.enqueue("https://example.com/a", 0) is True
    assert store.enqueue("https://example.com/b", 0) is True
    assert store.enqueue("https://example.com/c", 0) is True
    assert store.queue_depth() == 3
    entry = store.dequeue()
    assert entry is not None
    assert store.queue_depth() == 3  # active rows still count
    store.mark_done(entry.url)
    assert store.queue_depth() == 2


@needs_redis
def test_queue_depth_skips_full_scan(
    store: RedisFrontierStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """queue_depth must stay O(1) even when counts() is unusable."""
    assert store.enqueue("https://example.com/a", 0) is True

    def _boom() -> dict:
        raise AssertionError("full scan must not run in queue_depth")

    monkeypatch.setattr(store, "counts", _boom)
    assert store.queue_depth() == 1


class _MemoryPipeline:
    """Queue client calls like redis-py, then run them in order."""

    def __init__(self, client: "_MemoryRedis") -> None:
        self._client = client
        self._cmds: list[tuple] = []

    def hset(self, *args: object, **kwargs: object) -> "_MemoryPipeline":
        self._cmds.append(("hset", args, kwargs))
        return self

    def hgetall(self, *args: object, **kwargs: object) -> "_MemoryPipeline":
        self._cmds.append(("hgetall", args, kwargs))
        return self

    def zadd(self, *args: object, **kwargs: object) -> "_MemoryPipeline":
        self._cmds.append(("zadd", args, kwargs))
        return self

    def incr(self, *args: object, **kwargs: object) -> "_MemoryPipeline":
        self._cmds.append(("incr", args, kwargs))
        return self

    def execute(self) -> list[object]:
        return [
            getattr(self._client, name)(*args, **kwargs)
            for name, args, kwargs in self._cmds
        ]


class _MemoryRedis:
    """Minimal client with true sorted-set index semantics.

    ZRANGE orders by (score, member) ascending and counts negative
    indexes from the tail — the documented server behavior the store
    relies on, so a fetch-window regression fails here as in prod.
    Hash values are stored as strings like ``decode_responses=True``.
    """

    def __init__(self) -> None:
        self.sets: dict[str, set[str]] = {}
        self.strings: dict[str, int] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.zsets: dict[str, dict[str, float]] = {}

    def sadd(self, key: str, *members: str) -> int:
        values = self.sets.setdefault(key, set())
        added = 0
        for member in members:
            if member not in values:
                values.add(member)
                added += 1
        return added

    def incr(self, key: str, amount: int = 1) -> int:
        self.strings[key] = self.strings.get(key, 0) + amount
        return self.strings[key]

    def hset(self, key: str, mapping: dict | None = None, **kwargs: object) -> int:
        fields = self.hashes.setdefault(key, {})
        items = dict(mapping or {})
        items.update(kwargs)
        for field, value in items.items():
            fields[str(field)] = str(value)
        return len(items)

    def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    def zadd(self, key: str, mapping: dict[str, float]) -> int:
        zset = self.zsets.setdefault(key, {})
        added = 0
        for member, score in mapping.items():
            if member not in zset:
                added += 1
            zset[member] = float(score)
        return added

    def zrange(self, key: str, start: int, stop: int) -> list[str]:
        ordered = [
            member
            for member, _ in sorted(
                self.zsets.get(key, {}).items(), key=lambda kv: (kv[1], kv[0])
            )
        ]
        total = len(ordered)
        if start < 0:
            start = max(total + start, 0)
        if stop < 0:
            stop = total + stop
        stop = min(stop, total - 1)
        if start > stop:
            return []
        return ordered[start : stop + 1]

    def zcard(self, key: str) -> int:
        return len(self.zsets.get(key, {}))

    def pipeline(self) -> _MemoryPipeline:
        return _MemoryPipeline(self)


def _memory_store() -> RedisFrontierStore:
    """Store wired to the in-memory client (no server needed)."""
    from collections import deque

    store = RedisFrontierStore.__new__(RedisFrontierStore)
    store._prefix = "fw-test-mem"
    store._lock = threading.Lock()
    store._client = _MemoryRedis()
    store._lease_ttl_s = 120
    store._pending = deque()  # type: ignore[attr-defined]
    return store


def test_pending_shows_drain_head_not_tail() -> None:
    """The queue view must list what workers take next (highest score).

    Regression: pending() fetched ZRANGE 0..limit (the lowest scores),
    so with a deep backlog the admin list froze on rows that drain
    last while the depth count moved. Workers claim via ZPOPMAX.
    """
    store = _memory_store()
    for i in range(30):
        assert store.enqueue(f"https://example.com/{i}", 0) is True
    assert store.enqueue("https://example.com/urgent", 0, priority=9) is True
    head = store.pending(limit=5)
    assert len(head) == 5
    assert head[0].url.endswith("/urgent")


@needs_redis
def test_pending_shows_drain_head_live(store: RedisFrontierStore) -> None:
    """Same drain-head assertion against a real Redis (see above)."""
    for i in range(10):
        assert store.enqueue(f"https://example.com/{i}", 0) is True
    assert store.enqueue("https://example.com/urgent", 0, priority=9) is True
    head = store.pending(limit=5)
    assert len(head) == 5
    assert head[0].url.endswith("/urgent")

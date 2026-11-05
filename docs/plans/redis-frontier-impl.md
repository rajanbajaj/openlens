# Redis Frontier — File-Level Execution Spec (implements `docs/plans/redis-frontier.md` steps 1–6)

Scope: what an implementer edits, in what order, with what acceptance gates.
Source of truth for behavior: `docs/plans/redis-frontier.md`. Evidence base:
prior-0 (ABC/factory/config/cache-Redis pattern), prior-1 (worker/crawler call sites +
behavior matrix for `test_frontier_redis.py`), prior-2 (compose/cache/Architecture/Ops-routes/
conformance-spec/fake-Redis/tests/CI/indexes). Gaps inherited: none open — all three
prior results `complete:true`; residual non-blocking notes (no live Postgres-claim vs Redis
comparison, `test_frontier_redis.py` does not exist yet) are build tasks below, not spec gaps.

## 1. Config keys (exact)

### 1a. `frontier.backend` + `frontier.redis` (`configs/default.yaml`, ~lines 284–296 today)

Keep every existing `frontier:` key unchanged (`enabled`, `crawl_id`, `checkpoint_interval_pages`,
`max_attempts_per_url`, `queue_table`, `checkpoint_table`, `lease_ttl_s`, `heartbeat_interval_s`,
`reaper_interval_s`). Add:

```yaml
frontier:
  backend: ${OPENLENS_FRONTIER_BACKEND:-postgres}   # NEW; default MUST stay postgres
  redis:                                            # NEW section
    url: ${OPENLENS_FRONTIER_REDIS_URL:-redis://localhost:6379/1}
    key_prefix: ${OPENLENS_FRONTIER_REDIS_PREFIX:-fw}
    flush_interval_s: ${OPENLENS_FRONTIER_FLUSH_S:-5}
    lease_ttl_s: ${OPENLENS_FRONTIER_LEASE_TTL_S:-120}   # passthrough; same default as
    heartbeat_interval_s: ${OPENLENS_HEARTBEAT_INTERVAL_S:-40}  # frontier.lease_ttl_s
```

Rules: `backend` accepts `sqlite|postgres|redis` only (unknown → `ConfigError`, mirroring
`cache/factory.py:validate_cache_section`). `redis.url` default DB index `1` (cache uses `0`;
separate DB index required by plan step 6). `key_prefix` default `fw`; all key layouts below
are `{prefix}:...`. Empty/non-string `url` → `ProviderError(UNAVAILABLE)` at factory time.
`flush_interval_s` must be positive number; `lease_ttl_s` positive int.

### 1b. `storage:` section untouched

`open_frontier_store(config)` today keys off top-level `storage:` (`backend`, `sqlite.db_path`,
`postgres.*`; see `src/openlens/storage/frontier.py:4774-4804`). Do NOT move it. The new
`frontier.backend: redis` is a second selector read by the crawler/frontier wiring (worker +
`main.py build_engine` area): when `frontier.backend == redis`, construct `RedisFrontierStore`
from `frontier.redis`; otherwise fall through to the existing `open_frontier_store(config)`.
`frontier.enabled` keeps gating the crawl path; DB row in `frontier_settings` still wins once
written (header comment `default.yaml:277-283`).

## 2. `RedisFrontierStore` public method list (must match the ABC)

New file: `src/openlens/storage/frontier_redis.py`, class `RedisFrontierStore(FrontierStore)`.
The ABC lives at `src/openlens/storage/frontier.py:184` with 28 abstract members; implement
ALL of them with identical signatures (do not rename/omit):

`enqueue(url, depth=0, parent_url=None, priority=None)->bool` (:188) ·
`dequeue()->FrontierEntry|None` (:206) ·
`mark_done(url, *, etag=None, last_modified=None)->None` (:217) ·
`mark_failed(url, error)->None` (:228) ·
`mark_skipped(url)->int` (:258) ·
`requeue_active()->int` (:284) ·
`requeue(url)->bool` (:292) ·
`clear_queued()->int` (:314) ·
`is_known(url)->bool` (:324) ·
`pending(limit=500, offset=0)` (:431) ·
`failed(limit=100, offset=0)` (:436) ·
`failed_domains(limit=100, offset=0)->list[tuple[str,int,str]]` (:444) ·
`failed_domain_count()->int` (:456) ·
`failed_domain_urls(domain, limit=100, offset=0)` (:461) ·
`failed_domain_url_count(domain)->int` (:473) ·
`queued_domain_urls(domain, limit=100, offset=0)` (:478) ·
`queued_domain_url_count(domain)->int` (:490) ·
`requeue_failed_domains(domains)->int` (:495) ·
`purge_failed_domains(domains)->int` (:505) ·
`queued_domains(limit=100, offset=0)->list[tuple[str,int,int]]` (:514) ·
`queued_domain_count()->int` (:527) ·
`bump_queued_priority(domains, delta=1)->int` (:532) ·
`counts()->dict[str,int]` (:541) ·
`save_checkpoint(checkpoint)->None` (:546) ·
`load_checkpoint(crawl_id=DEFAULT_CRAWL_ID)->CrawlCheckpoint|None` (:551) ·
`get_settings(settings_id=DEFAULT_SETTINGS_ID)->dict` (:558) ·
`save_settings(payload, settings_id=DEFAULT_SETTINGS_ID)->None` (:563) ·
`close()->None` (:570).

Inherited concrete defaults come free (no override needed unless Redis makes them cheaper):
`mark_backoff/dequeue_backoff` (:232/:246), `skip_queued_domains` (:267), `requeue_stale` (:299),
`status_of/statuses_of/entry_of` (:328/:337/:359), `reseed/reseed_many` (:368/:380),
`submit_crawl_request/list_crawl_requests/set_crawl_request_status` (:401/:416/:422),
`claim/heartbeat/reap_expired_leases/requeue_owned/owned` (:582/:598/:612/:629/:640),
`reserve_politeness/get_politeness_delay` (:646/:664),
`register_worker/heartbeat_worker/list_workers/prune_workers` (:669/:677/:687/:693),
`increment_counters` (:702). Hot-path overrides REQUIRED anyway: `claim` (SET NX PX),
`heartbeat` (PEXPIRE), `reap_expired_leases` (TTL-expiry is automatic; method scans claims),
`mark_backoff/dequeue_backoff` (backoff ZSET). Dataclasses reused unchanged:
`FrontierEntry` (:151), `CrawlCheckpoint` (:170); constants reused:
`STATUS_*` (:64-72), `DEFAULT_LEASE_TTL_S=120` (:54), `DEFAULT_HEARTBEAT_INTERVAL_S=40` (:58).

Connection pattern (copy `src/openlens/cache/redis_backend.py:22-66` + `cache/factory.py:53-102`):
lazy `import redis` → `ProviderError(UNAVAILABLE)` when missing; `redis.Redis.from_url(url,
socket_timeout=...)`; `ping` health-check gate at factory time → `ProviderError(UNAVAILABLE)`
when unreachable; best-effort `close()`. No `fakeredis`/`redislite` (repo has zero references;
do not add new test doubles — conformance tests skip without a live URL).

Input validation: fail closed on bad input exactly like `_check_filter_args` (invalid
limit/offset/domain shapes raise before any Redis call).

## 3. Key layout (exact; `{p}` = `key_prefix`, default `fw`)

| Key | Type | Members / fields | Used by |
|---|---|---|---|
| `{p}:claim:<norm-url-hash>` | STRING (owner worker-id, `PX lease_ttl_s*1000`, `NX` on acquire) | value = worker id | `claim` (SET NX PX), `heartbeat` (PEXPIRE), terminal transitions (DEL) |
| `{p}:seen` | SET | members = normalized URLs | `enqueue` dedup (SADD; 0 = known → False), `is_known` (SISMEMBER) |
| `{p}:ready:<worker-or-global>` | ZSET score = `priority * 1_000_000 − depth` | member = normalized URL | `enqueue` (ZADD), `dequeue/claim` (ZPOPMIN path), priority-order claim |
| `{p}:meta:<norm-url-hash>` | HASH + TTL | `attempts,last_error,etag,last_modified,depth,parent_url,priority,domain,status,owner,leased_at` | `mark_done/mark_failed/mark_backoff/entry_of/status_of`, attempts budget |
| `{p}:backoff` | ZSET score = `not_before_ts` (unix sec) | member = normalized URL | `mark_backoff` (ZADD), `dequeue_backoff` (ZRANGEBYSCORE ≤ NOW) |
| `{p}:stats:` + name | STRING counters via INCR | `enqueued,claimed,done,failed,skipped,backoff` | `counts()`, `increment_counters`, flusher input |

Normalization: reuse the frontier normalized-URL function (same PK-collapse semantics as
Postgres PK dedup). Score formula preserves `priority DESC, depth ASC` without a table scan
(plan step 2). Entry HASH gets a TTL ≥ lease TTL so crashed claims self-clean; Kafka
redelivery + mirror rebuild cover the rest (plan Crash Persistence).

## 4. Flusher seam (what PASS-2 consumes from PASS-1)

PASS-1 MUST expose (no Postgres writes from the hot path yet):

- `RedisFrontierStore.drain_pending_flush(batch_size) -> list[FlushRecord]` — terminal
  transitions + discovery inserts buffered since last drain (in-memory deque inside the store,
  appended by `mark_done/mark_failed/mark_skipped/enqueue` alongside the Redis write).
- `FlushRecord` dataclass (new, in `frontier_redis.py`): `kind ∈ {done,failed,skipped,
  enqueued,checkpoint,settings}`, `url, depth, parent_url, priority, attempts, last_error,
  etag, last_modified, owner, ts`.
- `RedisFrontierStore.snapshot_stats() -> dict[str,int]` — current `{p}:stats:*` counters
  (INCR-maintained); flusher diffs and writes deltas to Postgres.
- `close(flush=True)` param: flush-on-shutdown path calls `drain_pending_flush` to empty.

PASS-2 builds `FrontierMirrorFlusher` (new file `src/openlens/storage/frontier_flush.py`):
`Flusher(source: RedisFrontierStore, mirror: PostgresFrontierStore, interval_s)` loop —
`drain_pending_flush` → batched mirror writes (`mark_done/mark_failed/...` on Postgres) +
`save_checkpoint` every interval; exposes `frontier_mirror_lag_seconds` gauge
(now − last successful flush). All ops reads (`failed*`, `queued_domain*`, `requeue*`,
`purge*`, `clear_queued`, checkpoints, settings, crawl requests) stay on the Postgres mirror
implementations unchanged (plan step 4).

## 5. Test file plan (mirror `tests/unit/test_frontier.py`)

New `tests/unit/test_frontier_redis.py` (does not exist today). Offline-safe convention
(same as `tests/integration/test_cache_redis.py:20` + `tests/unit/test_cache.py:98,174-182`):
runs against live Redis ONLY when `OPENLENS_TEST_REDIS_URL` is set, else `pytest.skip`;
unreachable-backend test asserts `ProviderError(UNAVAILABLE)` on `127.0.0.1:6399`.
Behavior matrix (from prior-1 inspected call sites; map each to a test):

- Priority: `test_enqueue_priority_orders_dequeue_before_depth`,
  `test_enqueue_priority_none_keeps_explicit_overwrites`,
  `test_domains_priority_bumps_only_queued`,
  `test_frontier_priority_index_covers_claim_ordering` (ZSET score formula).
- Leases/expiry: `test_claim_records_owner_and_lease`, `test_claim_rejects_empty_worker_id`,
  `test_no_double_claim_under_thread_concurrency`, `test_heartbeat_refreshes_owned_rows_only`,
  `test_reap_recovers_expired_but_never_steals_live`,
  `test_reap_recovers_legacy_ownerless_rows`, `test_requeue_owned_releases_only_that_worker`,
  `test_worker_lease_shutdown_releases_in_flight`, `test_worker_lease_context_manager`,
  `test_two_workers_claim_distinct_urls_without_collision`,
  `test_pause_reaps_expired_leases_but_keeps_live_ones`.
- Attempts (3-budget + `test_crawler_rejects_bad_frontier_config`; crawler retry branches
  `crawler.py:2335-2377`): `test_failed_lists_reasons_most_retried_first`,
  `test_domains_requeue_revives_with_reset_attempts`.
- Backoff: `test_backoff_rows_hide_from_main_dequeue`,
  `test_run_kafka_worker_backoff_mode_drains_backoff_pool`,
  `test_run_kafka_worker_main_hands_off_429_to_backoff`,
  `test_backoff_frontier_view_serves_only_backoff_rows` (+ `_BackoffFrontier` 415-424,
  `_SkipGuardFrontier` 371-377 with `MAX_SKIPS_PER_CLAIM=100`).
- Dedup/discovery/checkpoints: `test_enqueue_dedups_normalized_urls`,
  `test_worker_enqueue_new_urls_dedups`, `test_pause_then_resume_avoids_refetch`,
  `test_crawl_through_store_marks_done_and_checkpoints`,
  `test_enqueue_seeds_repeat_pens_known_url`, `test_checkpoint_and_settings_round_trip`,
  `test_discovery_pass_enqueues_only_new_urls`, `test_discovery_pass_handles_sitemaps`,
  `test_discovery_pass_refreshes_stale_done` (+ `requeue_stale` cases from
  `test_frontier_refresh.py`).
- Flusher (PASS-2): `test_flusher_batches_terminal_transitions`,
  `test_flush_on_shutdown_drains_all`, `test_mirror_lag_metric_under_sla`,
  `test_ops_reads_serve_from_mirror_with_documented_lag`.

## 6. Compose changes (`deploy/local/docker-compose.yml`)

Existing `redis` service (lines 477-494: `redis:7-alpine`, `--appendonly yes --maxmemory
256mb --maxmemory-policy allkeys-lru`, `redis-data` volume, `redis-cli ping` healthcheck) is
the CACHE Redis — do not alter its eviction. Add a SECOND service `redis-frontier`
(plan step 6): `image redis:7-alpine`, `command redis-server --appendonly yes
--appendonly everysec --maxmemory <sized from frontier_queue row count × ~500 B/key>
--maxmemory-policy noeviction`, separate named volume `redis-frontier-data`, same
`redis-cli ping` healthcheck shape, port mapped from `${REDIS_FRONTIER_PORT:-6380}`.
Crawler/backoff services get `OPENLENS_FRONTIER_REDIS_URL=redis://redis-frontier:6379/0`
(alongside existing `OPENLENS_REDIS_URL=redis://redis:6379/0` at lines 36,164,231) and
`depends_on: redis-frontier: {condition: service_healthy}` mirroring lines
66-67/182-183/254-255. Architecture table (`docs/wiki/Architecture.md:85-96`) gains the new row.

## 7. Docs updates

- `docs/wiki/Crawler-and-Frontier-Ops.md` (207 lines; routes verified in
  `src/openlens/api/routes.py:1454-2915`): add Staleness SLA box — every ops read
  (`/frontier/failed*`, `/frontier/queued/domains`, `/frontier/status`, auto-triage
  1952-2088) serves the Postgres mirror, lag ≤ `flush_interval_s` (default 5 s);
  requeue/purge/skip converge via the mirror. No route changes.
- `docs/wiki/Architecture.md` services table: `redis-frontier` row (frontier leases/queues,
  persisted AOF, `noeviction`) beside the existing cache-`redis` ephemeral row.
- Storage live-conformance spec (`docs/workflow/v1/storage-live-conformance_spec.json`):
  note it covers mysql/postgres only — Redis frontier conformance is the new
  `test_frontier_redis.py` matrix above, not that spec.

## 8. PASS split + gates

**PASS-1** (config + factory + store hot path + unit tests; backend gates green):
1. `configs/default.yaml` `frontier.backend` + `frontier.redis` keys (§1a).
2. `src/openlens/storage/frontier_redis.py` — full 28-method ABC surface; hot path on
   §3 layout; `drain_pending_flush`/`snapshot_stats` seam (§4); lazy-driver +
   `ProviderError(UNAVAILABLE)` + `ping` gate.
3. Factory wiring (§1b) with unknown-backend `ConfigError`.
4. `tests/unit/test_frontier_redis.py` behavior matrix (§5, minus flusher tests) with
   `OPENLENS_TEST_REDIS_URL` skip convention.
   Gates: `pytest tests/unit/test_frontier_redis.py tests/unit/test_frontier.py -q`,
   `ruff check`, `ruff format --check`, `mypy src` — all green WITHOUT compose changes.

**PASS-2** (flusher + mirror wiring + compose + docs + full gates incl. admin re-run):
1. `src/openlens/storage/frontier_flush.py` + shutdown-flush wiring + `frontier_mirror_lag_seconds`.
2. Ops reads pinned to mirror (§4); staleness SLA docs (§7).
3. `redis-frontier` compose service + env/depends_on (§6); Architecture table row.
4. Shadow dual-write → cutover rollout (plan step 5); rollback = flip `frontier.backend`
   back (leases TTL out; Kafka redelivers in-flight).
   Gates: full `.venv/bin/python -m pytest tests -q`, `ruff check`/`format --check`,
   `mypy src`, PLUS admin suite re-run (ops reads flow through the mirror) and the
   load check (Postgres write QPS ≥10x down, search p99 flat, `kafka_lag_total` drains,
   mirror lag under SLA). Highest-risk gate: shadow-comparison count divergence blocks cutover.

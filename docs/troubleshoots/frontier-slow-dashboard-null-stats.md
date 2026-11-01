# Frontier dashboard slow (>5s status/WS/crawl-requests) with `stats: null`

Date: 2026-11-01 · Fixed in v0.4.34 (follows
[crawler-zero-workers-idle-heartbeat](crawler-zero-workers-idle-heartbeat.md))

## Symptoms

- `GET /api/v1/frontier/status`, the `WS /api/v1/frontier/live` socket, and
  the crawl-request review endpoints take seconds per call while crawling
  continues and the store is not paused.
- Workers beat with `claimed: 0`, `stats: null`, `pages_per_min: null`
  even though the queue keeps churning (`done`/`skipped` grow between polls).
- `checkpoint` stays `{phase: running, pages_attempted: 0, pages_indexed: 0}`
  (normal mid-pass — it persists at pass end only, not per page).
- Crawler containers sit in `do_sys_poll` with only internal sockets.

## Root causes (two, compounding)

1. **No index matched the claim/pending ordering.** Both `claim()`
   (`WHERE status='queued' ORDER BY priority DESC, depth ASC,
   discovered_at ASC LIMIT 1 FOR UPDATE SKIP LOCKED`) and `pending()`
   sort priority-first, but the only index was
   `(status, depth, discovered_at)`. On a ~100k-deep queue every claim,
   dashboard poll, WS push, and crawl-request review paid a full sort
   under write contention — claims slowed to a crawl, so workers finished
   zero pages and heartbeats omitted the stats fragment (`stats` is only
   sent after `cumulative_pages/tokens/errors > 0`).
2. **N+1 terminal-row check in the status overlay.** Each status poll and
   2s WS push ran one `SELECT status` per in-memory snapshot seed to drop
   already-finished seeds. After a large seed paste that is thousands of
   round trips per refresh.

## Fix (v0.4.34 / v0.4.35)

v0.4.34:

- `src/openlens/storage/frontier.py`: new
  `idx_frontier_status_priority (status, priority DESC, depth ASC,
  discovered_at ASC)` in the Postgres DDL, the Postgres coordination
  migrations (prod gains it on next backend/worker boot — both run the
  ordered schema), and the SQLite migration path. Kept out of the raw
  SQLite DDL script on purpose: that script runs before the legacy column
  backfill, where `priority` does not exist yet.
- Same file: bulk `statuses_of()` (chunked single-query `IN` lookup on
  SQLite/Postgres; looping default on the base class) and
  `src/openlens/api/routes.py`: the overlay now does one bulk terminal-row
  check, falling back to the per-URL loop only for stores without it.

v0.4.35 — the eviction stall underneath the same symptoms:

- `evict_oldest()` runs `UPDATE ... WHERE url IN (SELECT ... WHERE
  status='queued' ORDER BY discovered_at ASC LIMIT N)` on **every drain push
  past the cap** (plus a `COUNT(*)` sizing query per push via
  `BoundedFrontier.__len__`). With the queue pinned at cap that is a full
  100k-row sort per discovered URL x 12 workers — and concurrent evictions
  deadlock each other (see the `deadlock detected` lines in the Postgres
  log). Drains never finish a page, so workers sit in their initial
  `_drain_once()`: only the forced startup beat ever lands (12 backend
  hits ~= 1 per crawler), the registry prunes them after 240 s, and
  status/WS reads queue behind the eviction write locks.
- New `idx_frontier_queued_discovered (status, discovered_at)` in the same
  three schema sources turns eviction into an index-ordered probe and makes
  concurrent evictions take victims in the same deterministic order (far
  fewer deadlocks).

Regression tests: index presence/shape in all three schema sources
(`test_frontier_priority_index_covers_claim_ordering`,
`test_frontier_eviction_index_covers_oldest_first`), bulk lookup semantics
(`test_statuses_of_returns_known_statuses_bulk`), and one-bulk-call overlay
with zero single lookups
(`test_status_overlay_uses_single_bulk_terminal_check`).

## How to confirm on prod

1. Deploy, restart backend + crawlers, `POST /api/v1/frontier/resume` once.
2. `GET /api/v1/frontier/status` and the WS stream return in well under a
   second; the crawl-request review list loads promptly.
3. Within a few minutes workers report `claimed > 0` with populated
   `stats`, `pages_per_min`, and `tokens_per_min`.
4. `SELECT status, COUNT(*) FROM frontier_queue GROUP BY 1;` keeps moving.

## Do not chase these (ruled out for this signature)

- 403 heartbeat auth failures (compare token hashes first).
- Postgres saturation (connections vs `max_connections`, ungranted locks,
  prepared transactions, WAL-replay lag on the replica).
- Kafka consumer-group lag (meaningless when the topic itself is empty).
- `documents` count frozen (expected until the pass-end checkpoint write).
- Eviction-deadlock log lines: self-recovering statement aborts under
  concurrent evictions — a symptom of the unindexed eviction sort fixed in
  v0.4.35, not a separate cause.
- Container CPU/memory: crawlers at ~0.5% CPU are lock-waiting, not
  throttled (`cpus: 1.0` is ample for fetch/parse). The one real resource
  limit, Postgres DSM in Docker's 64MB default `/dev/shm` (`could not
  resize shared memory segment`), is already raised to 256MB in
  `deploy/local/docker-compose.yml`.

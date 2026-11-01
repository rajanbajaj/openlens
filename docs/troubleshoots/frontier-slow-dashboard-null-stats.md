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

## Fix (v0.4.34)

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

Regression tests: priority-index presence/shape in all three schema sources
(`test_frontier_priority_index_covers_claim_ordering`), bulk lookup
semantics (`test_statuses_of_returns_known_statuses_bulk`), and one-bulk-call
overlay with zero single lookups
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
  concurrent evictions, a separate follow-up — not the cause of slow reads.

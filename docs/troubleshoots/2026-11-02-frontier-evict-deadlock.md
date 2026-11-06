# Troubleshoot: Postgres deadlock on concurrent queue-cap evictions

## Symptoms

- Postgres log: `ERROR: deadlock detected`, two `UPDATE
  frontier_queue SET status = 'skipped', last_error = 'evicted:
  queue cap exceeded' ... WHERE url IN (SELECT ... ORDER BY
  discovered_at ASC LIMIT $1)` blocking each other.
- One eviction per incident aborts (the crawler suppresses it as
  best-effort and retries on the next push), so the queue keeps
  working — but the log spam scales with worker count and the
  aborted pass wastes a full COUNT + UPDATE.

## Root cause (found 2026-11-02)

Two workers breaching `max_queued_urls` at once each ran
`UPDATE ... WHERE url IN (SELECT ... ORDER BY discovered_at ASC
LIMIT n)`. Both subqueries pick the same oldest rows, but each
backend locks them in a different physical visit order — a classic
lock-ordering deadlock. Surfaced at 24 workers because concurrent
evictions went from rare to routine.

## Fix (in tree)

`PostgresFrontierStore.evict_oldest` now selects victims through a
CTE with `FOR UPDATE SKIP LOCKED` over a fully deterministic order
(`discovered_at ASC, url ASC`): every concurrent evictor locks rows
in the same global order and skips — never waits on — rows the other
already holds, so the statements cannot deadlock each other.
Overflow accounting is unchanged (one COUNT, one UPDATE).

## How to confirm it is gone

```sql
SELECT count(*) FROM pg_stat_database_conflicts
WHERE datname = current_database();
-- plus: no new `deadlock detected` lines mentioning
-- 'evicted: queue cap exceeded' after the deploy.
```

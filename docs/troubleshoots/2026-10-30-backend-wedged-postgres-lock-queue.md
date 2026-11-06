# Troubleshoot: backend wedged — idle CPU, all HTTP timing out, search 502

## Symptoms

- `GET /healthz`, `POST /api/v1/search`, frontier reads all time out
  (curl `28`), from host AND between containers.
- `docker stats` shows the backend near 0% CPU; Postgres is up.
- The in-container docker healthcheck may still pass briefly, then fail.
- Search may degrade first to `502 provider_timeout` (5s per-provider
  budget) before the full wedge.
- Refreshing the admin dashboard (status polls + `/frontier/live`
  socket every 2s) accelerates the freeze.

## Root cause (found 2026-10-30)

Two compounding defects around Postgres locking:

1. **Leaked read transactions.** The Postgres pools
   (`PostgresDocumentStore`, `PostgresFrontierStore`) returned
   connections after successful SELECTs without `commit()`/`rollback()`.
   psycopg leaves them `idle in transaction`, holding `AccessShare`
   locks on `documents` / `frontier_queue` indefinitely.
2. **Boot-time DDL vs live traffic.** Every process start (backend +
   each crawler) runs `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`
   migrations, which need `AccessExclusive`. One leaked `AccessShare`
   blocks the ALTER; the queued ALTER then blocks *every* later query
   (`Lock: relation` in `pg_stat_activity`) — cascade to a full wedge.

Fix (in tree): both pools roll back on the success path before
recycling (`_release` in `postgres_backend.py`,
`PostgresFrontierStore._connection` in `frontier.py`); regression
tests in `tests/unit/test_postgres_pool_hygiene.py`. Rollback is a
no-op for already-committed writers.

## Diagnose

```bash
# Inside postgres: who waits on what (CHR() avoids shell-quote fights).
docker exec openlens-local-postgres-1 psql -U openlens -d openlens \
  -t -A -c "SELECT pid, state, wait_event_type, wait_event, left(query, 80)
            FROM pg_stat_activity
            WHERE datname = CHR(111)||CHR(110)||CHR(101)||CHR(110)||CHR(108)||CHR(101)||CHR(110)||CHR(115)
            AND pid <> pg_backend_pid() ORDER BY pid;"
```

- Rows with `state = idle in transaction` on SELECTs = leaked
  transactions (the app bug above — should not recur after the fix).
- Rows with `wait_event_type = Lock`, `wait_event = relation` on
  `ALTER TABLE` = the DDL queue; rows of SELECTs also waiting on
  `relation` = the cascade. That is the wedge signature.

## Immediate relief (buys time, does not fix)

Terminate the stuck DDL backends (and any `idle in transaction`
holders predating the pool fix) by pid from the query above:

```bash
docker exec openlens-local-postgres-1 psql -U openlens -d openlens \
  -t -A -c "SELECT pg_terminate_backend(<pid>), pg_terminate_backend(<pid>)"
```

Terminating a waiting ALTER or a read-only SELECT transaction is
safe (DDL is `IF NOT EXISTS` idempotent; reads retry). Verify with
`curl http://localhost:8000/healthz` and a search POST. If the wedge
returns within minutes, the pool fix is not deployed — check the
release run.

## Do not

- Do not `docker compose down -v`: volumes hold the data and are not
  the problem; `-v` only destroys the corpus.
- Do not scale crawlers to "push through it": every new process
  re-runs boot DDL and makes the lock queue worse.
- Do not expose postgres/redis/kafka ports to "debug remotely":
  they are `127.0.0.1`-bound by design; use `docker exec` instead.

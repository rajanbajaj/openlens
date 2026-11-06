# Troubleshooting

Symptom-first runbook for operating OpenLens in production. Each entry
gives the likely cause, the exact check, and the fix. All `ssh`
commands target the prod host; read-only unless marked **mutating**.

Related pages: [Crawler and Frontier Ops](Crawler-and-Frontier-Ops),
[Search Path](Search-Path), [Backend Setup](Backend-Setup),
[Testing and CI](Testing-and-CI).

Per-incident notes live in `docs/troubleshoots/`, one dated file per
incident (`YYYY-MM-DD-<slug>.md`, chronological in plain `ls`). This
page is the symptom index; those files are the deep dives.

## Quick symptom index

| Symptom | Section |
|---|---|
| `POST /api/v1/search` → 502 `provider_timeout` on common terms | [Search 502](#search-502-providertimeout) |
| Admin provider `timeout_s` edit changes nothing | [Timeout edit ignored](#timeout-edit-ignored) |
| Result total reads `10,000+` | [Capped totals](#capped-totals) |
| `crawl-worker failed: Timeout reading from socket`, containers restarting | [Redis socket timeouts](#redis-socket-timeouts) |
| Queue depth stuck, crawl rate collapsed, workers idle | [Queue not draining](#queue-not-draining) |
| Admin shows far more workers than containers | [Phantom workers](#phantom-workers) |
| Postgres writer CPU pegged / container RAM full | [Postgres pressure](#postgres-pressure) |
| Redis RAM climbing, `HGETALL` storms in `SLOWLOG` | [Redis pressure](#redis-pressure) |
| Enqueue ignores Max queued URLs / skipped not logged | [Enqueue cap](#enqueue-cap) |
| Failed domains full of `403` / `max_bytes abort` | [Triage backlogs](#triage-backlogs) |
| Admin live queue counts freeze | [Stale live queue](#stale-live-queue) |
| Release deploy fails on crawler health gate | [Deploy health gate](#deploy-health-gate) |

## Search 502 `provider_timeout`

Common single-word queries (`love`, `news`) can match tens of thousands
of documents. The page query used to count every match with
`COUNT(*) OVER ()` (~4s of heap fetches at 44k matches) **plus** rank
and sort them (~4s), blowing the 5s provider budget. Rare terms return
in milliseconds because the same plan touches few rows — so slowness
that scales with term frequency is the signature, not contention.

Check (prod Postgres, read-only):

```bash
docker exec openlens-local-postgres-1 sh -c \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -t -c \
  "SET statement_timeout='"'"'20s'"'"'; \
   SELECT count(*) FROM documents \
   WHERE search_tsv @@ plainto_tsquery('"'"'english'"'"','"'"'love'"'"');"'
```

If the count alone takes seconds, the term is just frequent. Fixes
already in the codebase:

- Totals are exact up to `MAX_EXACT_TOTAL` (10,000) and saturate above
  it. The page counts through a `LIMIT 10001` probe that stops the
  bitmap scan early (~0.5s) while the ranked CTE still scores all
  matches, so top-N order is unchanged.
- `CustomIndexProvider` honors `timeout_s` (it previously ignored the
  setting and always enforced 5s — see below).

If 502s persist after those, raise `timeout_s` under Admin → Providers
(requires a backend restart) rather than waiting on a cold cache.

Incident note: `docs/troubleshoots/2026-11-06-search-love-502-capped-total.md`.

## Timeout edit ignored

**Cause (fixed):** `CustomIndexProvider` extended the provider ABC
directly instead of `BaseSearchProvider`, so `configure()` never read
`timeout_s` and the manager's live read fell back to the 5s default
forever. The 502 body names the enforced budget
(`timed out after 5.0s`) — trust it over the admin screen.

**Restart rule:** Admin → Config writes persist to the config store and
overlay in-memory reads, but live provider objects are built at boot.
Any provider change (`timeout_s`, enable/disable) needs a backend
restart:

```bash
docker restart openlens-local-backend-1
```

then re-run the query and confirm the 502 body (if any) names the new
budget.

Incident note: `docs/troubleshoots/2026-11-06-provider-timeout-ignored.md`.

## Capped totals

Totals above 10,000 matches report `10000`; the UI renders `10,000+`.
Only the displayed total saturates — ranking and pagination are
unchanged (the cap covers ~1000 pages). Below the cap totals are exact.

## Redis socket timeouts

`failed to read frontier URLs: Timeout reading from socket` (or
`frontier redis backend unreachable`) in crash-looping crawlers means a
client-side read timed out, not a dead Redis. The classic trigger was a
full frontier scan building one pipeline with an `HGETALL` per known
URL (250k+ commands, multi-second reply). Scans now execute in
`_META_PIPELINE_BATCH` (500) command chunks, so they degrade to slow
instead of dying.

Check:

```bash
docker logs openlens-local-crawler-4 --since 30m 2>&1 | grep -c "timeout reading from socket"
docker exec openlens-local-redis-frontier-1 redis-cli -n 0 info commandstats | grep -E "hgetall|sscan"
docker exec openlens-local-redis-frontier-1 redis-cli slowlog get 10
```

Many `HGETALL fw:meta:*` entries with 30–50ms durations point at full
scans (admin polling, purge jobs). If timeouts persist with chunking in
place, downscale crawlers/analytics to 2 replicas each to cut
concurrent scan load, then look for the caller.

## Queue not draining

Workers idle with a non-empty queue is usually the drain path parked,
not an empty frontier. Distinguish first:

```bash
docker exec openlens-local-redis-frontier-1 redis-cli -n 0 zcard fw:ready
docker exec openlens-local-kafka-1 kafka-consumer-groups --bootstrap-server localhost:9092 --describe --all-groups
docker logs openlens-local-crawler-4 --since 30m 2>&1 | tail -5
```

- `fw:ready` growing + no crawl log progress → drain loop parked
  (check drain budget settings, then thread dump / restart one crawler).
- Kafka lag growing on crawl topics → consumers stalled (check
  backoff topic depth separately — backoff crawlers are their own
  consumer group).
- Hourly crawl rate collapses while the queue stays healthy →
  discovery blackhole (seeds exhausted, robots-gated, or skip-listed);
  check failed/skipped domain stats before reseeding.

Never purge failed domains without skip-listing them first, or the next
discovery pass re-enqueues the same failures.

## Phantom workers

Admin shows 25–47 `alive` workers with 2 crawler containers because
worker IDs embed a per-boot suffix and the registry's stale window is
~240s: every restart mints new IDs while old rows linger. The count
converges on its own. Trust container state over the table:

```bash
docker ps --format "{{.Names}} {{.Status}}" | grep crawler
```

If the count never converges, the heartbeat path is broken (check
`POST /api/v1/frontier/workers/heartbeat` in backend logs).

## Postgres pressure

Writer CPU at 100% with crawlers moved to Redis usually means FTS
count/rank scans (see [Search 502](#search-502-providertimeout)),
autovacuum churn, or connection sprawl — not the frontier. Check:

```bash
docker exec openlens-local-postgres-1 sh -c \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c \
  "SELECT application_name, state, count(*) FROM pg_stat_activity GROUP BY 1,2;"'
```

Container RAM near its limit is usually page cache, not a leak.
Compare `anon` (real RSS) against `file` (reclaimable cache):

```bash
docker exec openlens-local-postgres-1 cat /sys/fs/cgroup/memory.stat | grep -E "^(anon|file) "
```

High `file` with low `anon` is a warm cache — good, unless the cgroup
ceiling is close. Sustained writer CPU plus cache pressure is the
signal to vertically scale Postgres (the scale workflow persists
memory limits) rather than restart it (a restart just colds the cache
and makes search slow again).

Incident note: `docs/troubleshoots/2026-11-06-postgres-ram-page-cache.md`.

## Redis pressure

```bash
docker exec openlens-local-redis-frontier-1 redis-cli --bigkeys
docker exec openlens-local-redis-frontier-1 redis-cli -n 0 info commandstats | grep -E "hgetall|sscan"
```

- One growing `zset fw:ready` (~10k members is normal) plus hundreds of
  thousands of `fw:meta:*` hashes is the steady state.
- Sustained tens of thousands of `HGETALL`/s with the crawlers idle
  means something full-scans on a loop — sample `commandstats` 45s
  apart; each `_iter_metas` pass costs roughly one `HGETALL` per seen
  URL. Find the polling caller (admin screens, purge jobs) before
  resizing.
- Memory over the limit evicts meta hashes the mirror still
  references; scale Redis RAM through the scale workflow (persisted),
  not `CONFIG SET`.

Incident note: `docs/troubleshoots/2026-11-06-redis-hgetall-storm-chunked-scan.md`.

## Enqueue cap

Seeds enforce Max queued URLs **before** publish and report
`skipped_over_cap` (with a `skipped` analytics event); discovery
overflow sheds to skipped with the same reason. If the queue keeps
growing past the cap, the writes bypassed the seed path — check the
caller, not the cap. Skipped URLs are parked, not lost: the roadmap
overflow holding pen trickles them back in as the queue drains.

## Triage backlogs

Failed-domain lists dominated by `http 403` (bot-gated: Amazon,
scientific publishers, social) and `max_bytes abort` (oversized pages)
are crawler-repellent sources, not bugs. Tune from the failing stats:

- Skip-list or deprioritize persistent `403`/`405` domains instead of
  re-crawling them; honor `robots.txt` crawl-delay where given.
- `max_bytes abort` on media/retail hosts means the fetch cap is doing
  its job — raise it only for hosts whose content you actually want.

## Stale live queue

Admin queue counts update but URLs/statuses freeze (or vice versa)
means the snapshot endpoint and the live socket disagree. Check the
socket first: `WebSocket /api/v1/frontier/live` should show
`[accepted]` + `connection open` in backend logs. If the socket is
live and the UI is stale, the fault is in the panel subscription, not
the backend. Queue-depth reads are `O(1)` (`ZCARD`/`COUNT`), so a slow
admin screen is a scan caller, not the depth query.

## Deploy health gate

A release that fails with a crawler `restarting` health gate then
succeeds on rerun is the gate racing container startup, not a bad
image: rerun the failed workflow before rolling back. Only `v*` tags
deploy; release commits bump `pyproject.toml` and never move tags.
Backend + frontend + secrets gates must all be green before declaring
done — tests alone are not done when a built artifact exists.

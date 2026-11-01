# Crawler and Frontier Ops (developers)

## Queue model

Postgres `frontier_queue` is the source of truth; the admin page
(`Queue`, port `3001`) shows queued/active rows with depth and
priority. Seeds enter via resume-with-seeds (optional per-call
`priority`; higher dequeues first, `ORDER BY priority DESC, depth
ASC`); discovered links inherit decayed priority (5 → 4 → 3 → 2 …)
and same-domain scope. Claimed rows carry an owner lease
(`frontier.lease_ttl_s`, heartbeat every TTL/3); only expired
leases are requeued.

## Discovery and freshness

- Sitemap/RSS discovery feeds per domain config, run on an
  interval with warn-level per-feed logs and worker-carried
  summaries (`last_discovery` on status).
- Requeue stale rows for age refresh; feeds catch new URLs.
  Daily full recrawls are unnecessary.

## Distributed mode

With `kafka.enabled`, seeds and discoveries flow through topic
`crawl.urls`; every `crawler` replica joins group `crawlers`, so
Kafka assigns each URL once (`--scale crawler=N`). Offsets commit
only after crawl + index; the Postgres seen-set dedupes twins.
`POST /api/v1/frontier/clear` pauses, deletes queued rows, and
purges the topic so published messages cannot refill the queue.

## Ops checklist

- Size Postgres `max_connections` for ~10 per process × workers.
- Watch consumer lag (status `kafka_lag_total`) before scaling.
- `scripts/cleanup.sh` wipes indexed + queue data for a fresh crawl.
- Incidents and fixes: `docs/troubleshoots/`.

## Category backfill (`scripts/backfill_site_categories.py`)

Re-tags already-indexed rows under the current classifier without
refetching: it pages stored URL+metadata through
`classify_site_category` and rewrites only stale rows (wrong or
missing `site_category`, outdated `CATEGORY_RULES_VERSION`).

**When to run it** — after any classifier change ships: a new
category in `configs/category_domains.yaml`, a retuned
`classify_site_category` branch, or a `CATEGORY_RULES_VERSION`
bump. New crawls classify at extraction, so only pre-existing rows
need it. Safe to run live (500-row pages, row-level rewrites) and
idempotent — re-running rewrites zero rows.

**When NOT to run it** — it cannot add `description`, `date_modified`,
`geo_hint`, or chrome-free content; those need a recrawl (raw HTML
is never stored). It also never deletes or requeues URLs.

**Steps** (release host; the script resolves `src/` itself, but the
system python has no project deps — run it where the backend
environment exists):

```bash
cd ~/servers/openlens-release
BE=$(docker ps --format '{{.Names}}' | grep -i backend | head -1)
docker cp scripts/backfill_site_categories.py "$BE:/tmp/backfill_site_categories.py"
docker exec "$BE" python3 /tmp/backfill_site_categories.py
```

Expected output: `backfill done: <rewritten> rewritten of <total> documents`.

**Verify** with a category census (same host):

```bash
PGCONT=$(docker ps --format '{{.Names}}' | grep -i postgres | head -1)
docker exec -i "$PGCONT" psql -U openlens -d openlens -t -c \
  "SELECT metadata->>'site_category', COUNT(*) FROM public.documents GROUP BY 1 ORDER BY 2 DESC;"
```

Host-venv alternative (only when the checkout has its own `.venv`
with the project installed): `source .venv/bin/activate`, then
`python3 scripts/backfill_site_categories.py` from the repo root.

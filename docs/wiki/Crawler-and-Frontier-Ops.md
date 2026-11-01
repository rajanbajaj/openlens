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

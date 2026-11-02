# Crawl Path: Core Concepts

How the web becomes the index. Companion to the
[Architecture HLD](Architecture#high-level-design) and
[Crawler and Frontier Ops](Crawler-and-Frontier-Ops); code lives in
`src/openlens/indexing/` (crawler, worker, kafka_queue),
`src/openlens/storage/frontier.py` (queue states), and
`src/openlens/analytics/` (crawl telemetry).

## The queue: states and leases

The frontier is a Postgres table (SQLite offline), one row per
normalized URL: `queued → active → done`, or `queued → failed`,
plus `skipped`. Claim is a lease, not a lock: the worker takes
ownership with a 120 s TTL (`frontier.lease_ttl_s`, heartbeat every
TTL/3) and only expired leases are requeued — a restarting worker
never steals live rows. Attempts accumulate per row
(`max_attempts_per_url`, default 3); terminal rows stay failed
until an operator requeues them.

## Two coordinated paths, one store

- **Kafka mode** (`crawl-worker --queue kafka`): seeds and
  discovered links publish to `crawl.urls` (12 partitions, group
  `crawlers`); each live worker owns its partitions. Delivery is
  at-least-once — offsets commit only after index — and duplicates
  collapse on the normalized-URL primary key.
- **Store mode** (offline/tests): workers claim directly from the
  table. Multi-process crawling requires Postgres; SQLite
  coordination is process-local.

Both paths converge on the same drain: `run_index` + `PoliteCrawler`
with checkpoints (`phase`, `pages_attempted`, `pages_indexed`),
priority dequeue (`priority DESC, depth ASC`), and heartbeats
(`claimed`, stats, discovery) to the backend registry behind
`GET /frontier/status` and the live socket.

## Join order and the poll budget

Workers drain the store backlog **before the first poll**, so group
membership follows the drain, not the process: zero members during
a deep initial drain is expected. Every drain is time-boxed under
1/4 of `max_poll_interval_ms` (default 900 s), so a slow backlog
reads as steady membership instead of heartbeat-expiration kicks
(the generation-60 storm that motivated the bound). Watch
heartbeats and `queue_depth`, not the member count.

## Fetch pipeline: gates before network

Each claim passes the push-time gates in order: `deny_domains`
→ fetch-validity (`_is_fetchable_url`: scheme, host, nested
non-page schemes) → domain policies → `allow_domains` → depth cap
→ robots (+ crawl-delay) → per-domain page budget
(`domain_policies.max_pages`). Denied rows fail terminally with a
naming `last_error`; invalid URLs never reach the network.

The fetch itself is bounded (`timeout_s`, default 5 s) with
conditional revalidation (ETag/Last-Modified), per-domain
politeness (`max(crawl_delay_s, rate_limit_s, robots delay)`),
and a quality gate (`min_visible_chars`, soft-404 detection).
Quality-skipped pages never expand outlinks — trap pages cannot
spider further — and a host with thousands of near-empty pages is
capped by `max_pages` instead of the whole crawl.

## Retry budget

At most 3 fetch attempts per URL, surfacing across `crawl()`
calls (one fetch per URL per call). Retried: 429/5xx, timeouts,
transport errors. Terminal on first hit: any 4xx, `invalid url`,
empty fetch, content-type/quality skips. Every attempt emits one
`crawl.page` analytics event, so dashboard counts track attempts,
not distinct URLs — per-URL truth lives in the failed list.

## Operator handles

- `GET /frontier/failed/domains`: failed hosts with counts.
- Per host: **requeue** (attempts reset, error cleared),
  **flush** (delete failed history only), **skip** (queued rows),
  **block** (merge into `deny_domains` via the indexing config doc).
- `POST /frontier/clear` pauses, deletes queued rows, and purges
  the topic. Discovery feeds (sitemaps/RSS) and refresh passes
  keep the corpus fresh between checkpoints.

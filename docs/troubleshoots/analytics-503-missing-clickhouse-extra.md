# Admin analytics 503 "analytics store unavailable" (missing ClickHouse extra)

Date: 2026-11-02 · Fixed in v0.4.39

## Symptoms

- `GET /api/v1/admin/analytics/counts` returns
  `503 {"error": {"code": "provider_unavailable", "message": "analytics store unavailable"}}`
  on the release server, while the same endpoint returns 200 locally.
- The Analytics tab in the admin frontend shows "No analytics data".
- The backend log line reads:
  `analytics counts: store unavailable: ClickHouse driver
  'clickhouse-connect' is not installed; install the analytics extra...`
- The `analytics-worker` container crash-loops or exits with the same
  import error.

## Root cause

`deploy/local/Dockerfile.backend` installed only the `kafka` extra, but
compose runs that image as both the API and the `analytics-worker` with
`OPENLENS_ANALYTICS_BACKEND=clickhouse`. The ClickHouse store
lazy-imports `clickhouse-connect` (`_load_clickhouse`), so without the
`analytics` extra both the endpoint (503, fail-open by design) and the
worker fail. Nothing called the endpoint before the Analytics tab
landed (v0.4.38), so the missing driver was invisible until then.

Reproduced offline by pointing the app config at `clickhouse` without
the driver installed: the exact 503 body above; sqlite backend returns
200 on the same build.

## Fix (v0.4.39)

- `deploy/local/Dockerfile.backend`: `pip install .[kafka,analytics]`
  (comment records why both extras are required).
- Regression test `test_backend_image_installs_analytics_extra` in
  `tests/unit/test_analytics.py`: the Dockerfile must mention the
  analytics extra.

## How to confirm on prod

1. Deploy the release (the image rebuild is what installs the driver).
2. `GET /api/v1/admin/analytics/counts` returns 200 (counts start at
   zero and fill in as the worker drains `analytics.events`).
3. `docker ps` shows `analytics-worker` stable (not restarting).

## Do not chase these (ruled out for this signature)

- Postgres/Redis/Kafka reachability (the analytics store is a separate
  ClickHouse database on purpose; its failure 503s only this endpoint).
- The `sqlite.db_path` default (fixed separately in v0.4.38 for
  off-compose dev; compose uses ClickHouse).
- Events published while the worker was down: publish is fire-and-forget,
  so only new searches/feedback after the fix appear in counts.

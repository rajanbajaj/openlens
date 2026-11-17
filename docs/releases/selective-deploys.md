# Selective deploys (release CI)

Full fleet: push a version tag (`v*`). The release workflow deploys
the whole stack on the prod server.

One part at a time: Actions → release → Run workflow.

| What changed | Services input |
|---|---|
| Search UI | `frontend` |
| Admin UI | `admin-frontend` |
| Both UIs (usual demo prep) | `frontend admin-frontend` |
| API / pipeline / frontier | `backend` |
| Crawlers (politeness, fetch, backoff) | `crawler crawler-backoff` |
| Analytics sink | `analytics-worker` |
| Analytics store | `clickhouse` |
| Everything | `all` (or push a tag) |

Rules the workflow enforces:

- Only named services are rebuilt and recreated. Nothing else
  restarts — the fleet keeps crawling through a frontend deploy.
- Unknown service names fail the run before anything is touched.
- One deploy at a time (concurrency group `release-production`).
  Never run a manual `compose up` on prod while a release is in
  flight — two writers racing is what orphans half-renamed
  containers (`<id>_openlens-local-*`) and name conflicts.
- Fleet size comes from `CRAWLER_SCALE` /
  `ANALYTICS_WORKER_SCALE` / `CRAWLER_BACKOFF_SCALE` in
  `~/servers/release.env`. No CLI `--scale` flags: they do not
  survive the next plain `up -d` and silently shrink the fleet.
- Health gates match what was deployed: search probe for backend,
  HTTP 200s for the UIs, healthy-container polls for workers and
  data services, `SELECT 1` for ClickHouse. Backend healthz is
  checked on every run — a broken control plane never hides
  behind a green service restart.

## Downgrade to lightweight (release-lightweight CI)

Actions → release-lightweight → Run workflow (inputs: `ref`,
`admin_profile`). Detects the current fleet: if heavy services are
running it drains the crawlers, tears the full stack down with plain
`down` (never `-v` — `pgdata` is the index), and starts
`docker-compose-lightweight.yml` under the SAME project name
(`-p openlens-local`), so the data volumes are reused, not
recreated. If already lightweight it just refreshes. Search is briefly
unavailable during the swap — run at a quiet time. Rollback is a
plain `release` run; heavy-only volumes stay on disk in between.

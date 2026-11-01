# Backend Setup (developers)

Requires Python ≥ 3.10 and [`uv`](https://docs.astral.sh/uv/).

```bash
uv venv
uv sync
uv run uvicorn openlens.main:app --reload   # :8000
```

Needs a reachable Postgres (see `deploy/local/docker-compose.yml`);
without one the engine fails fast with `ProviderError(UNAVAILABLE)`.

## Config

YAML under `configs/` (`default.yaml`, `providers.yaml`,
`ranking.yaml`, `models.yaml`, `category_domains.yaml`), loaded by
`src/openlens/config.py`. Secrets are `${ENV_VAR}` / `${ENV_VAR:-default}`
references resolved at load; a bare `${VAR}` with no value fails
startup via `ConfigError`. Full variable table: README
(Environment variables). Never commit real keys — copy
`.env.example` to `.env` (gitignored) and export it.

Validate without booting:

```bash
uv run python -m openlens.cli config validate --config /tmp/x.yaml
uv run python -m openlens.cli providers health
```

## Read replica (search scaling)

`postgres-replica` is an async streaming standby: search/suggest reads
(`search`, `list_titles`, `list_domains`, `count`, `get`, …) go there
while all writes stay on the primary, so crawl write pressure cannot
starve search. Enabled only when `OPENLENS_PG_REPLICA_HOST` is set
(compose default: `postgres-replica`); empty means the primary serves
everything.

Fail-open contract (no operator action needed when the replica is
down, lagging, or mid-basebackup): the pool builds lazily, reads fall
back to the primary on any pool failure or saturation, and three
consecutive mid-use failures drop the pool until the next retry
window. A sick replica degrades to primary-only search, never to
failed searches.

Caveats:

- Async means lag (usually ms–s): freshly crawled docs appear in
  search with a delay. Check it with
  `SELECT now() - pg_last_xact_replay_timestamp();` on the replica.
- Crawlers stay primary-only on purpose, so dedup/hash checks never
  read stale rows.
- The backend then holds one more pool (`pool_size` conns on the
  replica): size `POSTGRES_MAX_CONNECTIONS` as
  `(replicas + 1) × 2 × pool_size + replica_pool_size + headroom`.
- First replica boot basebackups the whole DB (minutes on a big
  index); the 180s `start_period` covers it, and the backend boots
  fine without the replica in the meantime.
- Restart warmup is automatic: on boot the backend spawns a
  best-effort daemon thread that runs `warm_search_index()` on the
  primary and (when configured) the replica — `pg_prewarm` on
  `public.documents` plus the top-N search term probes — so post-
  restart cold caches recover without manual steps. It never raises
  and never blocks startup; the manual commands above remain as the
  fallback if you need to re-warm by hand.
  `shared_buffers=256MB` (both servers) keeps the hot set resident
  afterwards. Manual fallback (unchanged):
  `CREATE EXTENSION IF NOT EXISTS pg_prewarm;` on the primary, then
  `SELECT pg_prewarm('public.documents');` and
  `SELECT pg_prewarm('idx_documents_search_tsv');` on the replica.
- Search is single-scan: the total rides on the same match scan as
  the ranked page (`COUNT(*) OVER ()`), so one round trip replaces
  the old count-then-page pair and common terms pay for one GIN
  bitmap build instead of two. Totals stay exact.
- The privacy filter flattens each hit's scanned text once and
  reuses it across all PII classes and both passes (exclude +
  redact), instead of rebuilding the metadata walk up to 8x per hit.
- The pagination fill loop projects its refetch depth from the
  measured survival rate instead of blind doubling, so heavily
  filtered modes cover the page in ~2 passes instead of ~5 — each
  avoided pass also skips a full plugin-pipeline rerun and an
  expansion-variant fan-out.
- Autovacuum is self-tuned at schema init for crawler upsert churn:
  `autovacuum_vacuum_scale_factor=0.05` /
  `autovacuum_analyze_scale_factor=0.025`
  (`autovacuum_vacuum_threshold=1000` /
  `autovacuum_analyze_threshold=500`), so the documents table is
  vacuumed/analyzed after ~5% churn instead of the ~20% default.
  Idempotent `ALTER TABLE` — safe on every boot.

## Key routes

- `POST /api/v1/search`, `GET /api/v1/suggest`,
  `POST /api/v1/feedback`
- `POST /api/v1/agent/chat` (chat turns, trace, perspectives)
- `GET /api/v1/frontier/*` + pause/resume/clear/settings
  (mutations need `X-Admin-Token`)
- `GET/PUT /api/v1/admin/config/*` (same token gate)
- `GET /metrics` (opt-in), `/healthz`

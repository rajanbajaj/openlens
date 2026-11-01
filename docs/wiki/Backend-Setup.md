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
- After any postgres restart the caches are cold and common-term
  queries (10k+ matches) can take seconds: warm with
  `CREATE EXTENSION IF NOT EXISTS pg_prewarm;` then
  `SELECT pg_prewarm('public.documents');` and
  `SELECT pg_prewarm('idx_documents_search_tsv');` on the replica.
  `shared_buffers=256MB` (both servers) keeps the hot set resident
  afterwards.

## Key routes

- `POST /api/v1/search`, `GET /api/v1/suggest`,
  `POST /api/v1/feedback`
- `POST /api/v1/agent/chat` (chat turns, trace, perspectives)
- `GET /api/v1/frontier/*` + pause/resume/clear/settings
  (mutations need `X-Admin-Token`)
- `GET/PUT /api/v1/admin/config/*` (same token gate)
- `GET /metrics` (opt-in), `/healthz`

# OpenLens

Plug-and-play search engine skeleton: search backends (custom index,
OpenSearch, external APIs) and AI backends are swappable via config, with
filtering, ranking, and enrichment owned by independent plugins behind stable
interfaces (`src/openlens/core/interfaces.py` + `core/context.py`).

## Install

Requires Python ≥ 3.10 and [`uv`](https://docs.astral.sh/uv/).

```bash
uv venv
uv sync
```

## Run the API

```bash
uv run uvicorn openlens.main:app --reload
```

Then search:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/search \
  -H 'Content-Type: application/json' \
  -d '{"query": "red shoes", "mode": "shopping", "limit": 10}'
```

## Frontend

Requires Node.js 20 and npm. From `frontend/`:

```bash
npm ci
npm run dev
```

The dev server talks to the backend at `VITE_OPENLENS_API_URL`
(default `http://localhost:8000`, so boot the API first); production
builds use same-origin `/api/` instead:

```bash
VITE_OPENLENS_API_URL=http://127.0.0.1:8000 npm run dev
```

Other commands (all from `frontend/`): `npm test` (vitest),
`npm run lint`, `npm run format:check`, `npm run build` (`tsc` + vite).

## Run the tests

```bash
uv run pytest tests/unit tests/integration
```

Offline only: no network, no API keys, no OpenSearch cluster. Tests use fakes.

## Config overview

YAML files under `configs/` (loaded with the shared loader in
`src/openlens/config.py`):

| File | Purpose |
|---|---|
| `configs/default.yaml` | Engine wiring: which providers and plugins are enabled |
| `configs/providers.yaml` | Search-backend selection and per-provider settings |
| `configs/ranking.yaml` | Named per-mode ranking profiles (`default`, `child-safe`, `news`, `shopping`, `travel`) |
| `configs/models.yaml` | AI backend selection (all disabled by default; no live calls in the skeleton) |

Secrets use `${ENV_VAR}` (or `${ENV_VAR:-default}`) references resolved at
load time. An `${ENV_VAR}` with no value and no default fails startup
validation loudly via `ConfigError` — secrets are never logged or returned.

## Environment variables

Every variable the repo reads, with its default and where it is consumed.
Only enabled backends are instantiated, so most of these can stay unset —
set the ones for the backends you turn on.

| Variable | Default | Used for |
|---|---|---|
| `OPENLENS_CONFIG` | (source tree, then CWD) | Override config file location (`src/openlens/main.py`) |
| `MODEL_API_KEY` | (none — required when enabled) | Key for the `openai_compat` model backend (`configs/models.yaml`) |
| `MODEL_BASE_URL` | `https://api.meta.ai/v1` | Endpoint for the `openai_compat` model backend |
| `MODEL_NAME` | `muse-spark-1.3` | Model name sent to the `openai_compat` backend |
| `ANTHROPIC_API_KEY` | (none — required when enabled) | Key for the `claude` model backend |
| `MUSE_API_KEY` | (none — required when enabled) | Key for the `muse` model backend |
| `LOCAL_MODEL_ENDPOINT` | `http://localhost:11434` | Endpoint for the `local` model backend |
| `BING_API_KEY` | (none — required when enabled) | Key for the `bing` search provider (`configs/providers.yaml`) |
| `MYENGINE_URL` / `MYENGINE_API_KEY` | (commented example only) | Endpoint/key for a custom `http`-type provider |
| `OPENSEARCH_USER` / `OPENSEARCH_PASSWORD` | (commented example only) | Basic-auth credentials for the `opensearch` provider |
| `OPENLENS_MYSQL_HOST` / `OPENLENS_MYSQL_PORT` / `OPENLENS_MYSQL_DATABASE` / `OPENLENS_MYSQL_USER` / `OPENLENS_MYSQL_PASSWORD` | `localhost` / `3306` / `openlens` / `root` / (empty) | MySQL storage backend (`storage.mysql` in `configs/default.yaml`) |
| `OPENLENS_PG_HOST` / `OPENLENS_PG_PORT` / `OPENLENS_PG_DBNAME` / `OPENLENS_PG_USER` / `OPENLENS_PG_PASSWORD` | `localhost` / `5432` / `openlens` / `postgres` / (empty) | Postgres storage backend (`storage.postgres` in `configs/default.yaml`) |
| `OPENLENS_REDIS_URL` | `redis://localhost:6379/0` | Redis cache backend (`cache.redis` in `configs/default.yaml`) |
| `VITE_OPENLENS_API_URL` | dev `http://localhost:8000`, prod same-origin | Backend URL baked into the frontend (`frontend/src/api/client.ts`) |
| `OPENLENS_SMOKE_URL` | (unset — test skipped) | Target for the opt-in live-backend frontend smoke test |
| `BACKEND_PORT` / `FRONTEND_PORT` / `OPENSEARCH_PORT` | `8000` / `3000` / `9200` | Published ports in `deploy/local/docker-compose.yml` |
| `OPENSEARCH_INITIAL_ADMIN_PASSWORD` | (see `deploy/local/.env.example`) | First-boot admin password required by the OpenSearch image |

Rules: never commit a real key. Copy the root `.env.example` to `.env`
and export it before running (the loader reads process env only —
there is no dotenv dependency):

```bash
cp .env.example .env   # then fill in the backends you enabled
set -a; source .env; set +a
uv run uvicorn openlens.main:app --reload
```

(`deploy/local/.env.example` is the separate Docker Compose file.)
A bare `${VAR}` with no value and no `:-default` fails startup via
`ConfigError`; a `${VAR:-default}` reference falls back silently.

## Storage backends

`storage.backend` in `configs/default.yaml` is the single switch
(`sqlite` | `mysql` | `postgres`, default `sqlite`); per-backend
connection fields live under `storage.sqlite` / `storage.mysql` /
`storage.postgres`. An unknown backend or a non-mapping section fails
startup validation loudly via `ConfigError`.

| Backend | Fields |
|---|---|
| `sqlite` | `db_path` (default `data/openlens.db`; `:memory:` for ephemeral). The legacy provider-entry `db_path` still works as the fallback when `storage.sqlite.db_path` is unset; a config with no `storage:` section at all still boots SQLite. |
| `mysql` | `host` / `port` (default `3306`) / `database` (`db` alias accepted) / `user` / `password` / `table` / `charset` / `pool_size` / `connect_timeout` / `checkout_timeout`. Conventional env vars `OPENLENS_MYSQL_HOST` / `OPENLENS_MYSQL_PORT` / `OPENLENS_MYSQL_DATABASE` / `OPENLENS_MYSQL_USER` / `OPENLENS_MYSQL_PASSWORD`. |
| `postgres` | `host` / `port` (default `5432`) / `dbname` (`database` / `db` aliases accepted) / `user` / `password` / `schema` / `table` / `pool_size` / `connect_timeout` / `checkout_timeout`. Conventional env vars `OPENLENS_PG_HOST` / `OPENLENS_PG_PORT` / `OPENLENS_PG_DBNAME` / `OPENLENS_PG_USER` / `OPENLENS_PG_PASSWORD`. |

```yaml
storage:
  backend: sqlite
  sqlite:
    db_path: "data/openlens.db"
```

Server backends need their driver-only extras
(`pip install openlens[mysql]` for `pymysql`, `pip install
openlens[postgres]` for `psycopg` v3); SQLite has zero extra
dependencies. Drivers lazy-import, so unconfigured backends add no
import-time dependency — instantiating without the driver raises
`ProviderError(UNAVAILABLE)`, never a raw `ImportError`, and connection
failures at construction surface the same way.

Known lossy FTS translation: `search` takes the already-parsed FTS5-shaped
query string, and each server backend translates it to its native form
without cross-engine normalization. MySQL passes it verbatim into
`MATCH(title, content) AGAINST (... IN NATURAL LANGUAGE MODE)`, so FTS5
phrase quotes, column filters, boolean operators, and `*` prefixes are
treated as ordinary words/punctuation. Postgres strips phrase quotes,
`*` prefixes, `^` boosts, `:` column filters, parentheses, and
`AND`/`OR`/`NOT`/`NEAR` keywords, then passes the remainder to
`plainto_tsquery('english', ...)` — which ANDs the surviving terms, so an
FTS5 `OR` silently becomes an AND. Scores are engine-local
(MySQL relevance, Postgres `ts_rank`, SQLite `-bm25` point the same
direction but are never comparable across backends); only document id
sets are expected to agree, and tokenization differences (MySQL
stopwords / minimum token lengths, Postgres `english` stemming) can make
id sets differ on some queries. Blank queries return `([], 0)` without
touching the server.

Concurrency: SQLite uses one shared connection plus a lock with
`PRAGMA journal_mode=WAL` (readers don't block the single writer) and a
busy timeout. MySQL and Postgres instead open a bounded connection pool
at construction (`pool_size` default 5, blocking checkout with
`checkout_timeout` default 5.0s, `connect_timeout` default 5s);
`close()` drains every pooled connection and is idempotent. All backends
share the same write contract: bulk writes commit per `chunk_size`
chunk (default 500, no cross-chunk atomicity), upsert on repeated ids,
`delete` of a missing id returns `False`, and `metadata` round-trips as
a parsed dict.

## Pipeline

Every request flows through 10 stages (`PipelineStage` in
`core/interfaces.py`): query parsing → query rewrite → retrieval →
normalization → deduplication → filtering → scoring → ranking → enrichment →
explanation. Plugins declare their stage, type, dependencies, and whether
they are mandatory (fail closed) or optional (failures isolated and recorded
in `SearchContext` diagnostics).

## Operations

Run, observe, and troubleshoot OpenLens without reading response JSON:
one JSON log line per request plus a Prometheus metrics endpoint expose
provider/plugin behavior.

### Run

```bash
uv run uvicorn openlens.main:app --reload
uv run python -m openlens.cli search "cats" --json
uv run python -m openlens.cli config validate
uv run python -m openlens.cli providers health
```

### Blue-green search index

One OpenSearch cluster serves through the `openlens` alias while each
crawl fills a fresh timestamped stage index (`openlens_YYYYMMDDHHMMSS`).
Promoting a verified stage index is one atomic alias flip — no app
restart, no data copy, instant rollback. The SQL store stays canonical;
the CLI loads it into stage and manages the lifecycle:

```bash
uv run python -m openlens.cli index add seed-urls...   # crawl -> SQL store
uv run python -m openlens.cli opensearch stage          # fresh stage index
uv run python -m openlens.cli opensearch load           # SQL store -> stage
uv run python -m openlens.cli opensearch verify --index openlens_<stamp>
uv run python -m openlens.cli opensearch promote --index openlens_<stamp>
uv run python -m openlens.cli opensearch rollback --index openlens_<prior>
uv run python -m openlens.cli opensearch prune          # drop old indices
```

(`load` targets the newest stage index, creating one when none exists;
serving reads the alias, so running servers need no restart after a
flip — only the short result-cache TTL can briefly serve pre-flip hits.)

Moving an index between clusters (local <-> remote over an `ssh -L`
tunnel) copies mappings first, then documents with ids preserved, so
reruns overwrite instead of duplicating:

```bash
ssh -L 19201:localhost:9200 you@remote-host   # one terminal
OPENSEARCH_SOURCE_USER=... OPENSEARCH_SOURCE_PASSWORD=... \
OPENSEARCH_DEST_USER=... OPENSEARCH_DEST_PASSWORD=... \
uv run python -m openlens.cli opensearch migrate \
  --source http://localhost:9201 --dest http://localhost:9200 \
  --index openlens_20261028
```

Credentials travel via environment variables only — they are never
logged, returned, or echoed on failure.

### Observe

Structured logs: every API search emits one JSON record on stderr at
INFO (request id, mode, limit, provider latencies, plugin executions,
error count — never the raw query; the query is logged at DEBUG only).
The log level comes from `configs/default.yaml`:

> Gap: `python -m openlens.cli search` does not emit the per-request
> record yet — the CLI builds on `build_engine` directly and has no
> logging hookup (follow-up for the CLI owner, not this wiring).

```yaml
logging:
  level: INFO   # DEBUG | INFO | WARNING | ERROR
```

Metrics: `GET /metrics` serves Prometheus text (per-provider latency
sums/counts with fixed `le` histogram buckets, per-provider error counts,
per-plugin timings/execution counts, request totals). It is disabled by
default — the route 404s unless enabled:

```yaml
metrics:
  enabled: true   # default false; restart (or rebuild the app) to apply
```

Scrape it with `curl localhost:8000/metrics` (with `metrics.enabled: true`)
and expect a 404 when disabled (default).

Every response carries the request id twice: `diagnostics.request_id`
in the body and the `X-Request-Id` response header (a client-supplied
`X-Request-Id` request header is echoed; otherwise one is generated).
Browsers can send and read it — it is in the CORS `allow_headers` list.

One annotated example log line (line breaks added):

```json
{"timestamp": "2026-10-28T12:00:00+00:00", "level": "INFO",
 "logger": "openlens.search", "message": "search completed",
 "event": "search.completed", "request_id": "9f2c…hex",
 "mode": "default", "limit": 10,
 "provider_latencies_ms": {"custom_index": 3.2},
 "provider_result_counts": {"custom_index": 4},
 "result_count": 4, "fallback_used": false,
 "plugin_executions": [{"plugin": "safety-filter",
   "stage": "filtering", "status": "ok",
   "latency_ms": 0.4, "error": null}],
 "errors": []}
```

A `/metrics` excerpt after one search:

```text
openlens_requests_total{status="ok"} 1
openlens_provider_latency_ms_count{provider="custom_index"} 1
openlens_provider_latency_ms_sum{provider="custom_index"} 3.2
openlens_plugin_latency_ms_count{plugin="safety-filter",stage="filtering"} 1
openlens_plugin_executions_total{plugin="safety-filter",stage="filtering",status="ok"} 1
```

Label names are fixed (`provider`, `plugin`, `stage`, `status`, `le`) —
query text, doc ids, and error strings never become labels.

### Troubleshoot

- `config validate` exits nonzero without booting a server when the
  config is malformed (use a temp file — never commit a bad config):

  ```bash
  uv run python -m openlens.cli config validate --config /tmp/bad.yaml
  ```

- A 503 from `POST /api/v1/search` means the engine failed to boot;
  check startup logs and `configs/default.yaml`.
- Secrets are never logged or returned: API keys, passwords, and model
  credentials are redacted to `[REDACTED]` in log records and absent
  from diagnostics and `/metrics` output (see
  `tests/integration/test_secret_leak.py`).
- Validation gates for this area (offline only):

  ```bash
  UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/unit/test_logging.py tests/unit/test_metrics.py tests/integration/test_metrics.py tests/unit/test_cli.py tests/integration/test_secret_leak.py -q
  UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests -q
  UV_CACHE_DIR=/tmp/uv-cache uv run ruff check src tests
  UV_CACHE_DIR=/tmp/uv-cache uv run ruff format --check src tests
  UV_CACHE_DIR=/tmp/uv-cache uv run mypy src
  UV_CACHE_DIR=/tmp/uv-cache uv run pip-audit --progress-spinner=off
  ```

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

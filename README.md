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

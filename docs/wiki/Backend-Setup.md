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

## Key routes

- `POST /api/v1/search`, `GET /api/v1/suggest`,
  `POST /api/v1/feedback`
- `POST /api/v1/agent/chat` (chat turns, trace, perspectives)
- `GET /api/v1/frontier/*` + pause/resume/clear/settings
  (mutations need `X-Admin-Token`)
- `GET/PUT /api/v1/admin/config/*` (same token gate)
- `GET /metrics` (opt-in), `/healthz`

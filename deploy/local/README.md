# OpenLens local deploy (Docker)

From the repo root:

```bash
docker compose -f deploy/local/docker-compose.yml up --build -d
```

- Frontend: http://localhost:3000 (proxies `/api/` to the backend, no CORS setup)
- Backend: http://localhost:8000 (`/healthz`, `POST /api/v1/search`)
- OpenSearch: http://localhost:9200 (disabled in `configs/providers.yaml` by
  default; enable it there to use it)

Stop with `docker compose -f deploy/local/docker-compose.yml down`.
Add `-v` to also drop the `openlens-data` and `opensearch-data` volumes.
Seed the index by pointing the `custom_index` provider at a populated
`data/openlens.db`, or enable the OpenSearch provider after indexing into it.

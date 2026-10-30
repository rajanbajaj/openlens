# OpenLens local deploy (Docker)

From the repo root:

```bash
docker compose -f deploy/local/docker-compose.yml up --build -d
```

- Frontend: http://localhost:3000 (proxies `/api/` to the backend, no CORS setup)
- Admin frontend: http://localhost:3001 (`${ADMIN_FRONTEND_PORT:-3001}`; frontier dashboard + config editors, same `/api/` proxy)
- Backend: http://localhost:8000 (`/healthz`, `POST /api/v1/search`)
- Postgres: http://localhost:5432 (the default store)
- pgAdmin: http://localhost:5050 (browser UI; log in with
  `PGADMIN_DEFAULT_EMAIL` / `PGADMIN_DEFAULT_PASSWORD` from `.env`,
  then add a server with host `postgres`, port `5432`, and the
  `POSTGRES_*` credentials)
- Redis: http://localhost:6379 (the default search-result cache;
  shared by every backend replica, TTL-bounded)
- Kafka: 3-broker KRaft cluster (`kafka-1..3`, no Zookeeper).
  Internal: `kafka-1:19092,kafka-2:19092,kafka-3:19092`; host debug:
  `localhost:9092/9094/9095`. Topics auto-create with 12 partitions.
- Kafka UI: http://localhost:8080 (`${KAFKA_UI_PORT:-8080}`) — topics,
  `crawlers` consumer-group lag, and message browser for `crawl.urls`.
- Crawler: Kafka consume loop (`crawl-worker --queue kafka`, PID 1)
  plus the heartbeat reporter (`python -m openlens.indexing.worker`)
  against `http://backend:8000`; replicas visible at
  `GET /api/v1/frontier/workers` (the crawl dashboard lives in the
  admin app, not the user frontend). One replica runs by default.

Scale the crawl workforce with:

```bash
docker compose -f deploy/local/docker-compose.yml up --build -d --scale crawler=3
```

Worker notes: replica ids auto-resolve (unique hostnames), so no extra
config is needed; set `OPENLENS_WORKER_ID` in `.env` only to pin one
replica. Every replica joins the same Kafka consumer group
(`crawlers`), so each URL goes to exactly one live worker; up to 12
replicas get their own partition share. Heartbeats are admin-gated
when `OPENLENS_FRONTIER_ADMIN_TOKEN` is set (compose forwards it, so
keep it in sync with the backend).
Postgres keeps documents, checkpoints, settings, and the seen-set.
Pool math: each replica holds a documents pool plus a frontier
pool (~2 × `pool_size` server connections at default settings), so size
Postgres `max_connections` as N × (pool_size × 2).

Stop with `docker compose -f deploy/local/docker-compose.yml down`.
Add `-v` to also drop the `openlens-data`, `pgdata`,
`pgadmin-data`, `redis-data`, and `kafka1/2/3-data` volumes.
The seed corpus is indexed into Postgres automatically on first boot
(tables are created with `CREATE TABLE IF NOT EXISTS`).

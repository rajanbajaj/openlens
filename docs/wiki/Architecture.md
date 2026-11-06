# Architecture (developers)

## High-level design

Four views: system context, search path, crawl path, analytics
path. Service ports are the `deploy/local` defaults; the crawler
count assumes `--scale crawler=6` against 12 `crawl.urls`
partitions.

### System context

```mermaid
flowchart TB
    U[Browser :3000] --> B[Backend API :8000]
    A[Admin console :3001] --> B
    B --> PG[(Postgres primary :5432<br/>documents + frontier + settings)]
    B --> PGR[(Postgres replica :5433<br/>search reads)]
    B --> R[(Redis :6379<br/>result cache)]
    B --> RF[(redis-frontier :6380<br/>frontier leases/queues)]
    W <--> RF
    B --> K[Kafka KRaft x3<br/>crawl.urls x12 + analytics.events]
    B --> CH[(ClickHouse :8123/:9000<br/>openlens_analytics)]
    W[Crawler workers xN] <--> K
    W --> PG
    W -->|heartbeats| B
    RF -.->|write-behind mirror<br/>ops reads stay on PG| PG
    AW[Analytics worker x1] --> K
    AW --> CH
    P[pgAdmin :5050] -.-> PG
    KU[kafka-ui :8080] -.-> K
```

### Search path

```mermaid
flowchart TB
    REQ[POST /search or /agent/chat] --> VAL[Validate schemas]
    VAL --> ENG[SearchEngine + Redis result cache 120s]
    ENG --> PROV[Providers: custom_index over Postgres]
    PROV --> P1[1 query_parsing]
    P1 --> P2[2 query_rewrite]
    P2 --> P3[3 retrieval]
    P3 --> P4[4 normalization]
    P4 --> P5[5 deduplication]
    P5 --> P6[6 filtering]
    P6 --> P7[7 scoring]
    P7 --> P8[8 ranking]
    P8 --> P9[9 enrichment]
    P9 --> P10[10 explanation]
    P10 --> PER[Perspectives: clusters + stance + honesty notes]
    P10 --> AG[Agent mode: retrieval/agent.py reuses engine]
    PER --> RESP[SearchResponse]
    AG --> RESP
```

### Crawl path

```mermaid
flowchart TB
    SEED[Seeds: resume-with-seeds or crawl-requests] --> K[Kafka topic crawl.urls]
    K --> CON[Consume: one URL per live worker]
    CON --> ENQ[Enqueue: Redis ready queue<br/>ZSET priority/depth + seen set]
    ENQ --> CLM[Claim: SET NX lease + heartbeat PEXPIRE]
    CLM --> POL[Push-time gates: domain/policy/robots/depth]
    POL --> FET[Fetch: politeness + conditional + 5s bound]
    FET --> Q[Quality gate: reject skips expansion]
    Q --> IDX[Index documents to Postgres]
    Q --> LINK[Discovered links batch-published to topic]
    LINK --> K
    IDX --> TERM[Terminal: mark done/failed/skipped in Redis]
    TERM --> COM[Commit offset: at-least-once + store dedup]
    TERM --> FL[Write-behind flusher → Postgres mirror<br/>batches every flush_interval_s]
    FL --> OPS[Ops reads: failed lists, domains, requeue<br/>served from mirror, lag ≤ 5s]
    CLM --> HB[Heartbeat claimed/stats/discovery to backend]
    HB --> API[GET /frontier/status + live socket]
```

Hot path (`frontier.backend: redis`, default stays `postgres`):
claims, leases, heartbeats, and the ready queue live in
redis-frontier instead of per-URL Postgres round-trips; the flusher
mirrors terminal states, checkpoints, and settings to Postgres, so
every ops read above is served from the mirror with bounded
staleness. Rollback is flipping the flag back — leases TTL out and
Kafka redelivers anything in flight.

### Analytics path

```mermaid
flowchart LR
    S[Search + feedback API] --> T[Kafka topic analytics.events]
    C[Crawl workers: one crawl.page event per fetch attempt] --> T
    T --> AW[Analytics worker: batch flush / 5s]
    AW --> CH[(ClickHouse openlens_analytics<br/>TTL retention, no DELETEs)]
    CH --> AD[Admin: counts + top + series + feedback summary]
```

### Data and infrastructure

| Service | Role | Ports | State |
|---|---|---|---|
| `backend` | FastAPI API + heartbeat registry + WS fan-out | 8000 | stateless |
| `frontend` / `admin-frontend` | nginx SPAs | 3000 / 3001 | stateless |
| `postgres` | documents, frontier, checkpoints, settings | 5432 | `pgdata` |
| `postgres-replica` | async search reads, primary fallback | 5433 | `replica-data` |
| `redis` | SearchEngine result cache (120 s TTL) | 6379 | ephemeral |
| `redis-frontier` | Frontier hot path (leases/queues, `noeviction`, AOF); ops reads stay on Postgres (mirror lag ≤ `flush_interval_s`) | 6380 | `redis-frontier-data` |
| `kafka-1/2/3` | KRaft brokers, `crawl.urls` x12, `analytics.events` | 9092/9094/9095, internal 19092 | `kafka-data-*` |
| `crawler` xN | drain + fetch + index + heartbeat | — | store-backed resume |
| `analytics-worker` | topic-to-ClickHouse batch sink | — | offsets in Kafka |
| `clickhouse` | append-only analytics, TTL expiry | 8123 / 9000 | `clickhouse-data` |
| `pgadmin` / `kafka-ui` | ops inspection | 5050 / 8080 | stateless |

Cross-cutting rules: mandatory pipeline plugins fail closed;
reporting and fan-out fail open (a down broker stalls, never
crash-loops). Claimed rows carry owner leases (120 s TTL); offsets
commit only after index. Drains are time-boxed under 1/4 of
`max_poll_interval_ms` so a busy consumer is never mistaken for a
dead one. Kafka lag without members means nobody has polled yet —
watch heartbeats and `queue_depth`, not the member count
([troubleshoot](../troubleshoots/2026-11-02-kafka-group-fewer-members-than-crawlers.md)).

Deep dives: [Search Path](Search-Path) (stages, providers, ranking,
agent mode) and [Crawl Path](Crawl-Path) (frontier, fetch pipeline,
retry budget).

## Request path

`POST /api/v1/search` → validate (`api/schemas.py`) → `SearchEngine`
(`core/engine.py`) → provider retrieval → 10-stage pipeline
(`core/pipeline.py`) → ranker → explanations → response assembly →
`SearchResponseSchema.from_core`. Agent mode (`mode="agent"`) and
`POST /api/v1/agent/chat` branch into `retrieval/agent.py`, which
reuses the same in-process engine as its only search tool.

## Plugin pipeline

`core/interfaces.py` owns the stable shapes; `core/context.py` the
per-request state. Plugins declare stage, type, dependencies, and
whether they are **mandatory** (fail closed — unfiltered results
never render) or **optional** (failures isolated into diagnostics).
Ranking profiles (`configs/ranking.yaml`) are per-mode and validated
at startup.

## Index and storage

`custom_index` provider over Postgres (`storage.postgres`), with
SQLite for offline/tests and a MySQL extra. Documents carry metadata
JSON plus promoted columns. Drivers lazy-import; missing drivers
raise `ProviderError(UNAVAILABLE)`, never raw import errors.

## Perspectives (neutrality track)

- `retrieval/story_clusters.py` — pure title-overlap clustering
  (Jaccard + subject veto + time window), no I/O, no model.
- `retrieval/stance.py` — model-first stance judge with a
  deterministic keyword fallback; never raises.
- `retrieval/perspectives.py` — assembles the wire block from the
  ranked page (pipeline `story_id` first, clusterer for orphans),
  plus honesty notes (missing sides, trust caveats) and the
  extractive fallback answer.
- The search pipeline's `news-reranker` still owns ranking signals
  and internal `story_groups`; the API block never exposes it.

## Frontend contract

Backend `openapi.json` snapshots are checked in per app; orval
regenerates `src/api/generated/`. Hand-mirrored `api/types.ts` plus
runtime guards (`api/guard.ts`) degrade malformed fields to absent.

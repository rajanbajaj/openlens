# Architecture (developers)

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

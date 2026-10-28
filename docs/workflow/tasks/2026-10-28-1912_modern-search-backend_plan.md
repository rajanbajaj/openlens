# Modern Search Backend Plan (retrieval quality + search API surface)

## Goal

Make OpenLens search like a modern engine on the backend: hybrid
keyword-plus-optional-semantic retrieval over the custom index, and the
missing query-assistance API surface (suggestions, spelling, related
searches, robust pagination) — all behind the existing provider/plugin
seams, all disabled-by-default or fail-open, offline suite green.

## Success Criteria

- `POST /api/v1/search` responses carry assistance fields
  (`suggestions`, `spelling`, `related`) and stable pagination
  (`limit`/`offset` passthrough plus a `next_offset` hint) without
  changing any existing field's shape.
- New `GET /api/v1/suggest?q=…` returns prefix completions from index
  terms; empty index returns `[]`, never 500.
- Hybrid retrieval (FTS5 candidates + optional embedding signal fused
  with the existing RRF helper) improves or matches keyword-only recall
  on the repeatable eval corpus, is deterministic (3× same order), and
  is a no-op when no embedding backend is enabled.
- Crawl breadth work is config-driven (per-domain policy, refresh
  reporting) with no new required daemons; sandbox-blocked network
  paths skip exactly like existing loopback tests.
- Full backend gates green: pytest, ruff check, ruff format --check,
  mypy, pip-audit; no secrets in logs/metrics/responses.

## Approach

Workspace facts (verified 2026-10-28 against the tree):

- `src/openlens/api/routes.py` exposes exactly one endpoint
  (`POST /api/v1/search`); `api/schemas.py` is the Pydantic-only HTTP
  contract layer, so all wire additions land there.
- `src/openlens/retrieval/` already owns `query_parser.py`,
  `normalizer.py`, `deduplicator.py`, `fusion.py` (RRF baseline),
  `query_processor.py` (normalize → intent → expand) plus
  `query_expand/intent/normalize.py`. No suggest/spell module exists.
- `ModelManager` (`src/openlens/models/manager.py`) already exposes
  `embed(texts)` with `local` + `openai_compat` backends, both disabled
  by default; AI understanding is rule-fallback otherwise. The semantic
  signal therefore reuses `embed()` and fails open when disabled.
- `providers/custom_index.py` serves the SQLite FTS5 store
  (`storage/` DocumentStore); `indexing/seed.py` is 94 lines, so the
  suggest-vocabulary and eval corpus both need a bigger offline fixture.
- Observability (`observability/logging.py`, `metrics.py`) already
  emits per-request records with fixed label names; new timings reuse
  the same feed points, never new label dimensions.

Key decisions:

| Decision | Recommendation | Why |
|---|---|---|
| Fusion method | Reuse `retrieval/fusion.py` RRF for keyword+semantic | Same pattern as federated search; no uncalibrated score mixing |
| Semantic failures | Fail open (signal absent, keyword order kept) | Mandatory-fail-closed applies to safety/privacy, not quality signals |
| Suggest source | Index terms + anonymized query counts, in-process | No network, no new service; matches offline-first bar |
| Pagination | Keep `limit`/`offset`, add `next_offset` hint only | No cursor-state service to operate in the monolith |
| Click signals | Optional `POST /api/v1/feedback` storing counts only | Future ranking input; no accounts, no PII |

## Steps

1. Contracts + suggest helpers: extend `api/schemas.py` (suggest
   response, assistance fields, `next_offset`); new
   `retrieval/suggest.py` (prefix completion, edit-distance spelling,
   related-query derivation) with offline unit tests.
2. Hybrid retrieval: new `retrieval/semantic.py` (`embed` via
   `ModelManager`, cosine, disabled-no-op); custom-index provider fans
   out to both signals and fuses with RRF; eval corpus fixture grows
   past the seed set; hybrid recall ≥ keyword-only proven offline.
3. Suggest/feedback routes + pagination hint: `GET /api/v1/suggest`,
   assistance wiring in search, `POST /api/v1/feedback` (counts only),
   secret-leak coverage for every new field.
4. Index breadth: per-domain crawl policy in config, refresh/duplicate
   reporting from existing hashes, scheduling hook on the existing
   `run_index` CLI delegation (no daemon); network-dependent tests skip
   like existing loopback tests.
5. Integrate: engine wiring, full gates, DoD checklist with evidence.

## Validation Plan

- `UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests -q`
- `UV_CACHE_DIR=/tmp/uv-cache uv run ruff check src tests`
- `UV_CACHE_DIR=/tmp/uv-cache uv run ruff format --check src tests`
- `UV_CACHE_DIR=/tmp/uv-cache uv run mypy src`
- `UV_CACHE_DIR=/tmp/uv-cache uv run pip-audit --progress-spinner=off`
- New tests: suggest determinism + empty-index `[]`, spelling fixture,
  hybrid 3× determinism, semantic-disabled byte-identical order,
  feedback counts-only (no PII stored), secret-leak sweep of new fields.
- Manual (reported, not claimed): one live `muse-spark-1.3` semantic
  query with `MODEL_API_KEY` set; Docker search smoke.

## Risks / Open Questions

- Embedding latency on large corpora: bounded by existing provider
  timeouts; semantic path is opt-in and server-side only.
- Suggest quality on a tiny local index: prefix source degrades
  gracefully to `[]`; quality bar is behavior, not coverage.
- None open: namespaces (`_signals`, `assistance`) follow existing
  conventions; no core-interface breakage.

## Definition of Done

1. `GET /api/v1/suggest` live with offline tests.
2. Search assistance fields (`suggestions`, `spelling`, `related`)
   populated with tests.
3. Hybrid retrieval shipped disabled-by-default with eval evidence.
4. Feedback endpoint counts-only with secret-leak test.
5. Per-domain crawl policy + refresh reporting configured.
6. All five backend gates green with exact commands evidenced.

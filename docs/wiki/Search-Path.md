# Search Path: Core Concepts

How a query becomes an answer. Companion to the
[Architecture HLD](Architecture#high-level-design); code lives in
`src/openlens/core/` (engine, pipeline, context), `providers/`
(retrieval sources), `retrieval/` (query + post-retrieval logic),
`ranking/` (profiles, ranker), `models/` (LLM backends), and
`src/openlens/plugins/` (stage plugins).

## The 10 stages

Every request flows through `PipelineStage.ordered()` in
`core/interfaces.py`. Each stage runs its plugins in order; a plugin
declares `stage`, `mandatory`, and dependencies. Mandatory plugins
fail closed (a failure aborts the request); optional ones degrade
(the failure is recorded in `SearchContext` diagnostics and the
pipeline continues).

1. **query_parsing** — structure the raw string (`retrieval/query_parser.py`,
   `query_normalize.py`; AI-assisted `plugins/ai_understanding.py`).
   Intent detection (`query_intent.py`) picks the mode profile here.
2. **query_rewrite** — expand and rephrase for recall
   (`query_expand.py`; synonyms, spelling, entity variants).
3. **retrieval** — providers fetch candidates. `custom_index` is the
   Postgres-backed default: it translates the FTS5-shaped query to
   native form per backend (`plainto_tsquery` on Postgres —
   phrase quotes, `OR`, and `*` prefixes do not survive; only doc-id
   sets are expected to agree across backends).
4. **normalization** — scores to a common scale
   (`retrieval/normalizer.py`, `fusion.py` for multi-provider merge).
5. **deduplication** — collapse near-identical hits
   (`retrieval/deduplicator.py`) before they can crowd the ranking.
6. **filtering** — remove what must never show. Fail-closed set:
   `safety_filter`, `privacy_filter`, `user_restrictions`,
   `mode_category_filter` (+ `date_range_filter`,
   `ai_classification`). A filter failure aborts, never leaks.
7. **scoring** — additive signals: `relevance_scorer`,
   `authority_scorer`, `trust_scorer`, `integrity_scorer`,
   `freshness_scorer`, `performance_scorer`, `preference_scorer`.
   Optional: a dead scorer degrades its signal, not the request.
8. **ranking** — `ranking/ranker.py` applies the mode's named
   profile from `configs/ranking.yaml` (`default`, `news`,
   `shopping`, `travel`, `child-safe`) to the scored set.
9. **enrichment** — attach display metadata: `trust_enricher`
   (caveats, authority notes), `attribute_enricher`,
   `opinion_detector`, `ai_summary` (model-backed, optional).
10. **explanation** — why-this-result affordances and mode
    rerankers: `commerce_reranker`, `news_reranker`,
    `diversity_reranker` run here so their trade-offs stay visible
    instead of silently reordering.

## Providers and the index

Providers (`providers/`) are retrieval sources behind one
interface; the engine fans out to every enabled provider and fuses
the candidates. The only first-party provider is `custom_index`
(Postgres/SQLite/MySQL via `storage/`); the shape is the extension
point for external sources. Serving reads the store directly, so a
reindex needs no restart — only the 120 s Redis result cache
(`cache/`, `SearchEngine`, TTL 120 s) can briefly serve
pre-reindex hits.

## Post-retrieval intelligence

- **Perspectives** (`retrieval/perspectives.py`,
  `story_clusters.py`, `stance.py`, `claim_extract.py`): same-story
  coverage grouped at query time with per-source stances, coverage
  gaps, and honesty notes. No ranking influence by design.
- **Agent mode** (`POST /api/v1/agent/chat`,
  `retrieval/agent.py`): a model rewrites the query, calls the index
  through budgeted tools, and returns a cited answer — fail-closed
  to standard results when no model is configured. Model access is
  exclusively `MODEL_API_KEY` / `MODEL_BASE_URL` / `MODEL_NAME`
  via the `openai_compat` backend (`configs/models.yaml`); there
  are no vendor keys.
- **Suggest** (`GET /api/v1/suggest`): prefix index with an
  in-memory per-process cache on the engine; malformed backends
  degrade to silence, never an error surface.
- **Feedback** (`POST /api/v1/feedback` + admin summary): anonymous
  counts with reasons, the training signal for ranking work.

## Failure contract

Fail closed on retrieval, filtering, and validation (mandatory
plugins, `validateChatMessage`, schema guards). Fail open on
enrichment, perspectives, and fan-out: one dead source must never
take down the answer. Diagnostics (`SearchContext`) record what
ran, what was skipped, and why — the admin trace view reads the
same structure the pipeline writes.

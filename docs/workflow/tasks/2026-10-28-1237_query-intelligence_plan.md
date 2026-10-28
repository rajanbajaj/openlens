# Query Intelligence Plan

## Goal

Understand the query before retrieval: normalize, classify intent, and
expand — so downstream providers, filters, and rankers work with intent,
not raw strings (original plan §9A).

Falsifiable bar: for a fixed fixture table of ≥20 queries, the stage
returns the expected normalized form, expected intent label, and expected
expansion set with per-term provenance on every row; any explicit
`mode` in the request passes through byte-identical in 100% of rows.

## Reality baseline (verified 2026-10-28 against the tree)

- `retrieval/query_parser.py` is a lowercase `[a-z0-9]+` tokenizer joined
  as an FTS5 AND query. It drops non-ASCII (`café` → `caf`), with no
  unicode normalization and no stop-word handling.
- `retrieval/normalizer.py` is a RESULT normalizer
  (`(Document, score) → SearchResult`), NOT a query normalizer. There is
  no query-text normalization module; do not confuse the two.
- `SearchRequest` has `mode: str = "default"`; `Ranker.run` selects the
  profile via `get_profile(request.mode or fallback)` and FAILS LOUDLY
  (`ConfigError`) on unknown non-empty modes (an empty `mode` silently
  uses the ranker fallback — the API rejects blank modes, direct engine
  callers are not checked). Existing profiles: `default`, `child-safe`,
  `news`, `shopping`, `travel`. Any suggested mode MUST be one of these
  (or a profile added in the same task with config validation).
  Intent labels are not modes: only `shopping`/`travel`/`news` map 1:1
  (see Scope) — `child-safe` is never suggested, only ever explicit.
- `PipelineStage` has `QUERY_PARSING` / `QUERY_REWRITE` enum values, but
  `SearchEngine.search` calls NO query-processing stage today — it goes
  straight to provider search, and `CustomIndexProvider.search` parses
  internally. The implementer ADDS the call site in the engine (before
  provider selection); there is no existing stage to "wire into". In
  particular this stage can NOT be an ordinary pipeline plugin: the
  `Pipeline` runs after provider retrieval, so a `QUERY_PARSING` plugin
  would see the query only after it was already searched.
- `models/interfaces.py` defines `AIModelProvider.embed(texts)` AND
  `classify(text, labels)`; `models/fake.py` implements both
  deterministically offline. Semantic hooks reuse these two methods —
  never a new model interface.
- API caps input at the wire only: `MAX_QUERY_LENGTH = 1000`
  (`api/schemas.py`, Pydantic `Field(max_length=...)`).
  `SearchEngine.search` and `SearchRequest` enforce no length bound, so
  direct engine callers (tests, CLI, future federated fan-out) bypass
  the cap — the stage MUST enforce its own input bound (reject or
  truncate per its config), not assume the API's.
- Provenance visibility gap: `SearchContext.diagnostics()` returns only
  `fallback_used` / `provider_latencies_ms` / `plugin_executions` /
  `errors` — `context.metadata` is NOT included, and the API
  `DiagnosticsSchema` copies only those four keys. Provenance stored
  only in `context.metadata` is therefore invisible to API callers;
  step 3 pins how it surfaces instead of assuming metadata is enough.

## Scope (all connected: one query-processing stage)

- Query-text normalization (new module, e.g.
  `retrieval/query_normalize.py`): unicode NFKC, casefold, whitespace
  collapse, punctuation policy — hardened so parser output is stable.
  Must not change `retrieval/normalizer.py`'s result-normalizer role.
- Intent classification: navigational / informational / shopping / travel
  / news → sets a suggested mode + machine-readable signals on the
  request/context (never silently overrides an explicit `mode`:
  explicit mode always wins; suggestion recorded separately for
  observability). Pinned intent→mode map: `shopping→shopping`,
  `travel→travel`, `news→news`, `navigational→default`,
  `informational→default`; `child-safe` is never a suggestion.
- Query expansion: synonym/related-term expansion with per-term
  provenance (each added term carries its source rule/set id;
  provenance must survive through to `SearchContext.metadata` /
  diagnostics, not silent rewrites). Hard cap on added terms
  (default ≤8 per query, config-tunable). Synonym/intent keyword sets
  live in the stage's own config dict (same `{name, config}` shape the
  plugin registry already validates, supplied via a new `query:` key
  the engine consumes — no new config file), each set carrying the id
  that provenance records. Expansion is passed downstream as a
  structured term set, never as a rewritten query string:
  `CustomIndexProvider.search` re-parses `request.query` with the
  AND-joining `parse_query`, so a concatenated expansion string would
  turn OR-synonyms into AND-required terms and empty most result sets.
  Step 4 pins the exact hand-off (expanded terms travel alongside the
  original query and the provider builds an OR-of-ANDs `MATCH`).
- Semantic hooks: `AIModelProvider.embed` / `classify` reuse kept
  OPTIONAL and OFF by default (fake backend in tests); no heavy model
  dependencies in this task. When enabled, each call is bounded by the
  backend's `timeout_s` (the `providers/base.py` `run_bounded` pattern);
  an unbounded synchronous model call on the pre-retrieval path is not
  acceptable even behind the off-by-default flag.

## Steps

1. Query-text normalizer + fixture tests (unicode, mixed case,
   punctuation-only input, 1000-char input, over-cap input from a direct
   engine caller); prove existing `test_custom_index.py` parser
   expectations still pass or are updated with justification.
2. Intent classifier (rule-based, config-driven keyword sets compiled
   ONCE at `configure()` into set/trie lookups — never recompiled per
   request) + fixture-table tests incl. ties (deterministic priority
   order documented in config schema). Lifecycle pinned: the stage is a
   standalone query processor constructed and configured from
   `SearchEngine.configure` / `build_engine` (its `configure()` is that
   construction step) — it is not built by `PluginRegistry` and does not
   run inside the post-retrieval `Pipeline`.
3. Expansion with provenance tracking + tests proving provenance keys
   survive into context metadata AND into the engine-level diagnostics
   payload (extend `SearchContext.diagnostics()` or the engine's
   response assembly to carry a bounded `query` provenance entry; the
   API `DiagnosticsSchema` passthrough for that entry is named here as
   a required touch in `api/schemas.py`, otherwise provenance stays
   engine-internal — see cross-plan notes); term-cap enforcement test.
4. Engine call-site: invoke the stage in `SearchEngine.search` ahead of
   provider selection, handing expanded terms to providers as the
   structured set pinned in Scope (test: an expanded synonym matches a
   document the unexpanded AND query misses, and the original terms
   remain required); mode-suggestion rules + tests (explicit mode
   always wins; unknown suggested modes impossible by construction —
   validated against `get_profile` at startup).
5. Regression: existing parser/normalizer tests untouched and green
   (normalize-then-parse equivalence on the seed corpus queries).

## Validation Plan

- `uv run pytest tests/unit/test_query_intelligence.py tests/unit/test_custom_index.py -q`
  green; then `uv run pytest tests -q` green.
- `uv run ruff check src tests`, `uv run ruff format --check src tests`,
  `uv run mypy src` clean.
- Manual: `"best dresses under 3000"` (no mode) → suggested
  `shopping` with reasons listing matched keywords; same query with
  explicit `mode: news` → executes `news`, suggestion recorded but not
  applied. Non-ASCII query (`"café reviews"`) → normalized without
  token loss vs. today's `caf` behavior (document the before/after).

## Scalability notes

| Bottleneck | Breaks at | Fix shape (folded in or flagged) |
|---|---|---|
| Unbounded synonym fan-out → giant FTS MATCH per request | 10x lexicon / long queries | Hard cap on added terms (in scope); OR-of-ANDs shape, never raw string concat |
| Keyword-set scan per request (sets × terms) | 100x QPS with large config sets | Compile sets once at `configure()` (in scope); single-pass lookup |
| Per-request `classify`/`embed` model calls (N+1 backend fan-out) | Any real backend at 10x QPS | Hooks optional + off by default (in scope); each call timeout-bounded (in scope); batching/caching flagged for AI-capabilities plan |
| Provenance list growth | Long queries | Bounded by the same term cap (in scope) |
| Synchronous pre-retrieval CPU (normalize + classify) on every request | 100x QPS | Rule path is O(query length) with compiled sets (in scope); repeated-query memoization, if ever added, must be a bounded LRU — an unbounded query cache is explicitly rejected here |
| Oversized input from direct engine callers (API 1000-char cap bypassed) | Any non-API caller | Stage enforces its own bound (in scope, step 1) |

Rewrite-forcing risk (flagged, not built): none at 100x for the
rule-based path; learned intent/rerank models move this stage toward a
batched inference service — owned by the AI-capabilities plan.
Cross-plan seam (review S1/D5, left for synthesis to confirm): when this
stage is present, the AI-capabilities plan's model-based understanding
defers to these deterministic signals and its rule fallback runs only
when this stage is absent — never two competing intent labels on one
request.

## Non-goals

- Learned embeddings/rerank models (belongs to AI Capabilities; this
  task only keeps the `embed`/`classify` seams optional and working).
- Query spelling correction.
- Changing ranking profiles or provider selection (this task suggests a
  mode; scoring/rerank packs own what modes mean; federated-search owns
  multi-provider selection).
- Rewriting `retrieval/normalizer.py` (result normalization stays as is).

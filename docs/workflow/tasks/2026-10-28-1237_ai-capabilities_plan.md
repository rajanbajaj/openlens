# AI Capabilities Plan

## Goal

Make AI a working, optional, swappable capability: the engine boots with
or without AI, and ships AI plugins for query understanding,
advisory classification, and summarization/perspective enrichment —
fake backend by default, one real backend live by config (original
plan §9F + principles 5–6).

Falsifiable: with `models.fake.enabled: true`, a search returns
AI-labeled enrichments; with every `models.*.enabled: false` the engine
boots and returns identical results minus AI fields; killing the model
mid-query still excludes unsafe hits via deterministic rules.

## Reality baseline (verified against the tree — read before implementing)

- `ModelManager.from_config` (`src/openlens/models/manager.py`) exists
  and routes to the primary backend — but it **raises `ConfigError`
  when zero backends are enabled** (see
  `test_models_yaml_loads_offline_with_everything_disabled`). So
  "all-AI-disabled is a valid config" does NOT mean `from_config`
  succeeds with none enabled: the engine wiring must treat the manager
  as optional (skip `from_config` / catch `ConfigError` when no entry
  is enabled) and boot without AI. `configs/models.yaml` ships with
  every backend disabled.
- `AISummaryPlugin` (`src/openlens/plugins/ai_summary.py`) already
  exists: ENRICHMENT-stage, optional, defaults to `FakeModelProvider`,
  swappable via per-plugin `model: {type, ...}` config or constructor
  injection. It is NOT wired through `ModelManager`, and `ai-summary`
  is NOT in `BUILTIN_PLUGINS` (`src/openlens/core/registry.py`) — today
  it resolves only via a `module:Class` path. This plan must pick ONE
  seam (decision recorded in step 1): either route AI plugins through
  the manager's primary, or keep per-plugin `model:` config. Do not
  ship both.
- Real-backend config shape + key handling already exist:
  `configs/models.yaml` declares `claude`/`muse`/`local` with
  `${VAR:-}`-resolved `api_key`/`endpoint`, and
  `src/openlens/models/backends.py` validates credentials at configure
  time. The stubs raise `ProviderError(UNAVAILABLE)` on inference. The
  remaining work is a live client for ONE backend, not config plumbing.
- `BaseModelProvider` validates `timeout_s` (default 10s, ceiling 300s)
  in `src/openlens/models/base.py` but nothing enforces it — neither the
  stubs (`backends.py`), the fake (`fake.py`), nor `ModelManager`
  (no bounded-call wrapper) applies the timeout to inference. Search
  providers already solve the same problem: `ProviderManager._search_one`
  and `BaseSearchProvider.run_bounded`
  (`src/openlens/providers/base.py`, `manager.py`) run calls on a daemon
  thread and raise `ProviderError(TIMEOUT)` on overrun. The live model
  client must reuse that pattern (or `httpx` native `timeout=`), not
  invent a second mechanism.
- `SafetyFilter` (`src/openlens/plugins/safety_filter.py`) is mandatory
  (`mandatory=True`), deterministic (blocked terms/domains substring
  match over title/snippet/url, fail-closed via `PluginError(mandatory=True)`).
  The filter-pack plan explicitly assigns AI classification to THIS plan
  and keeps deterministic rules for itself — the seam is clean: model
  labels are advisory only, rules still own exclusion. Proof obligation:
  excluded hits stay excluded with the model disabled, enabled-returning-
  "safe", and raising mid-query (step 3 tests all three).
- Config-source gap: `main.load_app_config()` / `build_engine(config)`
  (`src/openlens/main.py`) load only `configs/default.yaml` (or
  `$OPENLENS_CONFIG`); they never read `configs/models.yaml`, and
  `default.yaml` has no `models:` section today. Wiring step 1 must
  decide where the `models:` section lives at runtime (embed in
  `default.yaml` vs load `models.yaml` as a second file) — `load_config`
  (`src/openlens/config.py`) resolves `${VAR}` per-file, so a second
  file keeps secret handling identical. `test_models_yaml_*` in
  `tests/unit/test_ai_seams.py` loads `models.yaml` directly; no engine
  test boots a manager today.
- Pipeline-stage gap: `PipelineStage.QUERY_PARSING` / `QUERY_REWRITE`
  exist (`src/openlens/core/interfaces.py`), but `SearchEngine.search`
  (`src/openlens/core/engine.py`) calls `_search_with_fallback` first
  and only then runs `Pipeline` → `Ranker`. A QUERY_PARSING plugin
  therefore executes *after* retrieval today and cannot influence which
  documents are fetched. Query-understanding in this plan is thus
  post-retrieval intent/entity labeling unless the engine grows a
  pre-retrieval hook (coordinate with the query-intelligence plan's
  engine call site, seam S1 in the review plan — one hook, not two).
  Scope below treats labeling as the v1 deliverable and names the hook
  as an explicit dependency, not an implicit assumption.
- `httpx>=0.27` is already a core dependency (`pyproject.toml`);
  `src/openlens/providers/http.py` / `external.py` show the lazy-import
  + `timeout=self.timeout_s` + error-mapping pattern to mirror for the
  `local` client. No new HTTP dependency and no vendor SDK is needed.
- Per-plugin latency is already recorded: `Pipeline.run`
  (`src/openlens/core/pipeline.py`) appends one `PluginExecution`
  (name/stage/status/latency/error) per plugin to
  `SearchContext.diagnostics()`. This plan does not need a new latency
  hook for plugin-level timings; only *per-result* model-call breakdown
  inside one plugin would need new metadata, and that is explicitly out
  of scope (see Scalability bottleneck 1 fix shape).
- `AISummaryPlugin` details the cap must respect: source text is
  `f"{title} {snippet}"[:max_chars]` (`max_chars` default 500, validated
  [1, 10000] in `configure`), and `run` loops `context.results` serially
  raising `PluginError` on any backend exception (isolated by the
  pipeline because the plugin is optional). The top-K cap in this plan
  bounds that loop; `FakeModelProvider` truncates completions to 120
  chars and derives embeddings deterministically (dim default 8, ceiling
  64) — tests must not assume longer fake output.
- No query-understanding, classification, or perspective plugin exists
  yet (only the `AIModelProvider.complete/embed/classify` interface and
  the fake backend). `src/openlens/plugins/` today holds only
  `ai_summary.py`, `relevance_scorer.py`, `safety_filter.py`,
  `trust_enricher.py`, `base.py`.

## Scope (all connected: one AI integration surface)

- Engine wiring: build an optional `ModelManager` from the `models:`
  section at startup; zero-enabled boots without AI (no manager, no
  AI plugins fail the boot). Single `primary` routing; no per-plugin
  backend fan-out. Config source is decided in step 1 (see Reality
  baseline gap) and the choice is recorded in this file before any
  code lands — implementers do not re-decide it.
- AI query understanding: one QUERY_PARSING/QUERY_REWRITE-stage plugin
  producing intent/entities via the model, with a rule-based fallback
  (keyword/pattern heuristics) when no backend is enabled. Fallback
  output is labeled as rule-based (`ai_model: rule-based` vs the
  backend name). V1 is post-retrieval labeling only (see pipeline-stage
  gap); influencing retrieval requires the shared pre-retrieval engine
  hook owned jointly with the query-intelligence plan and is out of
  scope here beyond declaring the dependency.
- AI safety/privacy classification: one FILTERING-stage plugin that is
  OPTIONAL (`mandatory: false`) and advisory-only — it writes labels
  into `result.metadata` / `result.warnings`, never excludes and never
  mutates `context.results`. The mandatory `SafetyFilter` still
  excludes alone. Model outage (or no backend) means "no advisory
  labels", never "gate opens". The plugin must not import or call
  `SafetyFilter`; the seam is data-only (labels in metadata).
- AI summarization + perspective analysis: extend the existing
  `AISummaryPlugin` pattern to per-result `ai_summary` and
  `ai_perspectives` metadata, each labeled with the generating backend
  name (`ai_model`). Enrichment is capped (see Scalability): top-K
  results only via a single config key `enrich_top_k` (default 5,
  validated [0, 20]; `0` disables enrichment without disabling the
  plugin), applied after filtering/scoring so K counts surviving
  results, not raw hits. One shared model-call helper serves both
  summary and perspective prompts — not two independent loops.
- One live real-backend client (pick `local` first: no vendor key in
  CI, endpoint-gated): reuse the existing stub's config shape and
  credential validation (`LocalModelProvider.configure` in
  `backends.py` — `endpoint` required, `model` default `"default"`),
  implement `complete`/`embed`/`classify` over `httpx` with
  `timeout_s` enforced via the native `httpx` timeout (mirroring
  `providers/http.py:_fetch`) and errors wrapped via
  `BaseModelProvider.wrap_error` (never leak raw client exceptions or
  the endpoint credential, if any, into messages/logs). `health_check`
  stays never-raising and returns `False` when unreachable, matching
  the stub contract. Offline suite uses fake only, no network, no keys;
  live-client tests use a local fake HTTP server (e.g. `httpx` mock
  transport or a loopback stub) or recorded fixtures, never the real
  network.

## Steps

1. Manager seam + config-source decisions (settled — do not re-decide)
   + optional wiring + tests. DECISIONS (settled pre-code gates; the
   implementer does not re-decide): (a) injection seam =
   **manager-primary** — the engine builds one `ModelManager` and
   injects its primary into AI plugins (the plan's own criteria: ≥2
   AI plugins need a backend and Scope forbids per-plugin different
   backends in v1, so the per-plugin `model:` alternative is rejected;
   shipping both remains a defect). (b) runtime `models:` source =
   **load `configs/models.yaml` as a second file alongside
   `configs/default.yaml`** (the file already exists, tests already
   load it directly, and `load_config` resolves `${VAR}` secrets
   per-file identically; embedding in `default.yaml` is rejected).
   Implement: build manager
   only when ≥1 backend enabled, else boot with `model_manager=None`
   (catch the zero-enabled `ConfigError` from `ModelManager.from_config`
   at the wiring boundary, do not relax `from_config` itself — its
   loud failure is covered by `test_models_yaml_loads_offline_with_everything_disabled`);
   never let "no AI" fail the boot or require a dummy plugin; register
   AI plugins in `BUILTIN_PLUGINS` (`src/openlens/core/registry.py`,
   short names `ai-summary` plus the new understanding/classification
   names) so engine config resolves them without `module:Class` paths.
   Tests (`tests/unit/test_ai_wiring.py`): all-disabled boots and
   searches fine (results identical minus AI fields, ordering
   unchanged); fake-enabled routes through primary; unknown primary
   fails startup with `ConfigError`; disabled backends are never
   instantiated (reuse the `Boom` pattern from `test_ai_seams.py`).
2. AI query understanding with fallback + tests (both paths).
   One QUERY_PARSING/QUERY_REWRITE-stage plugin; v1 labels intent/
   entities post-retrieval (see Reality baseline gap) — the step does
   not claim retrieval influence. If the shared pre-retrieval hook
   (query-intelligence seam S1) has landed, wire to it; otherwise the
   plugin runs in-pipeline and the limitation is documented in the
   plugin docstring. `tests/unit/test_ai_understanding.py`: fake-backend
   path labels intent/entities with backend name in metadata;
   disabled/no-backend path returns rule-based output labeled
   `ai_model: rule-based`; model raising `ProviderError` mid-query
   falls back to rules without failing the request (pipeline isolation
   for optional plugins already guarantees the request succeeds — assert
   diagnostics record the fallback, not a 500).
3. AI classification as advisory input + tests. Rules still exclude
   alone; model outage adds zero labels and never opens the gate.
   `tests/unit/test_ai_classification.py`: advisory labels land in
   `result.metadata`/`result.warnings` only and `context.results`
   length is unchanged by the classification plugin alone; results
   excluded by `SafetyFilter` stay excluded in all three modes —
   (i) model disabled, (ii) model enabled returning "safe" for a
   blocked hit, (iii) model raising `ProviderError`; the plugin's
   `mandatory` is `False` and its `stage` is `FILTERING`.
4. Summarization/perspective enrichments + labeling + cap tests.
   Extend `AISummaryPlugin` (or split into two plugins sharing one
   model-call helper — one helper, not two call loops; the helper
   enforces `enrich_top_k` and `timeout_s` once). Respect the existing
   `max_chars` truncation; do not re-truncate. `tests/unit/test_ai_enrichment.py`:
   labels present with backend name (`ai_model`); enrichment capped at
   configured `enrich_top_k` (results beyond K have no `ai_summary`/
   `ai_perspectives` keys at all, not empty strings); `enrich_top_k: 0`
   yields no AI fields but a successful response; per-call `timeout_s`
   enforced (slow fake server / hanging stub → `ProviderError` with
   code `TIMEOUT` via `wrap_error`, request still succeeds minus AI
   fields because ENRICHMENT is optional — assert via `SearchContext`
   diagnostics, not just absence of fields).
5. One live backend client + key/timeout tests (no live calls in the
   suite). Implement the `local` HTTP client in place of the
   `LocalModelProvider` stub (same class name and `name == "local"`,
   same `configure` validation); keep `claude`/`muse` stubs raising
   `UNAVAILABLE`. Use lazy `import httpx` inside methods (mirroring
   `providers/http.py:_fetch`) so importing `openlens.models.backends`
   never requires network config. `tests/unit/test_model_local_client.py`:
   credential/endpoint validation errors (`ConfigError` on missing
   endpoint, matching stub behavior); timeout enforced against a
   hanging stub server (assert elapsed < 2× `timeout_s` and code
   `TIMEOUT`); error mapping to `ProviderError` codes (timeout → `TIMEOUT`
   retryable, 401/403 → `AUTH`, 429 → `RATE_LIMIT`, 5xx/conn → `UNAVAILABLE`);
   `health_check` returns `False` (never raises) against a dead endpoint;
   secret-leak suite (`tests/integration/test_secret_leak.py`) extended
   to cover model keys/endpoints in logs/errors/diagnostics.

## Validation Plan

- `uv run pytest tests/unit/test_ai_wiring.py tests/unit/test_ai_understanding.py tests/unit/test_ai_classification.py tests/unit/test_ai_enrichment.py tests/unit/test_model_local_client.py tests/unit/test_ai_seams.py tests/integration/test_secret_leak.py -q` green (offline only; the secret-leak extension is part of done, not optional).
- `uv run pytest tests -q` green (offline only); `uv run ruff check
  src tests` and `uv run ruff format --check src tests` clean;
  `uv run mypy src` clean; `uv run pip-audit` with no new unacknowledged
  findings (no new dependency is expected — `httpx` is already present).
- Existing-behavior guard: `uv run pytest tests/unit/test_ai_seams.py -q`
  must stay green unmodified except where step 1's seam decision
  intentionally changes `AISummaryPlugin` construction — any such edit
  is called out in the PR description with the old vs new seam.
- Manual: (a) enable fake (`models.fake.enabled: true` via the config
  source chosen in step 1), run a search, confirm AI fields appear with
  `ai_model: fake` on at most `enrich_top_k` results; (b) disable all
  AI, confirm identical result ids and ordering minus AI fields;
  (c) point `local` at a dead endpoint (`http://localhost:9`), confirm
  mandatory `SafetyFilter` still excludes blocked hits and the request
  succeeds with an AI error recorded in `diagnostics.plugin_executions`
  / `diagnostics.errors`, not a 500.

## Scalability notes

- Bottleneck 1 — serial per-result model calls (breaks first, ~10x
  results or ~10x QPS). `AISummaryPlugin.run` (`ai_summary.py:92-104`)
  calls `complete()` once per result, serially, on the request path.
  At `limit=100` (`MAX_LIMIT` in `src/openlens/api/schemas.py`) that is
  100 sequential model round-trips per search; with a real backend this
  multiplies p99 latency and cost by N and exhausts vendor rate limits.
  Fix (folded into scope): cap enrichment to top-K via `enrich_top_k`
  (default 5, max 20), share one model-call helper with enforced
  timeout, and rely on the pipeline's existing per-plugin latency
  record (no new per-call hook in v1). NOT in scope: batching or
  parallel fan-out — flag for a later pass if the K-cap proves
  insufficient; unbounded fan-out is explicitly rejected.
- Bottleneck 2 — unenforced `timeout_s` (fails at first slow backend,
  any scale). One hung model call hangs the whole request today because
  neither the backend nor `ModelManager` bounds the call. Fix (folded
  into scope): enforce the already-configured `timeout_s` in the live
  `local` client via `httpx` timeout (and the daemon-thread pattern
  from `providers/base.py:run_bounded` where a client lacks a native
  timeout), mapping overruns to `ProviderError(TIMEOUT)`; treat timeout
  as optional-plugin isolation (fail open for enrichment, absent labels
  for advisory classification) so the request still succeeds.
- Bottleneck 3 — repeated summarization of identical content (~100x
  corpus churn / repeated queries). The same document is re-summarized
  on every search that returns it; cost grows with query volume ×
  result overlap, not corpus size alone. Fix shape (flagged, not
  built): bounded cache keyed by (backend, model, content-hash of
  title+snippet) with size cap + TTL and hit/miss counters in
  diagnostics; an unbounded dict cache is explicitly rejected. No cache
  ships in v1 — the K-cap bounds the blast radius until hit-rate data
  justifies one.
- Bottleneck 4 — classification fan-out over the full result set
  (binds at ~10x limit). Unlike summarization, advisory classification
  tempts an implementation to label every result. Fix (folded into
  scope): classification is capped to the same `enrich_top_k` window
  (or a dedicated `classify_top_k` defaulting to it) — never the full
  `limit=100` set — and the cap is asserted in step 3 tests.
- Rewrite-forcing risk: synchronous model calls inside the pipeline.
  The pipeline and FastAPI handlers are synchronous; each in-flight
  search holds a worker thread for the sum of its model calls. If model
  p99 exceeds ~1s at target QPS, thread-pool exhaustion forces an async
  boundary or a worker queue — that redesign is out of scope; the
  K-cap + enforced timeout keeps the monolith viable until then. A
  second rewrite-forcing risk is per-request `embed` calls for semantic
  retrieval: an embedding index / vector store (orphan O1 in the review
  plan) would force new storage infrastructure and stays deferred.

## Non-goals

- Bundled model weights or vendor SDKs in core deps (`pyproject.toml`
  stays `httpx`-only for model I/O). Training/fine-tuning.
- Letting model output override, reorder, or resurrect mandatory
  exclusions, ever — including via `final_score` mutation: AI plugins
  write metadata/warnings only and never touch scores or `context.results`
  membership (classification) or ranking order.
- Second live backend (`claude`/`muse` stay `UNAVAILABLE` stubs),
  streaming completions, per-request fan-out to multiple backends, and
  per-plugin backend selection beyond the single seam chosen in step 1.
- Embedding indexes / vector search / semantic retrieval (review-plan
  orphan O1 — needs a future owning plan; this plan's `embed` stays a
  backend capability with no index consumer).
- Pre-retrieval query rewriting that changes which documents are
  fetched (blocked on the shared engine hook, seam S1) — v1 labels
  intent post-retrieval only.
- Unbounded AI caches, unbounded prompt concatenation (prompts stay
  bounded by the existing `max_chars` truncation), and logging raw
  prompts/completions at INFO (DEBUG-gated only, never with secrets).

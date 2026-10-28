# Sub-Plans Review Plan

## Goal

Review the 9 architecture sub-plans (tasks 8–15, 17) for correctness,
coverage, and consistency — then refine each plan file in place. No
implementation and no workflow runs in this task.

## Scope

- The 9 plan files in `docs/workflow/tasks/`
  (`2026-10-28-1237_*_plan.md` plus
  `2026-10-28-1243_db-connectors_plan.md`), reviewed against
  `docs/workflow/tasks/original-architecture.md` (phases 1–6,
  §1–§16) and the actual tree state.
- Refinements go directly into the individual plan files (scope,
  steps, validation, non-goals). No new plans, no code changes.

## Steps

1. **Coverage trace.** Build a section-by-section map of original
   architecture → sub-plan (§6 providers, §8 strategies, §9A–F
   plugins, §10 pipeline, §11 ranking, §12 API, §13 storage, §14
   testing, §15 phases 4–6, §16 DoD). Flag any requirement with no
   owning plan or with two owners.
2. **Reality check.** Verify each plan's claims about what exists
   (files, behaviors, gaps) against the tree. Correct stale premises
   (e.g. anything already shipped since the plan was written).
3. **Overlap & seam check.** For each pair of adjacent plans
   (query→filters→scoring→rerank, rerank↔federated,
   filters↔AI-advisory), confirm shared boundaries live in exactly
   one plan and cross-references name the owner.
4. **Order check.** Confirm the 8→15 sequence has no forward
   dependency (no plan requiring a later plan's output to validate).
   Reorder rows if one is found.
5. **Quality pass per plan.** Each plan must have: a falsifiable goal,
   steps an implementer can execute without inventing scope,
   validation with exact commands/checks, and non-goals that close
   the obvious scope-creep doors. Rewrite weak sections in place.
6. **Consistency pass.** Uniform naming, no contradictory config
   shapes or interface assumptions across plans; `tasks_status.md`
   rows updated to match refined scopes.
7. **Scalability review.** For each sub-plan, ask: what breaks first
   as users, documents, and query volume grow 10x and 100x? Check
   for unbounded in-memory state, per-request work that scales with
   corpus size, missing pagination/streaming, lock contention on
   shared stores (e.g. the SQLite write path), unindexed lookups, N+1
   provider/model calls, and caches with no bounds or TTLs. Record
   per-plan findings (bottleneck + at-what-scale + fix shape), and
   refine plans where a cheap structural choice now (indexes, paging,
   bounded queues, async boundaries) avoids a rewrite later. Do not
   turn the monolith into distributed infrastructure — flag what
   would force that step.

## Validation Plan

- Traceability matrix: every original-plan requirement maps to
  exactly one ACCOUNTABLE owner (matrix kept in the review notes
  below). Valid accountable dispositions are: DONE (shipped
  skeleton), per-key ownership pinned by D1-D6, and named deferred
  orphans O1/O2 (explicitly owner=NONE, deferred by consensus,
  blocking nothing). No silent drops allowed.
- Each refined plan re-read end-to-end for the step-5 bar.
- `tasks_status.md` rows match final plan scopes; no dangling links.

- Scalability findings recorded per plan; cheap structural fixes folded into the plans, rewrite-forcing risks flagged, not built.

## Review notes (filled during execution)

### Trace matrix (original-architecture § → exactly one accountable owner)

Legend: T8=crawler-index, T9=query-intelligence, T10=filter-pack,
T11=scoring-pack, T12=rerank-pack, T13=federated-search,
T14=ai-capabilities, T15=observability-cli, T17=db-connectors,
DONE=skeleton/shipped (tasks 1–7), NONE=orphan (see below).

| Orig requirement | Owner | Notes |
|---|---|---|
| §1 modular monolith + provider/plugin extension | DONE | T8/T14 extend the two extension systems; no plan changes the monolith shape |
| §1 providers: OpenSearch / external API / custom index | DONE+T8 | Adapters shipped (incl. `http` type, task 6); T8 owns custom-index ingestion/differentiation |
| §2 principles (plugin arch, provider independence, safety, explainability…) | ALL | Cross-cutting; each plan cites its principle (T10=safety, T11/T12=explainability, T14=AI-optional) |
| §3 Query Processor | T9 | Deterministic stage; T14 adds model-based understanding alongside (seam note S1) |
| §3 Provider Manager (selection, timeouts, errors) | T13 | Single owner of routing incl. engine delegation; single/fallback semantics regression-guarded |
| §3 Normalizer + query-time dedup | DONE | T8 ingest dedup reconciled with, not replacing, query-time `deduplicator.py` |
| §3/§9B mandatory safety + privacy exclusion | T10 | Sole excluder owner; fail-closed; T14 advisory-only (seam note S2) |
| §3/§9C trust + relevance scoring | T11 | Named `signals` namespace owner; `recency_score` backfill; baseline migration |
| §3/§9D fairness/perspective rerank | T12 | Signal-based rerank via ranker composite; credibility-weighted, never equal-weight |
| §3/§9E enrichment (credibility, warnings, attributes, explanations) | T11+T12 split | T11: trust warnings/reasons; T12: product/hotel/article attributes; T14: AI summaries (seam note S3) |
| §3 result assembly + explanation | DONE+T11/T12 | Assembly shipped; reasons extended by scorers/rerankers (≤1 reason/result each) |
| §4 Recommended Project Structure | DONE | Target layout shipped by skeleton; modules created only when needed |
| §5 SearchRequest.filters keys | T10 owns schema | T10 owns validation/unknown-key rejection; consumer keys are exactly T11 `prefer_domains`/`prefer_topics` (any `prefer_*` passes through) + T12 `max_price`/`location`/`facilities` (seam S4/D4 — note corrected below) |
| §5 provider preferences | T13 | New frozen `ProviderPrefs` dataclass + API field; unknown names → `ConfigError` → 500 `config_error`, malformed shapes → 422 (see Consistency) |
| §5 SearchContext extensions | Per-owner keys | `signals`{trust,freshness,preference,relevance}→T11, {diversity,perspective,commerce}→T12, `story_groups`→T12, AI metadata keys (`ai_summary`/`ai_perspectives`/`ai_model`)→T14, diagnostics `provider_result_counts`/`provider_costs_estimated`→T13, `request_id`→T15; no key has two writers (see D6) |
| §6 provider interface / registry / health / timeouts | DONE | T13 reuses `timeout_s`/`run_bounded`; no new provider in any plan |
| §7 providers.yaml + secret resolution | DONE+T13 | T13 adds `federated` strategy; secret-leak suite extended by T14/T15 |
| §8 Strategy A single + B fallback | DONE | Regression-guarded by T13; semantics untouched |
| §8 Strategy C federated + RRF | T13 | Parallel fan-out, RRF `fusion.py`, merged cap M=200, eval harness |
| §9A query normalization/intent/expansion | T9 | Rule-based, term-cap, provenance; mode suggestion never overrides explicit mode |
| §9A semantic query understanding | T14 | Via `embed`/`classify`; T9 keeps hooks optional+off (seam note S1) |
| §9B child-safe/privacy/block/allow/user rules | T10 | `safety-filter` single blocklist owner extended in place; allowlist default-deny |
| §9C scoring signals | T11 | trust/freshness/preference + `relevance` migration; scorers never exclude |
| §9D rerank profiles (news/shopping/travel/diversity) | T12 | Constraint ordering + `keyword_boost` fallback; demote-never-exclude |
| §9F AI classification/summary/perspective/trust/privacy | T14 | Fake default, one live `local` client, top-K cap, enforced timeouts |
| §9F semantic retrieval / embedding index / vector search | NONE (orphan O1) | Every plan non-goals it (T14→retrieval, T9/T12→AI); deferred, needs a future owner |
| §10 pipeline stages 1–10 + plugin declarations | DONE+T9/T10/T11/T12/T13 | Engine/pipeline shipped; each plan declares stage + mandatory/optional per existing pattern |
| §10 mandatory-fail-closed / optional-isolation | DONE | Pattern in `pipeline.py`; T10/T11/T12/T14 follow it with `simulate_outage` hooks |
| §11 ranking profiles per mode + composite | DONE+T11/T12 | Profiles shipped; T11 adds signal weights, T12 adds rerank weights (sequential edits, T11→T12) |
| §11 gender-neutral evidence-aware ranking | T12 | Credibility-weighted perspective mix; distrusted-outlier test |
| §12 API (validation, modes, prefs, pagination, errors) | DONE+T13 | Prefs added by T13; filters consumed by T10/T11/T12; `/metrics` by T15 |
| §13 repository interface + swappable backends | T17 | `storage/base.py` + SQLite/MySQL/Postgres; SQLite default; lazy driver imports |
| §13 crawl/index/query separation, updates, dedup, reindex, hashes | T8 | Frontier+crawler, hash-column migration, URL/hash indexes, chunked commits |
| §13 OpenSearch as optional storage backend | NONE (orphan O2) | No plan owns it (T17 covers sqlite/mysql/postgres); deferred by consensus |
| §13 no distributed DB | ALL | Respected; rewrite-forcing risks flagged, not built |
| §14 provider/plugin configurability + normalization | DONE | Regression-guarded by every plan's full-suite validation step |
| §14 safety across providers / no bypass via failure | T10 | Fail-closed tests per filter incl. outage simulation |
| §14 optional failures don't crash | DONE | Each plan keeps optional plugins optional; T14 timeout→isolation |
| §14 ranking configurable | T11/T12 | Weights in profiles + `ranking.yaml` with loud validation |
| §14 cross-provider dedup | T13 | Fuse→dedupe order before scoring pipeline |
| §14 keys never exposed | DONE+T14/T15/T17 | Secret-leak suite extended to models/logs/metrics/DB URLs |
| §14 repeatable corpus eval | T13 | Offline single-vs-federated quality/latency/cost harness |
| §14 latency/failure/execution observability | T15 | Logs + `/metrics` built on `SearchContext.diagnostics()` only |
| §15 Phase 1 (Working Search Core) | DONE | Shipped by skeleton task 1 |
| §15 Phase 2 (Provider Abstraction) | DONE | Shipped by skeleton task 1 |
| §15 Phase 3 (Plugin Pipeline) | DONE | Shipped by skeleton task 1 |
| §15 Phase 4 (API done; logs/metrics/CLI) | T15 | Closes Phase 4; CLI reuses `build_engine`, owns surface not indexer |
| §15 Phase 5 (AI understanding, trust/dup, safety/privacy, explanations, news grouping) | T10/T11/T12/T14 | Rules→T10, deterministic signals→T11, grouping→T12, model paths→T14 |
| §15 Phase 6 (multi-provider, dedup, fusion, eval) | T13 | No new providers; `http` stub is the second test backend |
| §16 skeleton DoD (11 items) | DONE | Task 1 verified; all plans regression-guard it |

Orphans: O1 semantic/vector retrieval (circular non-goal — needs a
future plan to own it); O2 OpenSearch storage backend (deferred, no
demand). Scoped-out by consensus (named, not orphans): JS
rendering/sitemap discovery, spelling correction, quota enforcement
(accounting only), distributed tracing, cross-backend query
normalization, bundled model weights.

Double-owners resolved (matrix notes, sub-plans untouched):
D1 `repository.py` delete/chunking/indexes + WAL (T8+T17): T8 lands
first on SQLite and builds `delete`, the `content_hash TEXT` column
(+ idempotent ALTER/backfill, canonical `sha256(content)`), URL/hash
indexes, and chunked commits (500/call); T17 ports — never
re-implements — them into the `storage/base.py` contract and all
backends, and solely owns WAL (`journal_mode=WAL` + `busy_timeout`)
and server-backend connection pools (T8 explicitly flags WAL, does
not build it). Contract name is `delete(doc_id) -> bool` (True iff a
row was removed); a T8 `delete_document` name must alias to it, not
fork it. D2 `ranker.py` / `profiles.py` / `ranking.yaml`
(T11+T12): strictly sequential T11→T12 edits, guarded by unchanged
pre-existing ranking tests + YAML↔builtin parity test. D3
`core/engine.py` + `main.py` + `core/registry.py` shared files
(T9/T11/T12/T13/T14): independent hunks landing in row order with
regression tests — T9 pre-retrieval call site, T13 manager
delegation (single paging owner, `total` = pre-slice merged count),
profile wiring from T11, `BUILTIN_PLUGINS` additions in row order
(T11 scorers → T12 rerankers → T14 AI names). D4
`SearchRequest.filters` validation (T10) vs consumer keys
(T11/T12): T10 owns the schema/validator. CONFIRMED FINAL
RESERVATION (checked against refined T11/T12 scopes): accept-and-
pass-through `prefer_domains`, `prefer_topics` (prefix rule: any
`prefer_*`), `max_price`, `location`, `facilities` without consuming
or rejecting; only keys outside the owned schema and this
reservation are rejected. NOTE: the T10 plan text lists only
`max_price`/location — `facilities` (T12 `facilities: list[str]`
filter, consumed by `commerce-reranker`) was missing there; treat it
as reserved per this note (matrix-notes resolution; sub-plan text
not edited) and T10 step-3 tests must include a `facilities`
pass-through assertion. D5 rule-based intent (T9) vs T14
fallback: when T9's stage is present the T14 fallback defers to
T9's signals; T14's own rules run only when T9 is absent — never
two competing intent labels. D6 diagnostics whitelist
(`SearchContext.diagnostics()` + `api/schemas.py`
`DiagnosticsSchema`, touched by T9/T13/T15): no plan owns the whole
extension; each diagnostic key has exactly one writer and file
edits land sequentially in row order — T9 `query` provenance entry
(engine-level; API passthrough is a required `api/schemas.py` touch
owned by T9), T13 `provider_result_counts` /
`provider_costs_estimated`, T15 `request_id` (both whitelists move
together or the id is silently dropped). T10 removed-counts own no
diagnostics key in v1: they stay in `context.metadata`
(`safety-filter-removed` shape) as in-process evidence (T10 step 4
already states this); API surfacing of those counts is deferred,
not double-owned.

Seam verdicts (review step 3 — the four required adjacencies checked
against the refined plans; every shared boundary has exactly one
owner): Seam aliases cited in the matrix and sub-plans (each
resolves to exactly one verdict below): S1=query→AI single-intent-label
(D5, detailed below), S2=(c) sole-excluder, S3=(a)/(b) enrichment split,
S4=D4 filter-key reservation, S5=unassigned (no seam was ever
cited as S5 in any plan; the numbering gap is intentional and no
plan may cite S5 without first adding its verdict here),
S6=(d) storage placement,
S7=CLI↔indexer (detailed below), S8=diagnostics→observability
(detailed below).
(a) query→filters→scoring→rerank (T9→T10→T11→T12): PASS. T9 emits a
structured expansion term set (never a concatenated query string —
providers build OR-of-ANDs, since `parse_query` AND-joins) + a
suggested mode from the pinned map (`shopping`/`travel`/`news` 1:1,
`navigational`/`informational`→`default`, `child-safe` never
suggested; explicit `mode` always wins). T10 alone validates/
excludes on `SearchRequest.filters`; T11 alone owns
`metadata["signals"]` keys {trust, freshness, preference, relevance}
+ `recency_score` backfill (SCORING-stage scorers are self-sufficient
via `plugins/_signals.py` — they cannot depend on ENRICHMENT
`TrustEnricher` output because `validate_and_order` rejects
later-stage dependencies; the engine `Ranker` after the pipeline is
the only post-ENRICHMENT consumer). T12 consumes `trust` with the
tested `trusted`-bool fallback and writes disjoint keys {diversity,
perspective, commerce}. Filter-key hand-off is D4/S4 (T10 schema,
T11/T12 consumers).
(b) rerank↔federated (T12↔T13): PASS with pinned decision. T13's
merged cap M=200 (fetch depth `offset+limit` per provider at
`offset=0`, single engine slice `[offset:offset+limit]`,
`total` = pre-slice merged count) feeds T12's top-K=50 story
grouping (K ≤ M, compatible); fusion keys on `dedupe_key`, keeps the
best-ranked contributor, records `metadata["also_from"]`, sets
`final_score` to the fused score with `original_score` untouched —
no contradiction with the separate-scores rule. DECISION (closes
the T12 coordination item): rerankers stay at the EXPLANATION stage
(after ENRICHMENT, before the engine `Ranker`) emitting ranker-
consumed signals with stage-agnostic pure functions; they are NOT
folded into `Ranker` and the engine rank-order is NOT changed in
v1 — RANKING-stage placement is impossible because it cannot read
ENRICHMENT attributes/trust from the same pass. `story_groups`
(`context.metadata` + per-result `story_id`) is owned solely by
T12, stays internal in v1 (T15 does NOT whitelist it; only
per-result `metadata` reaches the API via `SearchResultSchema`).
(c) filters↔AI-advisory (T10↔T14): PASS. T10 is the sole excluder
(all FILTERING filters `mandatory=True`, fail-closed); T14's
classification plugin is `mandatory=False`, FILTERING-stage,
advisory-only (labels in `result.metadata`/`result.warnings`, never
removes hits, never mutates scores/membership — position-free within
FILTERING because it is data-only and never calls `SafetyFilter`).
T14 step 3 proves blocked hits stay excluded with the model
disabled, enabled-returning-“safe”, and raising mid-query.
(d) storage↔crawler↔custom-index (T8↔T17↔provider): PASS with
pinned placement. Top-level `storage:` section adopted (T17
preference): `storage.backend` (default `sqlite`) is the single
selection switch and `CustomIndexProvider` reads it; the
provider-entry `db_path` becomes the SQLite backend's field
(back-compat when no `storage:` section exists). T8 owns crawler +
indexer entry point + SQLite-first repository primitives; T17 owns
`storage/base.py` + MySQL/Postgres connectors. Port order is D1.
Consistency gap named, not blocking: T8's `get_hash`/`hash_exists`
helpers are NOT in T17's v1 base contract (`add_document`,
`add_documents`, `delete`, `get`, `search`, `count`, `close`), so
hash dedup is a SQLite/indexer capability until a future port adds
hash methods to the contract — no plan promises cross-backend hash
dedup in v1. S1 query→AI: single intent label per D5; the one
pre-retrieval hook is T9's engine call site — T14 v1 labels
intent/entities post-retrieval only and consumes that hook only if
it has landed (one hook, never two); the manager-primary vs
per-plugin `model:` injection seam and the `models:` source (embed
in `default.yaml` vs second `models.yaml` file) are pre-code
decisions owned by T14 step 1 and recorded there before coding —
implementers must not re-decide, and T9's dormant
`embed`/`classify` hooks (optional, off) align to that choice. S7
CLI↔indexer: T15 owns the CLI surface (`src/openlens/cli.py`,
`main(argv) -> int`); `index` subcommands delegate to T8's indexer
entry point and exit nonzero explicitly until it exists; `--provider`
exists only once T13 `provider_prefs` has landed (never
parsed-and-ignored). S8 diagnostics→observability: T15's registry is
fed from exactly one point (API route reading
`SearchResponse.diagnostics` after `engine.search`) with a fixed
label set (provider, plugin, stage, status) — the T13 free-form
diagnostics keys never become label names/values (fuzz-tested).

- Refinements applied per plan (cross-checked against the refined
  sub-plan texts in this synthesis; sub-plans themselves untouched):
  T8 crawler-index: `httpx`-bounded polite crawler (robots fetched
  once per host, streaming max-bytes, conditional-GET ETag/Last-
  Modified) + `content_hash TEXT` column with idempotent ALTER/
  backfill over `sha256(content.encode("utf-8"))`, shared URL
  normalization with `dedupe_key`, UNIQUE URL + hash indexes,
  chunked commits (one `add_documents` call per 500-doc chunk),
  preserved-fields = stable `id` + verbatim `title`/`content` +
  stored `trusted`/`recency_score`; WAL/off-process writer flagged
  to T17/synthesis. T9 query-intelligence: standalone
  `QueryProcessor` via `SearchEngine.configure`/`build_engine` (NOT
  a pipeline plugin — `Pipeline` runs post-retrieval), NFKC/casefold
  normalizer distinct from the result `normalizer.py`, pinned
  intent→mode map, structured expansion with per-term provenance
  ids + 8-term cap, stage-enforced input bound (API 1000-char cap is
  wire-only), query-provenance diagnostics entry owned here (D6);
  unbounded query cache explicitly rejected. T10 filter-pack:
  snippet/title/url-only scanning (no `content` field exists), single
  `safety-filter` blocklist owner extended in place with
  `urlparse().hostname` parsing (not substring, not `.netloc`),
  config-pinned PII patterns (no invented address grammar),
  redaction never writes score fields, literal-escaped per-request
  terms, mandated FILTERING order via `dependencies` asserted on
  `plugin_executions`, raise sites pinned (`ConfigError` at
  `configure` → 500 `config_error`; per-request `PluginError` → 500
  `plugin_error`); 422 mapping for per-request validation belongs
  to the API owner (see Consistency) — flagged, not assumed. T11
  scoring-pack: `ranking.yaml` unwired premise corrected (live
  `get_profile` reads builtins only) → `profiles` injection +
  YAML↔builtin parity test, `keywords` element/length validation +
  50-cap, freshness keys pinned (`published_at`/`published`/`date`,
  `domain_age_days`), counter-based relevance migration preserving
  term multiplicities, weighted defaults pinned, `frozenset` tier
  lookups, one reason/result. T12 rerank-pack: RANKING-before-
  ENRICHMENT impossibility corrected → EXPLANATION-stage rerankers
  with stage-agnostic pure functions (see seam (b) decision),
  diversity as soft `1/(1+count)` signal (never a hard cap/exclusion),
  grouping pinned (bigram index, Jaccard ≥ 0.5, same-date rule,
  block cap 25, K=50), commerce signal = satisfied-constraint
  fraction with currency-mismatch → neutral, corpus claims replaced
  with hand-built dated/tiered arrangements, weights pinned
  (diversity 0.25 all modes, perspective 1.0 `news`, commerce 1.5
  `shopping`/`travel`). T13 federated-search: engine has no timeout
  today → delegation puts the manager bound on the live path
  (`SearchEngine(providers=[...])` constructor kept, adapting to
  single/fallback; `build_engine` switches to the manager path),
  frozen `ProviderPrefs` dataclass (no bare `dict` in core),
  phantom normalize step removed (RRF over already-normalized
  results keyed by `dedupe_key` with `also_from`), single paging
  ownership (managers query `offset=0`, engine slices once, M=200),
  `_invoke` extraction + executor cap 16 + overall deadline,
  `cost_per_call` validation/storage pins, eval with test-local fake
  providers (20× reps after 3 warmups; p50 ≤ 3× single threshold,
  never exact ms). T14 ai-capabilities: reality baseline pins
  optional-manager boot (zero-enabled ≠ `from_config` success),
  `models.yaml` is NOT loaded by `main` today → `models:` source is
  a step-1 gate, QUERY_PARSING is post-retrieval today → v1 is
  labeling-only, `enrich_top_k` (default 5, [0,20]) caps summary +
  classification, one shared model-call helper with enforced
  `timeout_s` (`httpx` native timeout, mirroring
  `providers/http.py`), one live `local` client (claude/muse stay
  `UNAVAILABLE` stubs), fake output limits respected (120-char
  completions, `max_chars` truncation not re-applied). T15
  observability-cli: diagnostics 4-key whitelist reality (request-id
  extends BOTH `diagnostics()` and `DiagnosticsSchema` + CORS
  `X-Request-Id`), single metrics feed point (API route; feeding
  from both engine and manager would double-count), fixed-label
  registry with threaded test, `src/openlens/cli.py`
  (`argparse`, `main(argv) -> int`), `config validate` via tmp_path
  malformed-config fixture (no committed bad config), `index`
  exits nonzero until T8 lands (S7), `--provider` gated on T13.
  T17 db-connectors: FTS table shape / `configure` connection-leak /
  discarded repository-`total` / seed-on-configure realities
  pinned, `delete(doc_id) -> bool` + lossy per-backend query
  translation (`MATCH` / `MATCH ... AGAINST` / `to_tsquery`) in the
  `storage/base.py` (ABC) contract, lifecycle `close()` on
  reconfigure, WAL + `busy_timeout` + pooled connections folded in,
  chunk bound (500) shared via the interface, lazy driver imports +
  optional extras + CI service containers requested from the
  contracts owner (outside this file list).
- Consistency verdict (review step 6): PASS with the pinned
  exceptions already recorded above — no other contradictions
  found. Naming is uniform (`content_hash`, `dedupe_key`,
  `signals`, `story_groups`, `also_from`/`rrf_score`, `provider_prefs`,
  `storage.backend`, `enrich_top_k`); config shapes are additive and
  non-overlapping (`indexing:`, `query:`, plugin `{name, config}`,
  `reputation:{trusted,distrusted}`, ranking profiles/YAML,
  providers strategy `federated_timeout_s` + per-provider
  `cost_per_call`, `logging.level`, `metrics.enabled`,
  `storage.backend`) with dataclasses in core and Pydantic only in
  `api/schemas.py`, matching the shared context. Error-shape split
  is consistent across T10/T13: unknown names/keys raised as
  `ConfigError`/`PluginError` surface as 500 (`config_error`/
  `plugin_error`); only Pydantic shape failures are 422. No plan
  owns an `errors.py` change to map unknown names to 422 — that is
  a future API-owner decision, NOT an orphan (original §12
  consistent errors + applied-filter reporting are owned by DONE +
  the plans above; the 500 split is pinned and tested in T10/T13).
  Engine result cap for direct (non-API) callers is owned by T13
  (fetch depth `offset+limit` + merged cap M=200 + single slice);
  T10's flagged engine-cap item is therefore closed — no separate
  engine cap is planned. Two deliberate tensions resolved: (i) T11's
  non-goal “advisory model signals arrive later through `signals`”
  does NOT describe v1 — T14 writes `result.metadata`/`warnings`
  only and never touches `signals`/scores/membership in v1; the
  signals namespace remains T11/T12-owned. (ii) T14's `models:`
  source + injection seam are step-1 pre-code gates (single seam —
  shipping both manager-primary and per-plugin `model:` is a
  defect), not inconsistencies; synthesis records the gate, the
  implementer records the choice in the T14 file before coding.
- Order verdict (review step 4): NO REORDER — order changes: NONE.
  Checked every forward edge: T9→T10 (filter stage sees normalized
  query/filters; no T9 output needed to validate T10 fixtures),
  T10→T11→T12 (D4 reservation confirmed above lets T10 validate
  pass-through with literal keys before consumers land), T11→T12
  (`trust` fallback to `trusted` bool when T11 is absent),
  T12→T13 validated independently (T12 grouping on hand-built hits;
  T13 M=200 only bounds its input, and T13's own tests use fakes —
  neither needs the other to validate), T13→T14 (independent;
  T14's exclusion proofs need only T10), T9/T13→T15 (T15 reads
  diagnostics tolerantly; `index`/`--provider` gate explicitly on
  T8/T13 rather than requiring them), T8→T17 (D1 port order),
  T15→T17 (independent; T17 is last in row order despite number 17 —
  16 is this review task and imposes no dependency). No plan
  requires a later-numbered plan's output to validate; every
  cross-plan consume has a tested absent-owner fallback (T12
  `trusted` bool, T14 rule fallback + post-retrieval labeling, T15
  nonzero-exit / flag-absent gates, T17 back-compat default
  `sqlite`). `tasks_status.md` rows 8–15 and 17 notes match the
  refined scopes; row order left as-is.

## Non-goals

- Implementing anything. Running any workflow. Changing task
  numbering or the timestamp convention.

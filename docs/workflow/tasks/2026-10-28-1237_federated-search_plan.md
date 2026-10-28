# Federated Search Plan

## Goal

Query multiple providers per request under a new `federated` strategy
with parallel bounded fan-out and rank-based fusion (Strategy C, §8),
per-query provider preferences, and per-provider cost/latency
accounting — so that a federated query over two seeded fake providers
returns RRF-merged hits with provider attribution, killing one
provider still returns the survivor's hits with the failure recorded
in diagnostics, and the eval harness prints a quality/latency/cost
table over the fixture corpus; covered by
`tests/unit/test_federated.py` plus
`tests/integration/test_federated_eval.py`.

## Reality baseline (verified against the tree)

- Two routing implementations exist today and the engine uses only
  one: `ProviderManager.search` (`providers/manager.py`: single/
  fallback strategies, per-provider thread + `timeout_s` bound,
  latency recording) vs `SearchEngine._search_with_fallback`
  (`core/engine.py`: sequential provider list, own latency
  recording, **no timeout bound at all** — it calls
  `provider.search(request)` directly). `build_engine`
  (`src/openlens/main.py`) builds a manager only to extract the
  primary/fallback instances, then discards it; the engine never
  calls the manager, so the shipped live path has no provider
  timeout enforcement and a hung provider stalls the request
  indefinitely. Decision for this task: the manager owns the
  `federated` strategy (`manager.search` fans out); the engine
  delegates to the manager for provider selection instead of its
  private list loop (single-owner routing; the engine keeps
  pipeline + ranker + response assembly) — delegation is also what
  puts the existing timeout bound onto the live path. Interface
  pin: `SearchEngine` gains an optional manager-backed constructor
  path (`SearchEngine.from_manager(manager, plugins=...)` or an
  optional `manager=` argument) while the existing
  `SearchEngine(providers=[...])` list constructor keeps working
  for shipped tests, internally adapting the list to single/fallback
  manager semantics. `build_engine` switches to the manager path.
  Existing single/fallback tests must pass unchanged after the
  delegation, plus a new engine-level timeout test (below).
- `SUPPORTED_STRATEGIES` is `("single", "fallback")`; `"federated"`
  raises `ConfigError` today. Adding it touches `manager.py` +
  `configs/providers.yaml` docs + startup-validation tests.
- `SearchRequest` (`core/interfaces.py`) has no provider-preferences
  field (original §5 lists one; the dataclass has
  query/mode/limit/offset/filters), and `SearchRequestSchema`
  (`api/schemas.py`) has none either. This task adds a typed frozen
  dataclass `ProviderPrefs(include: tuple[str, ...] = (),
  exclude: tuple[str, ...] = (), primary: str | None = None)` in
  `core/interfaces.py` (core stays Pydantic-free; a bare `dict`
  field is not used), a `provider_prefs: ProviderPrefs` field with
  a default factory appended after `filters` on `SearchRequest`
  (existing positional construction stays compatible), plus the
  matching optional nested `ProviderPrefsSchema` API field with
  routes pass-through (`api/routes.py` builds the core dataclass).
  Unknown provider names are rejected loudly — never silently
  dropped. Status-code pin (corrects the obvious assumption):
  `src/openlens/api/errors.py` maps `ConfigError` → **500**
  `config_error` and only Pydantic `RequestValidationError` → 422,
  so manager-raised name errors surface as 500 `config_error`,
  while malformed prefs shapes (wrong types) fail Pydantic → 422.
  Tests assert exactly that split; returning 422 for unknown names
  would need an `errors.py` change and is left to synthesis.
- `retrieval/fusion.py` does not exist (`retrieval/` has
  query_parser/normalizer/deduplicator) — new module confirmed.
  Dedup exists (URL-normalized, keeps first:
  `retrieval/deduplicator.py:dedupe_key`). There is no separate
  normalize step to add: every provider already returns normalized
  `SearchResult`s (`CustomIndexProvider` via `normalize_many` +
  its own `deduplicate`, HTTP/Bing via `parse_payload`). Federated
  order is therefore: per-provider search (already normalized) →
  RRF fuse keyed by `dedupe_key` (fusion itself merges
  cross-provider duplicates; no second dedupe pass) → rank pipeline
  → engine slice `[offset:offset+limit]`. Paging ownership (today
  the engine never slices — providers honor `limit`/`offset` and
  the engine returns everything with `total = len(results)`):
  the manager becomes the single paging owner — every provider is
  queried with a copied request at `offset=0`,
  `limit=offset+limit` (fetch depth), the merged window is capped
  at M, and the engine slices once after ranking with
  `total` = pre-slice merged count. Passing the original `offset`
  to providers and slicing again in the engine would double-skip;
  tests pin the single-slice semantics for all strategies.
- Diagnostics (`SearchContext.diagnostics`) already carry
  `provider_latencies_ms`, `plugin_executions`, `errors`,
  `fallback_used`. Cost accounting adds
  `provider_result_counts: dict[str, int]` and
  `provider_costs_estimated: dict[str, float]` (from per-provider
  `cost_per_call` config, default 0.0), plus matching
  `DiagnosticsSchema` fields (`api/schemas.py`, Pydantic side).
- Timeout machinery exists in the manager/base layer and is reused,
  not reinvented — with two corrections: (a) it is not on the live
  engine path today (see the first bullet); delegation puts it
  there. (b) `timeout_s` comes from `BaseSearchProvider` (ceiling
  `MAX_TIMEOUT_S = 300`, `run_bounded` helper, manager thread
  enforcement), but the shipped primary `CustomIndexProvider`
  extends `SearchProvider` directly and has no `timeout_s` — the
  manager's `getattr(..., DEFAULT_TIMEOUT_S)` fallback is what
  bounds it, via an abandoned daemon thread (the underlying SQLite
  call is not cancelled and holds the repository lock until it
  returns; acceptable for local FTS, documented in Scalability).
  All-fail behavior for federated: raise one `ProviderError`
  (code `unavailable`) whose message names every attempted
  provider, with each failure also in `context.errors` — this
  extends, not matches, the current single/fallback "raise the
  last error" semantics, and tests pin the new message.
- Eval grounding exists: `tests/fixtures/corpus.py` (8 fixed docs,
  4 requests, `seed_provider`) and `tests/integration/`. Caveat:
  `seed_provider` only works on providers with `index_documents`
  (today just `CustomIndexProvider`, whose `name` is the constant
  `"custom_index"` for every instance — two instances are
  indistinguishable in result attribution). The harness therefore
  defines test-local fake providers (distinct names, in-memory
  token-overlap search over the fixture documents via
  `normalize_many`) instead of seeded custom-index instances or
  the `http` stub (which would need a mock transport). Offline
  only, no network/keys.

## Scope (all connected: one multi-provider retrieval path)

- Manager `federated` strategy: parallel fan-out over enabled
  providers (bounded `ThreadPoolExecutor`, `max_workers` =
  min(selected providers, 16)) with per-provider `timeout_s` plus
  an overall deadline (`federated_timeout_s` in the providers
  strategy section — not a per-provider entry — default = max
  per-provider bound + 1s, validated 0 < value ≤ `MAX_TIMEOUT_S`).
  Implementation pin: extract the thread-free `_invoke(name,
  request)` call from `_search_one`; single/fallback keep the
  existing thread-bound wrapper, federated submits `_invoke` to
  the executor and collects with the overall deadline
  (`shutdown(wait=False)` on expiry; late threads are daemons and
  are recorded as timeouts, never awaited). Partial failure
  degrades (survivor results + per-provider errors recorded),
  never silently drops a provider; empty from all providers is
  success with zero hits, distinct from all-failed.
- `retrieval/fusion.py`: reciprocal rank fusion over per-provider
  ranks (never raw cross-provider scores), keyed by
  `deduplicator.dedupe_key` so the same URL from two providers
  merges into one hit (kept object: the contributor with the best
  rank; other provider names recorded in
  `metadata["also_from"]`); `k` (default 60) and per-provider
  fusion weights configurable; merged hit carries
  `metadata["rrf_score"]` and `final_score` initialized to the
  fused score (`original_score` untouched); deterministic
  tie-break (score desc, provider name asc, doc id asc); pure
  function `fuse(ranked_lists, k, weights) -> merged`.
- Provider preferences end-to-end (API field → `SearchRequest` →
  manager selection): `include` restricts, `exclude` removes,
  `primary` boosts via a fusion weight multiplier
  (`federated_primary_boost`, default ×1.5, applied only when
  `primary` is set). Preferences intersected with enabled
  providers; empty selection is a request error (`ConfigError`),
  not silent fallback to all.
- Cost/latency accounting: per-provider latency (existing),
  result counts, estimated call cost in diagnostics + API schema.
  `cost_per_call` is read and validated by
  `ProviderManager.from_config` (number ≥ 0, bools rejected →
  `ConfigError`; popped before provider construction so providers
  never receive it; default 0.0). Accounting only — no quota
  enforcement. New diagnostics keys are also added to
  `SearchContext` storage (today `diagnostics()` has no home for
  them) and to `DiagnosticsSchema.from_core`, which currently
  copies only the four existing keys and would silently drop them.
- Evaluation harness (`tests/integration/test_federated_eval.py` +
  helper, offline): fixed query set over the fixture corpus
  comparing single vs federated on expected-doc recall, p50/p99
  latency shape (keys present, federated p50 within a documented
  multiple of single — not exact milliseconds), and estimated
  cost; prints a quality/latency/cost table.

## Steps

1. RRF fusion + unit tests (rank-only merging on hand-built lists,
   same-URL merge across providers with `also_from`, weight config,
   tie-break determinism, empty-list handling).
   Accept: pure function, no I/O, exact expected orders asserted.
2. Manager federated strategy + engine delegation + paging ownership
   + tests (parallel fan-out via `_invoke`, per-provider timeout,
   overall deadline, partial failure degrades with errors recorded,
   all-fail raises naming all providers, single/fallback regression
   tests green unchanged; engine-level tests: slow provider through
   `SearchEngine` times out within bound, offset paging through the
   engine returns the correct page exactly once with pre-slice
   `total`, for single and federated alike).
3. Provider preferences end-to-end + tests (include/exclude/primary
   honored with the ×1.5 boost changing fused order; unknown name →
   `ConfigError` in core and 500 `config_error` envelope via API,
   malformed shape → 422; empty selection → request error).
4. Accounting + eval harness + baseline. Accept: diagnostics keys
   (`provider_result_counts`, `provider_costs_estimated`) present
   end-to-end through the API schema with invalid `cost_per_call`
   rejected at startup; eval test (pinned repetitions: each of the
   4 fixture requests run 20× per strategy after 3 warmups) asserts
   recall ≥ single-provider recall on the fixture set and the cost
   table has one row per strategy; flaky latency assertions
   forbidden (keys present and federated p50 ≤ 3× single p50 only —
   a documented threshold, never exact milliseconds).

## Validation Plan

- `uv run pytest tests/unit/test_federated.py tests/integration/test_federated_eval.py -q` green.
- `uv run pytest tests -q` green; `uv run ruff check src tests` and
  `uv run ruff format --check src tests` clean; `uv run mypy src` clean.
- Manual: federated query across the two test-local fake providers
  merges with `provider` attribution per hit; stopping one provider
  returns the survivor's hits with `errors` naming the dead
  provider; eval run prints the quality/latency/cost table.

## Scalability notes (bottleneck + at-what-scale + fix shape)

- Sequential fan-out would make latency = Σ providers (3 providers
  × 5s timeouts = 15s worst case at 1x) — and the shipped engine
  path today is worse: sequential with no timeout at all. Fix folded
  into this plan: manager delegation + parallel fan-out with
  per-provider timeout + overall deadline.
- Thread-per-provider-per-request: bounded by provider count per
  request (executor capped at 16 workers), threads short-lived
  daemons as today — holds to ~100x QPS on a monolith. Two flagged
  limits, not built: (a) timed-out provider calls are abandoned, not
  cancelled, so sustained provider hangs accumulate daemon threads
  until the calls return on their own; (b) the true rewrite-forcing
  step is a cancellable async provider interface. Both are named
  here so the monolith's ceiling is explicit.
- SQLite lock contention (first shared-store bottleneck): the
  custom-index store (`storage/repository.py`) is one connection
  guarded by one `threading.Lock`, held across each search's
  COUNT + SELECT. Distinct federated providers use distinct stores,
  but concurrent API requests against the same provider serialize
  on that lock — queueing grows linearly and breaks first at ~10x
  concurrent QPS, well before RRF or scoring costs matter. Fix
  shape belongs to the storage plans (WAL + per-thread/read-pool
  connections, T8/T17 coordination); this plan adds no new locking
  and must not hold any manager lock across provider calls.
- Candidate explosion (N providers × fetch depth each) into
  superlinear downstream work (scoring passes, news grouping): fix
  folded in — each provider is queried at `offset=0` with fetch
  depth `offset+limit`, the merged list is capped at M (default
  200, configurable) before the scoring pipeline, final slice
  `[offset:offset+limit]` after ranking; offset semantics
  documented in `fusion.py` docstring. Per-request downstream work
  is then bounded by M at any corpus size or provider count.
- RRF itself is O(total candidates) with dict accumulators — fine
  to 100x; weights lookup O(1); no unbounded caches anywhere in
  this path (no caches added: identical queries re-fan-out every
  time, which is the deliberate freshness-over-cost trade; a bounded
  result cache with TTL is a future flagged option, not built).
  Federated cost is exactly N provider calls per request by design
  (visible via the new cost accounting); there are no per-result
  provider/model calls (no N+1).
- Eval harness runtime at 100x corpus: bounded fixed query set with
  pinned repetitions (step 4) + per-query timeout; harness fails
  loudly on timeout rather than hanging CI (folded in).

## Non-goals

- Learned fusion/rerank models (deterministic RRF baseline only).
- Quota enforcement or budget blocking (accounting only; enforcement
  is a later task if ever).
- New providers (uses existing custom/http/OpenSearch adapters;
  tests use test-local fakes — the `http` adapter would need a mock
  transport and adds no federated coverage beyond them).
- Async provider interface or distributed fan-out (monolith only;
  the async rewrite is flagged above, not built).
- Changing single/fallback semantics (regression-guarded, untouched
  behavior; the paging-ownership change in Reality baseline alters
  only where slicing happens, with identical pages and totals).
- Coordination left for synthesis (not this task): the
  `SearchEngine` manager-constructor shape and `total` semantics
  are shared with the API surface (`api/routes.py` documents the
  engine as owning slicing and the pre-pagination `total` — this
  plan makes that true); 422-vs-500 for unknown preference names
  (needs an `errors.py` decision); SQLite lock/WAL work owned by
  the storage plans (T8/T17); the new free-form diagnostics keys
  consumed tolerantly by observability (T15, seam S8).

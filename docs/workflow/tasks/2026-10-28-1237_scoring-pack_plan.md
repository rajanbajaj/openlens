# Scoring Signals Pack Plan

## Goal

Ship three deterministic SCORING-stage scorers (trust, freshness,
preference) that emit named signals into `result.metadata["signals"]`
without touching `final_score`, extend `TrustEnricher` with graded
reputation evidence, and wire the new signals into the `Ranker`
composite with per-mode weights — so that, under the pinned test
arrangement in step 6 (in-test documents carrying spread publication
dates plus a configured reputation tier list; the shipped fixture
corpus alone cannot show this — see Reality baseline), the same query
under the `default` vs `news` profile returns different top-3
orderings with reasons citing the `trust`/`freshness` signals, and
every new behavior is covered by `tests/unit/test_scoring_pack.py`.
Falsifiable check: step 6 fails if either signal is neutral-uniform,
if the weights do not differ per mode, or if cited reasons are absent.

## Reality baseline (verified against the tree)

- `TrustEnricher` (`src/openlens/plugins/trust_enricher.py`, ENRICHMENT
  stage, optional) already sets `metadata["trusted"]` (bool) /
  `metadata["trust_domain"]` and warns on untrusted domains from config
  `trusted_domains` (list). This task extends it; it does not build a
  second trust path. The enricher owns warnings; the new trust scorer
  owns the numeric signal. No double warnings.
- `Ranker.composite` (`src/openlens/ranking/ranker.py`) already reads
  `metadata["trusted"]` and `metadata["recency_score"]` (0..1) — but
  nothing in `src/` writes `recency_score` today (grep-confirmed). The
  freshness scorer's consumer exists and its input is dead; this task
  connects them.
- `RankingProfile` / `profile_from_dict` (`src/openlens/ranking/profiles.py`)
  accept a closed key set; unknown profile keys raise `ConfigError`.
  New per-mode weights therefore require changing `profiles.py` +
  `ranker.py` composite together, with startup-validation tests for
  bad values. Two gaps the implementer must close, not assume:
  (a) `configs/ranking.yaml` is **not wired at runtime** —
  `get_profile` reads only `BUILTIN_PROFILES`, `load_profiles` is
  never called by `Ranker`/`SearchEngine`/`build_engine`, so editing
  the YAML alone changes nothing (only
  `test_ranking_yaml_loads_five_profiles` reads it); step 5 wires it.
  (b) `profile_from_dict` does `tuple(data.get("keywords", ()))`
  with no element/length validation — a string value silently becomes
  a tuple of characters; step 5 validates it.
- No publication-date or domain-age field exists anywhere today:
  fixture corpus metadata is only `{"category": ...}`, seed docs only
  `{"topic": ...}`, Bing payloads only `{"mode": ...}`. The freshness
  date source and the domain-age source are therefore pinned in Scope
  (`published_at`/`published`/`date`, `domain_age_days`); without that
  pin every freshness score is neutral and the Goal cannot trigger.
- `BaselineRelevanceScorer` overwrites `final_score`
  (`final_score = original_score + bonus`), contradicting §9C named
  signals. New scorers use the `metadata["signals"]` namespace; the
  baseline scorer is migrated in this task to emit a `relevance`
  signal with identical math (no ranking change on existing tests).
- Stage-ordering constraint: `PipelineStage.ordered()` is
  QUERY_PARSING, QUERY_REWRITE, RETRIEVAL, NORMALIZATION,
  DEDUPLICATION, FILTERING, **SCORING, RANKING, ENRICHMENT**,
  EXPLANATION, and `validate_and_order`
  (`src/openlens/core/pipeline.py`) rejects a dependency in a later
  stage — so SCORING-stage scorers cannot depend on
  `trust-enricher` (ENRICHMENT) output. Scorers are self-sufficient
  via a shared pure helper (domain extraction, date parsing); no
  cross-stage dependency. (The engine runs its own `Ranker` after
  the whole pipeline, so enrichment output is still visible to
  ranking — that is the only post-ENRICHMENT consumer.)
- `SearchRequest.filters` is a free-form dict passed through
  `api/schemas.py` unvalidated; no preferred-domains/topics convention
  exists. This task defines `prefer_domains: list[str]` and
  `prefer_topics: list[str]` filter keys (the `prefer_` prefix is
  reserved for advisory boosts; filter-pack owns exclusion syntax).
- Plugin instances are long-lived (built once in
  `Engine.configure`); per-request state lives only in
  `SearchContext`/results, never on `self`.

## Scope (all connected: one scoring stage + its trust evidence)

- Trust scorer (`plugins/trust_scorer.py`, SCORING, optional): graded
  `trust` signal 0..1 from config-driven reputation tiers
  (`reputation: {trusted: [...], distrusted: [...]}` → dict lookup),
  HTTPS scheme bonus, domain-age bonus only from
  `metadata["domain_age_days"]` (int ≥ 0; absent/other types → no
  bonus, never a guess). A domain in both tiers is a `ConfigError`
  at `configure`. Missing metadata degrades to neutral 0.5 with a
  reason; never 0. All signal writes clamp to 0..1 in the shared
  helper.
- Freshness scorer (`plugins/freshness_scorer.py`, SCORING, optional):
  reads the first present of `metadata["published_at"]`,
  `metadata["published"]`, `metadata["date"]` (ISO-8601 incl. `Z`,
  or epoch seconds as int/float; unparseable or absent → neutral
  0.5, reason cited), exponential decay with configurable
  half-life (`half_life_days`, default 30, must be > 0), writes
  `freshness` signal and backfills `recency_score` (the key the
  ranker already reads) with the identical value.
- Preference scorer (`plugins/preference_scorer.py`, SCORING,
  optional): reads `prefer_domains`/`prefer_topics` request filters;
  domain match is exact-or-subdomain on the normalized domain,
  topic match is a case-insensitive token match over title+snippet;
  matches yield bounded boosts (signal 0..1). Malformed filter
  values (non-list, non-string entries) are ignored and recorded
  once per request in `context.metadata["preference_warnings"]`
  (asserted on `SearchContext` in tests; not part of the API
  diagnostics payload). No cross-query state.
- Credibility enrichment: extend `TrustEnricher` with tier-aware
  warnings/reasons (e.g. "distrusted domain", "no HTTPS") reusing the
  same reputation config shape as the trust scorer (single source).
  Backward-compatible: the existing `trusted_domains: list[str]`
  config (used by shipped tests) keeps working and maps to the
  `trusted` tier; its lookup also becomes a `frozenset` (today it is
  a list scanned per result). The enricher stays the only warner.
- Shared pure helpers (`plugins/_signals.py`, no I/O): domain
  extraction, date parsing, signal read/write (`signals` dict
  namespace, values clamped 0..1). No Pydantic outside `api/`;
  dataclasses only.
- Plugin registration: add `trust-scorer`, `freshness-scorer`, and
  `preference-scorer` to `BUILTIN_PLUGINS` in
  `src/openlens/core/registry.py` (without this, config-by-name
  cannot resolve them). Shipped disabled in `configs/default.yaml`
  (opt-in entries, `enabled: false`); the baseline scorer stays as
  enabled today. Tests construct scorers directly or enable them
  explicitly — no default-behavior change.
- Ranker composition update: composite adds
  `trust_weight * signals["trust"] + freshness_weight *
  signals["freshness"] + preference_weight * signals["preference"]`
  (missing keys → neutral 0.5 defaults, never `KeyError`); per-mode
  weights in `RankingProfile`, `profile_from_dict`, builtins, and
  `configs/ranking.yaml`. Pinned defaults (chosen so pre-existing
  tests pass unchanged: with no scorer enabled, every hit gets the
  uniform neutral 0.5, a constant offset that cannot reorder):
  `default` trust 0.5 / freshness 0.25 /
  preference 1.0, `news` trust 0.5 / freshness 2.0 / preference 0.0,
  `child-safe` trust 1.0 / freshness 0.0 / preference 0.0,
  `shopping`/`travel` trust 0.25 / freshness 0.0 / preference 1.0.
  Old `trust_boost` (bool) and `recency_boost` terms stay in the
  composite alongside the new signal terms; no term is removed.
- Profile wiring: `Ranker` accepts an injected
  `profiles: dict[str, RankingProfile]` (default: builtins);
  `SearchEngine.configure` accepts an optional `profiles` mapping
  and `build_engine` (`src/openlens/main.py`) loads
  `configs/ranking.yaml` via `load_profiles` when the file exists,
  falling back to builtins when absent. A parity test asserts YAML
  and builtins carry identical weights so the two cannot drift.

## Steps

1. Signal namespace + helpers + tests: `plugins/_signals.py`
   (`get_signal`/`set_signal` with neutral 0.5 defaults and 0..1
   clamping, domain/date parsing per the pinned keys in Scope).
   Accept: unit tests for defaults, malformed dates, IDN/
   case-normalized domains, out-of-range signal clamped.
2. Trust scorer + `TrustEnricher` tier extension + registry entry +
   tests (each tier, HTTPS bonus, `domain_age_days` present/absent,
   missing metadata → neutral, domain in both tiers → `ConfigError`,
   no duplicate warnings between scorer and enricher, reputation
   config validated loudly at startup: `frozenset`/`dict` built in
   `configure`; legacy `trusted_domains` config still accepted).
3. Freshness scorer + registry entry + tests (decay curve exact
   values at half-life multiples, dateless docs neutral with reason,
   each pinned date key + epoch form, `recency_score` backfill equals
   the `freshness` signal).
4. Preference scorer + registry entry + tests (boosts scoped to
   request: same instance, two requests, second shows no leak;
   malformed filters ignored with the note in
   `context.metadata["preference_warnings"]`).
5. Baseline scorer migration to `relevance` signal + ranker
   composition + profiles/config update + profile wiring + ranking
   tests. The migration preserves the exact bonus math including
   duplicate query terms (count multiplicities; do not deduplicate
   the query). Accept: all pre-existing ranking tests pass unchanged,
   new per-mode weight tests pass, unknown profile key still raises
   `ConfigError`, `keywords` given as a string / with non-string
   entries / over the cap raises `ConfigError`, and a tmp-path
   `ranking.yaml` with a changed weight changes live ordering
   (proves the wiring in Scope).
6. Ordering test (the Goal's falsifiable check): hand-built hits (or
   in-test `Document`s in an in-memory `CustomIndexProvider`) with
   spread `published_at` values across half-lives and one
   trusted-tier + one distrusted-tier domain, queried under
   `default` vs `news`: top-3 id orders differ and the moved hits'
   reasons cite `freshness`/`trust`. The shipped
   `tests/fixtures/corpus.py` is used only as a neutral-signal smoke
   check (all signals 0.5, ordering unchanged), not as this test's
   corpus — it has no dates or tiers to differentiate on.

## Validation Plan

- `uv run pytest tests/unit/test_scoring_pack.py -q` green.
- `uv run pytest tests -q` green (no regressions, esp. ranking/contract
  suites); `uv run ruff check src tests` and
  `uv run ruff format --check src tests` clean; `uv run mypy src` clean.
- Manual (step-6 arrangement, not the bare corpus): query
  "running shoes" under `default` vs `news` shows different ordering
  with reasons citing `trust`/`freshness`; a result with no
  dates/metadata keeps a neutral 0.5 signal and a reason saying so.
  (Bare `seed_provider` corpus correctly shows no difference — all
  signals neutral there.)

## Scalability notes (bottleneck + at-what-scale + fix shape)

- Trust list lookup is `domain in <list>` per result today
  (`TrustEnricher._trusted_domains` is a list): O(T×R). Breaks first
  at ~10x (10k-entry list × 100 results ≈ 1M string compares per
  request). Fix folded into this plan: build `frozenset`/`dict`
  tier map once in `configure` for both scorer and enricher;
  per-result lookup O(1); startup test asserts O(1) structure type.
- Ranker keyword counting is `haystack.count(kw)` per keyword per
  result (`ranking/ranker.py`): O(K × text). The haystack is already
  lowered once, but each keyword is re-lowered per result. Folded
  fix: store keywords pre-lowered in `profile_from_dict` and cap
  their count at 50 (`ConfigError` above; builtins use ≤ 5 — a cap
  of 500 would still allow 200 results × 500 keywords × ~700-char
  haystacks ≈ 70M char scans per request at the federated cap).
- Baseline scorer counts per query term with `list.count` over
  title/snippet token lists: O(Q × tokens) per hit. Fine at the
  federated merged cap M=200; folded fix (keeps step-5 math exact):
  build one `Counter` per hit, then O(1) per distinct query term
  with query-term multiplicities preserved.
- Reasons/metadata growth: each plugin appends per-result reasons;
  federated merges multiply candidates and bloat payloads at 100x.
  Folded rule: each scorer appends at most one concise reason per
  result; scorers do a single O(R) pass, no I/O, no network, no cache.
- Date parsing repeated across scorers: parse once in the freshness
  scorer, reuse via `recency_score`/`signals` keys (folded in).
  Domain `urlparse` runs once per scorer/enricher per result
  (2–3× per hit): negligible at M=200; do not cache it in
  `result.metadata` (that dict is serialized to the API). If the
  merged cap is ever lifted toward 100x candidates, fold domain/date
  parsing into one shared SCORING pass — flagged, not built.
- What does not scale with corpus: providers paginate in the store
  (`LIMIT ? OFFSET ?` over the FTS5 index in
  `storage/repository.py`), so scorers only ever see one bounded
  page/window of results. Per-request scoring cost scales with
  result count R, never with corpus size; no unbounded state, caches,
  or per-result provider/model calls are introduced.

## Non-goals

- Learned trust models and AI analysis (AI Capabilities owns §9F;
  advisory model signals arrive later through the same `signals`
  namespace — coordination item, not this task).
- External reputation API integrations (config lists only; no network
  in any scorer — offline test suite stays offline).
- Changing `SearchResult`/`SearchRequest` dataclass shapes (signals
  live in `metadata`, preferences in existing `filters`).
- Exclusion behavior: scorers never remove results; any neutralization
  beyond demotion belongs to filter-pack.
- Learned/domain-age data feeds: no age lookup service and no
  reputation list beyond operator config (see Scope pin).
- Coordination left for synthesis (not this task): the
  `core/registry.py` builtin additions and the `core/engine.py` /
  `main.py` profile wiring are shared files also touched by the
  rerank and federated plans (sequential T11→T12→T13 edits); the
  `signals` namespace contract defined here is consumed by rerank
  (T12) and AI capabilities (T14) with the tested fallbacks named in
  their plans.

# Reranking & Diversity Pack Plan

## Goal

Ship deterministic final-ordering behavior per mode — source-diversity
demotion, perspective-aware news grouping/rerank, shopping/travel
constraint ordering — driven by an attribute enricher, with every
rerank decision expressed as ranker-consumed signals (not private
re-sorts), so that under the pinned test arrangements in steps 3–4
(hand-built hits carrying price/published/perspective metadata; the
shipped fixture corpus has none of these — see Reality baseline) a
news query returns grouped stories with a credibility-weighted
perspective mix, a shopping query with a budget filter ranks
in-budget hits first with cited reasons, and repeated runs are
byte-identical in ordering; covered by
`tests/unit/test_rerank_pack.py`. Falsifiable checks: steps 3–5 each
fail if grouping, credibility weighting, budget demotion, or
determinism is absent.

## Reality baseline (verified against the tree)

- Ordering seam (critical, two parts): (1) `SearchEngine.search`
  (`src/openlens/core/engine.py`) runs `pipeline.run(context)` and
  then `self._ranker.run(context)` — the `Ranker` composite + sort
  runs after every pipeline plugin. A pipeline plugin that re-sorts
  `context.results` therefore has its ordering overwritten by the
  final ranker sort. Consequence: rerankers in this task emit
  ordering signals consumed by the `Ranker` composite (composition
  owned by the ranker, per §9C/§11); they do not privately re-sort.
  An engine change to run rerank after the ranker is explicitly out
  of scope (coordination item for synthesis). (2) Stage order makes
  the obvious home impossible: `PipelineStage.ordered()` puts
  RANKING **before** ENRICHMENT, and `validate_and_order` rejects a
  dependency on a later stage — so a RANKING-stage reranker cannot
  read the attribute enricher's (or `TrustEnricher`'s) output from
  the same pass; both are produced afterwards. Consequence: the
  rerankers below run at the **EXPLANATION** stage (after
  ENRICHMENT, still before the engine's `Ranker`), where enriched
  attributes and trust evidence exist and emitted signals are still
  consumed by the final composite. Their pure signal functions are
  kept stage-agnostic so synthesis can fold them into `Ranker`
  itself without rewriting the math.
- `Ranker` is `mandatory = False`, also registered as builtin
  `"ranker"`, but the engine instantiates its own copy — the pipeline
  must not also include `"ranker"` in production config (the shipped
  `configs/default.yaml` does not; a pipeline `Ranker` would apply
  the composite twice, re-feeding `final_score` as its own relevance
  input and duplicating reasons — an existing unit test does include
  it and only passes because the ordering survives the double-count,
  so do not copy that arrangement). This task keeps the engine-owned
  single rank; new tests never put `"ranker"` in the pipeline.
- Shopping/travel relevance partially exists as `keyword_boost`
  profiles (`ranking/profiles.py`, `configs/ranking.yaml`: "buy/
  price/deal…", "flight/hotel/guide…"). This task upgrades keyword
  heuristics to constraint-based ordering and keeps `keyword_boost`
  as the fallback when attributes are missing (graceful degradation,
  not removal).
- No story-group field exists on `SearchResult`
  (`core/interfaces.py` is the shared shape; this task does not
  extend dataclasses), and no price/published/perspective metadata
  exists in any fixture or provider payload today (corpus metadata
  is only `{"category": ...}`). Grouping output lives in
  `context.metadata["story_groups"]` (group id → member result ids,
  perspective labels) plus per-result `metadata["story_id"]`; the
  convention is documented in the plan's step-3 module docstring.
  Visibility pin: `SearchContext.diagnostics()` does **not** include
  `context.metadata`, and `SearchResponse` carries only
  `diagnostics()` — so `story_groups` is internal pipeline state,
  asserted on `SearchContext` in unit tests and invisible via the
  API until a later plan whitelists it (coordination for T15).
  Per-result `metadata["signals"]`/`metadata["story_id"]`, by
  contrast, do reach the API via `SearchResultSchema.metadata`.
- ENRICHMENT-stage order: the attribute enricher shares the
  ENRICHMENT stage with `trust-enricher`; they are independent
  (stable declaration order, no `dependencies` needed) and the
  attribute enricher never emits trust warnings (no duplication).
- Credibility-weighted perspectives ("fair, not equal", §11) consume
  the scoring-pack graded `trust` signal; when absent (scoring-pack
  not yet enabled) the reranker falls back to the `metadata["trusted"]`
  bool that exists today. Both are readable only because the
  rerankers run at EXPLANATION (post-ENRICHMENT) — see the seam
  above. Task order 11 → 12 already supports this; the fallback is
  tested, not assumed.
- Profile wiring dependency: per-mode weight edits assume the
  scoring-pack (T11) step that wires `configs/ranking.yaml` into the
  live `Ranker` (today `get_profile` reads builtins only). If T11
  has not landed, this task edits builtins + `profile_from_dict` +
  YAML identically and relies on the YAML↔builtin parity test, but
  must not claim YAML edits change live ordering.

## Scope (all connected: one reranking stage + its attribute feed)

- Attribute enrichment (`plugins/attribute_enricher.py`, ENRICHMENT,
  optional): normalizes product/hotel/article metadata into
  documented keys (`price`, `currency`, `location`, `facilities`,
  `published`, `perspective`). Missing attributes degrade to
  documented neutral sentinels; unparseable values keep the raw
  string plus a warning, never raise.
- Source-diversity reranker (`plugins/diversity_reranker.py`,
  EXPLANATION, optional): soft per-domain demotion emitted as a
  `diversity` ordering signal in the shared 0..1 namespace —
  `signal = 1 / (1 + prior_same_domain_count)` counted in incoming
  order (first hit per domain: 1.0, second: 0.5, third: 0.33…), so
  rank-1 is never demoted by diversity alone. A weighted-sum
  composite cannot enforce a hard cap N; dropping hits beyond N
  would be exclusion and is explicitly not done (see Non-goals).
- News grouping + perspective rerank (`plugins/news_reranker.py`,
  EXPLANATION, optional): story grouping over the top-K candidates
  (K=50, configurable) — tokenize titles (shared tokenizer), build
  word bigrams, index bigram → candidates, union-find merge pairs
  sharing ≥ 1 bigram with title-token Jaccard ≥ 0.5 and the same
  normalized `published` date (or both undated); blocks over 25
  candidates are left unmerged (see Scalability). Perspective mix
  weighted by credibility (trust signal or `trusted` fallback) —
  never equal-weight false balance; missing perspective normalizes
  to `"unknown"`. Emits a 0..1 `perspective` ordering signal —
  within each story group, the most credible member of each
  perspective gets its credibility (trust signal, else 1.0/0.5 from
  the `trusted` bool, else 0.5), other members of that perspective
  get half of it, ungrouped hits get neutral 0.5 — plus
  `story_groups` metadata; hits beyond K get no `story_id`.
- Shopping/travel relevance (`plugins/commerce_reranker.py`,
  EXPLANATION, optional): budget (`max_price` filter), location,
  facilities (`facilities: list[str]` filter) constraints from
  filters + normalized attributes → `commerce` ordering signal with
  cited reasons ("within your budget", "matches location"); the
  0..1 `commerce` signal is the satisfied fraction of the requested
  constraints (no constraints requested → neutral 0.5). Price
  compares only when currencies match (or the filter carries
  no currency), mismatched currency → neutral with a reason;
  location match is case-insensitive equality on the normalized
  value. Constraints unsatisfied → demote, never exclude
  (exclusion is filter-pack's job). Requires the filter-pack (T10)
  allowlist to accept `max_price`/`location`/`facilities` keys;
  until T10 lands, tests drive filters at engine level.
- Plugin registration: add `attribute-enricher`,
  `diversity-reranker`, `news-reranker`, and `commerce-reranker` to
  `BUILTIN_PLUGINS` in `src/openlens/core/registry.py`; shipped
  disabled (`enabled: false`) in `configs/default.yaml`.
- Ranking profiles extended per mode (`news`: perspective weight;
  `shopping`/`travel`: commerce weight; all modes: diversity
  weight), composite formula + `profile_from_dict` +
  `configs/ranking.yaml` updated together (sequential T11→T12 edits
  to the shared ranking files). Pinned weights: diversity 0.25 in
  every mode, perspective 1.0 in `news` (0 elsewhere), commerce 1.5
  in `shopping`/`travel` (0 elsewhere) — with no reranker enabled,
  signals are absent and the neutral 0.5 default keeps existing
  orderings unchanged. Explanation reasons cite the rerank signals
  (at most one concise reason per reranker per result).

## Steps

1. Attribute enricher + registry entry + tests (each attribute class,
   missing → sentinel, unparseable → raw + warning, no trust-warning
   overlap; `Pipeline` orders it before every EXPLANATION reranker
   regardless of declaration order).
2. Diversity reranker + registry entry + tests (signal values exact
   on a hand-built 6-hit list: first-per-domain 1.0, repeats 0.5,
   0.33…; rank-1 stability; no hit is ever removed).
3. News grouping + perspective rerank + registry entry + tests
   (grouping correctness on near-duplicate titles with the pinned
   bigram/Jaccard/date rule, credibility-weighted — not equal — mix
   with a distrusted outlier ranked below, `story_groups` shape
   asserted on `SearchContext.metadata`, hits beyond K=50 ungrouped;
   `trusted`-bool fallback path tested with scoring signals absent).
4. Shopping/travel relevance + registry entry + tests (budget/
   location/facilities constraints reorder with cited reasons on
   hand-built hits carrying `price`/`currency`/`location` metadata;
   currency mismatch → neutral; missing attributes fall back to
   keyword behavior with a reason saying so).
5. Profiles + composite + config update + mode tests. Accept:
   pre-existing ranking tests pass unchanged; the step 3–4
   arrangements show the Goal's orderings through the full engine
   (attribute enricher → rerankers → engine `Ranker`); repeated runs
   produce identical `context.results` id order (determinism test,
   no timestamps/randomness in ordering path). The shipped fixture
   corpus is used only for a determinism/diversity smoke check — it
   carries no price or perspective metadata to rerank on.

## Validation Plan

- `uv run pytest tests/unit/test_rerank_pack.py -q` green.
- `uv run pytest tests -q` green; `uv run ruff check src tests` and
  `uv run ruff format --check src tests` clean; `uv run mypy src` clean.
- Manual (step 3–4 arrangements through a test harness that prints
  `SearchContext.metadata`): news query returns grouped stories with
  mixed-but-credible perspectives and `story_groups` present in
  context metadata (not in the API diagnostics payload — see Reality
  baseline); shopping query with `{"max_price": N}` honors budget
  with reasons; three repeated runs print identical id order.

## Scalability notes (bottleneck + at-what-scale + fix shape)

- Story grouping naive pairwise is O(n²) string-sim compares. Breaks
  first at ~10x (federated merged cap M=200 candidates → ~20k
  compares per request, seconds of pure Python; unbounded candidates
  would be worse). Fix folded into this plan: group only the top-K
  candidates (K=50, configurable) via the bigram inverted index so
  pairwise compare runs only within small blocks, and skip merging
  for any block over 25 candidates (a shared bigram such as a
  stopword pair must not recreate an O(K²) block); document both
  cutoffs in the module docstring. Cost is then bounded by K and
  block cap at any corpus size — per-request work scales with the
  candidate window (≤ M=200), never with corpus size, because
  providers paginate before ranking.
- Diversity counting is O(n) with one dict pass and fine to 100x of
  the capped window; keep single pass. Perspective/commerce signal
  passes are likewise O(K)/O(n) with no per-result I/O.
- Nondeterministic ordering breaks caching and eval at any scale.
  Folded rule: stable sorts only, sorted iteration over groups, no
  wall-clock/randomness in the ordering path; determinism test in
  step 5 guards it.
- Attribute parsing repeated by enricher + rerankers: parse once in
  the enricher into normalized keys; rerankers read keys, never
  re-parse raw metadata (folded in). No per-result I/O or network,
  no cross-request cache or stored state: grouping is recomputed per
  request (≤ ~1.2k bounded compares at K=50), which is the correct
  trade until QPS — not corpus — forces a native/vectorized
  implementation; that rewrite is flagged, not built.

## Non-goals

- AI summarization of groups or model-based perspective analysis
  (AI Capabilities owns §9F generative work).
- Personalized reranking across sessions (no user profiles stored;
  boosts come only from the current request's filters).
- Private re-sorts that bypass the ranker, and any engine change to
  reorder ranker vs rerank (left as a synthesis coordination item).
- Hard diversity caps that drop hits, and guaranteed perspective
  quotas: a weighted-sum composite can only demote softly; removal
  belongs to filter-pack and quota guarantees need a different
  ordering mechanism (synthesis decision if ever required).
- New `SearchResult`/`SearchRequest` dataclass fields (groups and
  signals live in `metadata` under documented keys).
- Coordination left for synthesis (not this task): EXPLANATION-stage
  rerankers vs folding the same pure functions into `Ranker`
  (either home must keep the step 3–4 signal math); exposing
  `story_groups` through the API diagnostics (T15 whitelist);
  `max_price`/`location`/`facilities` filter-key registration owned
  by filter-pack (T10, seam D4); shared `ranking/*` + `registry.py`
  edits sequenced T11→T12.

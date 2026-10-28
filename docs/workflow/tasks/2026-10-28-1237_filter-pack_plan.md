# Safety & Privacy Filter Pack Plan

## Goal

Complete the mandatory filter layer: privacy-sensitive filtering,
blocklists/allowlists, and user-defined restrictions — all fail-closed,
all excluders (never scorers), per original plan §3/§9B/§10.

Falsifiable bar: a fixture hit-list containing one PII-bearing hit per
PII class, one hit per blocklisted domain/pattern, and hits violating a
sample user expression is reduced to exactly the compliant subset, with
every removal attributed to a named filter in `applied_filters` +
diagnostics; with any one filter's outage simulated, the request raises
instead of returning unfiltered hits.

## Reality baseline (verified 2026-10-28 against the tree)

- `plugins/safety_filter.py` EXISTS: mandatory `FILTER` at `FILTERING`
  stage, substring match over title/snippet/url for `blocked_terms` +
  `blocked_domains`, `simulate_outage` fail-closed test hook, records
  `applied_filters` + `metadata["safety-filter-removed"]`. It already
  owns term/domain blocklisting — the new blocklist/allowlist work must
  EXTEND it (or absorb it with its tests kept green), never create a
  second competing domain-blocklist owner. Its domain check is
  `domain in url.lower()` (substring over the whole URL), so
  `evil-example.com` matches a block on `example.com` and
  `example.com.evil.test` matches too — the host-parsing fix in Scope
  is a correctness fix, not a style change. (The nearest precedent,
  `TrustEnricher._domain_of`, uses `urlparse(...).netloc`, which keeps
  port/userinfo; filters must use `.hostname` instead — do not copy it.)
- `SearchResult` carries `snippet` (200 chars, built by
  `retrieval/normalizer.py`) — there is NO `content` field. Filters see
  title/snippet/url/metadata only. "Scan content" is impossible at the
  filter stage without a new content-carriage contract (out of scope;
  scan snippet/title/url/metadata).
- `SearchRequest.filters: dict` exists and `api/routes.py` copies it
  through, but NOTHING consumes it today (grep: only the dataclass
  field, the API schema, and the route copy reference it). User-defined
  restrictions define and own the keys of this dict (schema + validation
  + unknown-key rejection) — with one forward-compatibility carve-out
  pinned in Scope (review note D4).
- Pipeline fail-closed semantics already exist (`core/pipeline.py`:
  mandatory `PluginError` propagates; optional failures isolate into
  diagnostics). Each new filter follows the `safety_filter.py`
  pattern: `mandatory=True`, `PluginError(mandatory=True)` on outage or
  unverifiable enforcement, `simulate_outage` config hook for tests.
  No existing plugin declares `dependencies`; intra-`FILTERING` order
  is currently just declaration order in the topological sort, so the
  step-4 ordering must be declared, not assumed.
- Error-surface reality: `api/errors.py` maps `PluginError`→500
  `plugin_error`, `ConfigError`→500 `config_error`, and Pydantic
  `RequestValidationError`→422 — a bare `ValueError` raised during a
  request falls into the generic 500 `internal_error` handler. Plan
  wording that says "rejected with `ConfigError`/`ValueError` at the
  boundary" must therefore name the exact raise site and expected HTTP
  shape (Scope pins this); an unmapped `ValueError` is an internal
  error, not a client rejection.
- Removed-count visibility gap (shared with query-intelligence):
  `metadata["safety-filter-removed"]` lives in `context.metadata`,
  which `SearchContext.diagnostics()` does not include and the API
  `DiagnosticsSchema` does not copy — counts are in-process evidence
  today, not API-visible. Step 4 states this instead of implying the
  API already surfaces them.
- No conformance suite exists yet (`tests/` has `unit/` + `integration/`
  only; Wave 2F owns conformance suites per the shared context). This
  task writes its stage/isolation/explanation tests as ordinary unit +
  integration tests and LEAVES a conformance handoff note for 2F — it
  does not create the conformance suite.

## Scope (all connected: one mandatory-filtering stage)

- Privacy filter (new `FILTER`, `FILTERING` stage, mandatory): PII
  patterns (email, phone, government IDs, street addresses) over
  title/snippet/url; per-PII-class config chooses exclusion vs
  redaction. The pattern set per class is pinned in config (explicit
  regex list per class, each with fixtures in step 1); street-address
  matching is limited to those config-supplied patterns — no invented
  general address grammar. Redaction appends a reason per hit and never
  writes `original_score`/`final_score` itself (asserted in isolation;
  because `FILTERING` precedes `SCORING`, a redacted token can still
  change a downstream scorer's term overlap — the step-1 test asserts
  score fields untouched by the filter, not pipeline-final equality).
  Runs BEFORE scoring plugins (stage order already guarantees this;
  `dependencies` are only for intra-`FILTERING` order in step 4).
- Blocklist/allowlist filter: EXTEND `safety-filter` with URL-pattern
  lists (glob/regex from config, compiled once at `configure()`) and an
  allowlist mode that flips to default-deny when
  `context.request.mode == "child-safe"` (the only default-deny trigger;
  no other mode implies it). Domain matching must parse the URL's host
  via `urlparse(...).hostname` (not substring — `domain in url`
  false-positives are a known weakness of the current implementation —
  and not `.netloc`, which keeps port/userinfo); keep existing tests
  green while fixing the matcher.
- User-defined restrictions: schema + parser + applier for per-request
  `filters` expressions (must/must-not terms, domain rules). User terms
  are literals (escaped before any regex use — per-request input never
  becomes a raw regex). Invalid expressions are never silently ignored,
  with the raise site pinned: malformed config lists raise
  `ConfigError` at `configure()` (500 `config_error` via the API);
  malformed per-request `filters` raise
  `PluginError(mandatory=True)` from the applier (fail-closed 500
  `plugin_error`). A 422 mapping for per-request validation is a
  desirable API change but belongs to the API owner — flagged in the
  cross-plan notes, not silently assumed. Expressions compiled once per
  request into a predicate, applied per hit. Forward-compatibility
  (review D4): unknown-key rejection MUST accept-and-pass-through the
  reserved consumer keys owned by later plans — any `prefer_*` key
  (scoring pack) and `max_price` / `location` / `facilities` keys
  (rerank pack) — without
  consuming or rejecting them; only keys outside the owned schema and
  that reservation are rejected. Without this carve-out the validator
  shipped here breaks both later plans.
- Every filter: declares mandatory/optional, fails closed on outage or
  unverifiable enforcement, records applied-filter evidence
  (`applied_filters` entry + removed-count in context metadata, same
  shape as `safety-filter-removed`).

## Steps

1. Privacy filter + unit tests (each PII class × {redact, exclude};
   redaction-leaves-score-fields-untouched test at the filter in
   isolation; no-PII passthrough test).
2. `safety-filter` extension: host-parsing matcher + URL patterns +
   default-deny allowlist mode + tests (incl. allowlist-mode test where
   an unlisted domain is excluded and a listed one passes, substring
   false-positive regressions — `evil-example.com` and
   `example.com.evil.test` do NOT match a block on `example.com`, port/
   userinfo URLs parse to the bare host; existing safety-filter tests
   kept green).
3. User-restrictions schema/parser/applier + tests (each operator, plus
   invalid-expression rejection with the pinned raise site, unknown-key
   rejection, and reserved-key pass-through for `prefer_*` /
   `max_price` / `location` / `facilities` keys — including an explicit
   `facilities` pass-through assertion).
4. Pipeline wiring (all three in `FILTERING` stage with mandated order:
   user restrictions → blocklist/allowlist → privacy, each declaring
   `dependencies` on the previous so the topological sort enforces it —
   asserted via `plugin_executions` order in a test, not declaration
   order alone) + per-filter `simulate_outage` fail-closed tests +
   applied-filter evidence tests (evidence asserted on
   `applied_filters` + `context.metadata` counts in-process; API
   surfacing of the counts needs the diagnostics extension flagged in
   the cross-plan notes); conformance handoff note for Wave 2F (stage
   declaration, isolation, explanation guarantees to assert).

## Validation Plan

- `uv run pytest tests/unit/test_filter_pack.py tests/integration -q`
  green; then `uv run pytest tests -q` green.
- `uv run ruff check src tests`, `uv run ruff format --check src tests`,
  `uv run mypy src` clean.
- Manual: child-safe query (allowlist mode) over a fixture corpus with
  blocked domains + PII-bearing hits → excluded hits reported per
  filter in `applied_filters`/diagnostics, kept hits explainable;
  each filter with `simulate_outage: true` → request raises
  `PluginError(mandatory=True)` and no partial result list escapes.

## Scalability notes

| Bottleneck | Breaks at | Fix shape (folded in or flagged) |
|---|---|---|
| Regex-per-hit-per-pattern PII scan | 100x hits (O(hits × patterns)) | One compiled alternation per PII class, compiled at `configure()`; first-match short-circuit in exclude mode; scan capped to snippet/title/url lengths (in scope) |
| Blocklist linear scan per hit over user-sized lists | 10x list sizes | Set/dict lookup on parsed host + precompiled URL-pattern regexes; parse host once per hit (in scope) |
| Per-request expression parse cost | 10x QPS | Compile once per request; bound expression size/depth in schema (in scope) |
| Remote blocklist fetch on the request path | Any outage/latency | Lists load at `configure()`/startup only; refresh on timer, never per request (in scope) |
| Config-supplied URL/PII regexes: catastrophic backtracking (ReDoS) on adversarial snippets | Any single crafted hit | Pattern count + length caps validated at `configure()`; glob preferred, raw regex only from trusted config (never per-request input, which is escaped literals); host-set check runs before any regex tier so most hits never reach a regex (in scope) |
| URL-pattern regex tier is still O(patterns × hits) after the host-set miss | 10x patterns × 100x hits | Bounded pattern count (above); combined alternation per list instead of a per-pattern loop (in scope) |
| Hit count itself unbounded for direct engine callers (API caps `limit` at 100; `SearchEngine` does not) | 100x hits via non-API callers | Filters stay O(hits × classes) with short-circuits (in scope); an engine-level result cap belongs to the engine/federated owners — flagged, not built here |

Rewrite-forcing risk (flagged, not built): none at 100x for
deterministic rules; ML-based PII/safety classification (streaming or
sidecar inference) belongs to the AI-capabilities plan.
Cross-plan notes (left for synthesis): (a) D4 reserved-key pass-through
above must match the scoring/rerank packs' final key names — synthesis
confirms the reservation list when those plans land; (b) removed-count
and query-provenance surfacing share one missing piece — a diagnostics
extension (`context.metadata` → `diagnostics()` → `DiagnosticsSchema`)
that no single plan owns yet; (c) this plan remains the sole excluder
(review S2): the AI-capabilities plan labels/advises only, and its
outage must never change what these filters excluded.

## Non-goals

- AI-based classification (belongs to AI Capabilities; these are
  deterministic rules).
- Crawling-time filtering (ingest packs own crawl policy; these filters
  run at query time on normalized hits).
- Full-document-content scanning (filters see snippet/title/url/
  metadata; no new content-carriage contract in this task).
- Creating the Wave-2F conformance suite (this task ships unit +
  integration tests plus a handoff note).
- A second domain-blocklist owner: `safety-filter` remains the single
  owner, extended in place.

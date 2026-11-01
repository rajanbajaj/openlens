# Adding a New Search Category (e.g. Events)

Reference implementation: the `events` mode (release v0.4.15, commit
`59c6bd5`). A category is a domain-filtered search mode: pages tagged
with a `site_category` at ingest, pre-filtered at retrieval, ranked
with a dedicated profile, exposed as a UI tab. Follow the checklist
in order; each item has the exact file and the test that pins it.

Conventions: backend modes are kebab-case (`child-safe`); the YAML
key, profile name, `_MODE_SITE_CATEGORY` value, `site_category`
value, and frontend `SearchMode` member are all the same string.
Wire changes are additive — never rename an existing category.

## 1. Domain list (`configs/category_domains.yaml`)

Add a `<name>_allow` section with bare registrable domains (suffix
matching covers `www.` and subdomains, so never list those). For
event-like subsections of broad domains (e.g. `developer.apple.com`
for WWDC), add the full subdomain — never the apex domain, or every
page on that domain classifies into your category. Group entries
under a `# --- ... ---` comment and document the semantics in the
header comment.

## 2. Classifier (`src/openlens/indexing/crawler.py`)

Three coordinated edits, else the YAML list is silently ignored:

1. `SITE_CATEGORIES` — add the category value.
2. `_CATEGORY_DOMAIN_KEYS` — add the `<name>_allow` key.
3. `classify_site_category` — add the evaluation branch (domain hit
   → `1.0`), placed in the documented tie-break order.

Test: `tests/unit/test_index_titles_categories.py` — domain-hit
cases plus YAML spot-checks (`assert "<domain>" in
domains["<name>_allow"]`).

## 3. Retrieval pre-filter (exact totals)

1. `src/openlens/core/engine.py` — `_MODE_SITE_CATEGORY`: add
   `<name>: '<name>'` so `SearchEngine.search` passes
   `site_category` down.
2. `src/openlens/providers/custom_index.py` — `MODE_SITE_CATEGORY`:
   same entry for the row-level filter. Explicit
   `request.site_category` still wins over mode.

Test: `tests/unit/test_category_pushdown.py` — provider exactness,
explicit-beats-mode, and engine totals-matches-results pins.

## 4. Post-retrieval net (`src/openlens/plugins/mode_category_filter.py`)

Add `<name>` to `MODE_CATEGORY`. Decide strict (news/shopping:
category-exact only) vs keyword fallback (travel: exact match or
text keywords via a `<NAME>_KEYWORDS` set + the generalized
fallback). Document the choice.

Test: `tests/unit/test_mode_category_filter.py` — keeps, drops,
keyword, and empty-filter-fallback pins.

## 5. Ranking (`configs/ranking.yaml` + `src/openlens/ranking/profiles.py`)

Add the `<name>` profile to both, mirroring the closest sibling
(events mirrored travel: `keyword_boost`, `commerce_weight`,
domain keywords). The YAML↔BUILTIN parity test fails otherwise.

Test: `tests/unit/test_pipeline.py` exact-set assertion and
`tests/unit/test_rerank_weights.py` /
`tests/unit/test_ranking_weights.py` per-mode loops — extend each
to the new mode.

## 6. First-class plumbing (no silent `other`)

1. `src/openlens/storage/repository.py` — `SITE_CATEGORY_VALUES`:
   add the value or stored rows collapse to `other`.
2. `src/openlens/plugins/user_restrictions.py` — add to the inert
   loop; never to the child-safe branch (pinned by
   `tests/unit/test_plugin_contract.py`).
3. `src/openlens/retrieval/agent.py` — `build_parse_prompt` mode
   list, so agent-planned runs of the mode pass `_coerce_plan`
   (`get_profile` validates; unknown modes coerce to `default`).
   `ChatRequestSchema` needs no change — chat takes no mode.
4. `src/openlens/retrieval/query_intent.py` — optional:
   `INTENT_TO_MODE` entry to *suggest* the mode (events skipped
   this; like `child-safe`, a mode can stay explicit-only).

Tests: plugin-contract inert pin, agent plan passthrough
(`tests/unit/test_agent.py`), API acceptance
(`tests/unit/test_api.py`).

## 7. Frontend (`frontend/src/`)

1. `api/types.ts` — extend the `SearchMode` union (prettier
   `printWidth` is 100; wrap the union when it overflows).
2. `components/ModeSelector.tsx` — append to `SEARCH_MODES`
   (order is pinned by test; agent stays last).
3. `components/ResultCard.tsx` — `verticalHighlight` branch for
   the mode (reuse enrichments readers; degrade absent fields to
   no line) + `data-testid="result-vertical-<name>"`.
4. No `App.tsx` change needed — mode flows
   state → `buildSearchBody` → `searchLens` → `ResultList` →
   `ResultCard` automatically.

Tests: `ModeSelector.test.tsx` exact-set, card branch
present/absent tests, a new `App.<name>.test.tsx` mirroring
`App.agent.test.tsx` (mode in body + vertical render). Do NOT
hand-edit `response.fixture.test.ts` — re-capture a real envelope
for the new mode instead.

## 8. Data migration

New crawls classify at extraction. Already-indexed rows need
`backfill_site_categories()` (mirrors the classifier over stored
URL+metadata, rewrites only stale rows). There is no CLI or admin
button — run the script inside the backend container (see the
team's runbook); safe to run live, then verify with a
`site_category` count query.

## 9. Seeds and regen

- Seeds are pasted into the admin **Enqueue seeds** box, not
  committed — keep category seed lists out of the repo.
- OpenAPI has no enum for mode (open non-blank string), but still
  regen + orval after schema-adjacent changes and commit the
  output only if it drifts.
- Gates before handoff: backend `pytest tests -q`, `ruff check` +
  `format --check`, `mypy src`; frontend `vitest`, `format:check`,
  `lint`, `build`.

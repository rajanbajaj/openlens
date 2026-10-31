# Research findings: search filters, ranking, titles, tokenizer, categories, UI gaps

Source: 10-agent parallel research synthesis (2026-10-30), evidence-backed
against the repo at `5b7abf..88388ed`. Product values:
[original-feature-list.md](docs/workflow/v1/original-feature-list.md).

## 1. Ranked gap list (filters / ranking / UI / sponsored)

Reference: `§N` = section of `original-feature-list.md`.

### #1 UI stale results — CRITICAL, small fix (frontend-only)

No filter, mode, or provider-prefs edit re-runs search. `Sidebar`
`onChange` handlers only update state (`App.tsx:266-278` `setUserFilters`,
`App.tsx:241,256` `setMode`, `Sidebar.tsx:217-228`); only
`handleChipRemove` re-runs (`App.tsx:203-205`), and pagination alone
re-fetches (`App.tsx:166-174`). This is the primary cause of the reported
"disabling sponsors does nothing" complaint: the toggle wire is correct,
but results are never re-fetched until the user presses Search again.
Fix: call `runSearch` on filter/mode/prefs change, or add an Apply button.

### #2 Sponsored coverage gap — HIGH, medium fix

`exclude_sponsored` hard-excludes only hits with
`metadata.sponsored===True` (`user_restrictions.py:432,453`), which only
the crawler ever sets (rel/data-sponsored, `crawler.py:799-800`). Seeds
(`seed.py:5-94`) and all providers set no sponsored flag, so the flag half
is a no-op on federated/unflagged shopping results — the actual sponsored
surface. The companion `must_not_terms:['sponsored']` text fallback
(`filters.ts:129-131`) is the only effective half and is wrong both ways
(probe: misses real ads, removes organic articles about sponsored content).
All three checks use strict identity (`is True` / `=== true`), missing
truthy `1`/`'true'`. Fix: normalize sponsored to bool at ingestion, carry
a provider-declared marker, drop the text fallback; keep demote-by-default
(`ranker.py:139`, `sponsored_weight` 0.25–1.0 across
`profiles.py:59,73,88,103,118` / `ranking.yaml`) + badge + warning/reason
+ applied-filters reporting (`user_restrictions.py:561,567`).

### #3 Shopping demotes sponsored least where risk is highest — HIGH, small fix

`sponsored_weight` is 0.25 for shopping/travel vs 0.5 default and 1.0
news/child-safe (`ranking.yaml:62-99`); commerce reranker demotes, never
excludes (satisfied-fraction, currency-mismatch/missing neutral 0.5).
Fix: raise the shopping weight and/or default exclude on.

### #4 Brand/style/size/occasion ignored — MEDIUM-HIGH, medium fix

Accepted as passthrough (`user_restrictions.py:414-415`) but nothing
consumes them — commerce `constraints_from_filters` reads only
max_price/location/facilities (`commerce_reranker.py:113-138`), and
location/facilities are ordering-only, never exclude; max_price is
ordering-only unless `exclude_over_budget` (`user_restrictions.py:470-483`).

### #5 No sort control or param — MEDIUM, small-medium fix

Only display-side `Array.sort`; ranker `weight_overrides`
(`ranker.py:142-155`) has no UI. Fix: add sort control emitting overrides.

### #6 `prefer_*` mismatch — MEDIUM, small fix

Scorer reads `prefer_domains`/`prefer_topics` lists
(`preference_scorer.py:102-150`); UI emits `prefer_<suffix>=true` bools
(`filters.ts:170-175`) that match nothing. `personalize:false` honored.

### #7 safety_preset standard/balanced are no-ops — MEDIUM (trust), small fix

Only child-safe/strict filter (`safety_filter.py:288-306`). Fix: label or
implement.

### #8 Missing chips, single-value domain prefs, fail-closed providers — LOW-MEDIUM

No sidebar chips for excludeOverBudget, occasion, license, author, cuisine,
dietary tags, ingredients, allergens, personalize, safetyPreset
(`Sidebar.tsx:58-145`); domain prefs single-value; unknown provider names
fail closed server-side (`manager.py:423-444`) with no client validation.

### Ranking-stack context (verified)

RRF rank-only fusion (`fusion.py:44-56`, URL-key dedupe); literal-overlap
relevance, title 2x (`relevance_scorer.py:65-92`); semantic opt-in,
fail-open to `[]` (`semantic.py:71-103`); trust tiers + https + domain age;
freshness `0.5^(age/30d)`, undated neutral 0.5; diversity / news /
commerce ordering-only, never re-sort; composite ranker, missing signals
neutral 0.5, unknown modes fail loudly. **No popularity/engagement term
exists anywhere** — the fairness value "prevent popularity dominating
sensitive searches" has no direct counterweight beyond trust/fairness.

## 2. Garbled titles — root cause + fix

Single bug explains all samples (RedHat trailing icons, Microsoft repeated
privacy-icon titles, Quinn social-icon dump). `_LinkTitleParser`
(`crawler.py:293-321`) sets `_in_title` on ANY `<title>` start tag with no
svg/head scoping and concatenates all `handle_data`; `_PageDataParser`
repeats the identical unscoped logic (`crawler.py:448-529`); `_to_document`
uses the parser title verbatim with URL-only fallback — no og:title/h1
fallback, no cleanup (`crawler.py:1553-1564`); storage persists verbatim
(`repository.py:244-266,284-308`). Reproduced: head
`<title>Real Page Title</title>` + 2 svg icon titles concatenates to one
string. Body-text path is innocent (`extract_visible_text` only collects
`handle_data`, `crawler.py:353-368`).

Fix (crawler only + tests):

1. Scope title capture to head only — track svg depth and a head flag; set
   `_in_title` only when tag is `title` AND svg depth is 0 (ideally inside
   `<head>`). Apply identically in both parsers.
2. Title-selection cascade in `_to_document`: cleaned head `<title>` ->
   meta og:title / twitter:title (already collected, add lookup) ->
   first `<h1>` (add capture) -> URL fallback.
3. `_clean_title` helper: `html.unescape`, collapse whitespace, strip;
   reject titles that are pure icon-label runs (>50% tokens ending "icon"
   or repeated phrase) by falling through the cascade.

## 3. Tokenizer issues + fix plan (impact order)

All probe-confirmed:

1. ASCII-only `_TOKEN_RE=[a-z0-9]+` + `.lower()` (`query_parser.py:8`):
   `café` -> `['caf']`, `北京大学` -> `[]` with `fts ''` (silent
   zero-result), `e-mail` -> `['e','mail']`, `state-of-the-art` -> 4-term
   AND including stopwords.
2. `_punct_to_space` maps every non-alnum char to space
   (`query_normalize.py:41-44`): `github.com/pkg/errors@v0.9.1` ->
   `github com pkg errors v0 9 1`, destroying version/dot/@ structure.
3. FTS5 table uses default unicode61, no trigram/porter/prefix
   (`repository.py:141-143`): index folds café~cafe but parser emits `caf`,
   so they never meet; no prefix `*`, so code-prefix queries impossible.
4. `custom_index.py:281` AND-joins every token (mandatory); version-split
   queries over-constrain, single-char terms (`e`, `x`) dilute BM25;
   hyphenated names (`X-Phone 15 Pro`) lose phrase identity.
5. Pipeline split: `custom_index.py:276` calls `parse_query` on the raw
   query, bypassing `normalize_query` (NFKC/casefold, used only in tests);
   `semantic.py:79` embeds the raw query, diverging from the FTS token
   stream before RRF fusion.
6. `suggest.py`: whole-string prefix only, whitespace-only related-query
   split (`node.js` vs `node js` share zero tokens), whole-query
   Levenshtein (`max_distance=2`), zero synonym map/stemmer.
7. Load-bearing constraint: raw `e-mail` sent to FTS5 MATCH raises
   `OperationalError` (no such column) — operator-escaping must be kept.

Fix order: (a) widen `_TOKEN_RE` to Unicode alphanumerics
(`re.UNICODE` `\w` minus `_`) + casefold — biggest win, multilingual and
accents; (b) wire `normalize_query` into the custom_index path before
`parse_query` (one line); (c) preserve code-significant tokens (emit split
AND terms plus an optional/OR clause; demote pure-numeric/single-char
mandatory terms to SHOULD); (d) hyphen handling (compound + parts, or
trigram/prefix `"x-phone"*`); (e) normalize before embed in `semantic.py`
(shared string); (f) suggest upgrades, additive only (last-token
completion, normalized-token overlap, per-token spelling, small synonym
map like js/javascript, k8s/kubernetes).

## 4. Category detection (shopping / news / kids / other)

Status: **no `site_category` key exists anywhere** in `src/` or `configs/`.
Trust tiers (`trust_scorer.py:114-141`) and crawler fetch gates
(`crawler.py:829-831`) are not categories. Frontend has no category
filter; category UX is query `mode` plus ResultCard kind derived from mode.
Query-side intent labels (`ai_understanding.py:117-137`) are not page
labels.

Detectable from existing columns (no migration): shopping YES — price or
(currency+availability) or brand+Product/Offer JSON-LD
(`crawler.py:705-725,417-425`); rule `price OR (availability AND (brand OR
rating))` is high precision. News PROVISIONAL — Article/NewsArticle JSON-LD
+ `article:` meta + author + published date; over-triggers blogs/docs, so
ship as score with domain assist, not a hard label. Kids NO — only
negative signal `content_safety=adult` (`crawler.py:769-772`); no positive
kids signal. Other = default bucket. `content_type` is near-useless (almost
always text/html); `published` (normalized YYYY-MM-DD via `_to_iso_date`)
helps recency, not category.

New columns (all computable offline in `extract_page_attributes` /
`_to_document`, then added to `RANKING_METADATA_KEYS` + repository
`doc_columns`/DDL/migrate + custom_index FTS filter; backfill by re-running
the classifier over stored metadata+content):

- `site_category TEXT NOT NULL DEFAULT 'other'`, CHECK in
  (shopping, news, kids, other).
- `site_category_confidence REAL` 0..1 (1.0 exact-domain hit, 0.9
  multi-signal, 0.7 single strong, 0.5 model, 0.3 default other).
- `site_category_source TEXT` (`rules` | `model` | `default`, versioned,
  e.g. `rules-v1`).
- `kids_domain_hit INTEGER` 0/1 — auditable safety gate.
- Optional `news_domain_hit INTEGER` 0/1.

Config: new `configs/category_domains.yaml` with `shopping_allow`,
`news_allow`, `kids_allow`, `adult_block` (<200 entries, exact-or-subdomain
match per `_host_matches`/`_normalize_domain` semantics) — NOT
`providers.yaml` (provider wiring only).

Classifier: rules-first + model fallback, crawlable signals only (URL,
title, meta/JSON-LD, visible text; no WHOIS). Stage A deterministic:
shopping (domain hit 1.0 > price+(availability|currency|brand) 0.9 >
price-alone/Product+brand 0.7 > cart/checkout/product path + price-ish 0.6);
news (news-domain + article signals 0.9 > NewsArticle+published+author 0.85
> off-domain article+published 0.6 provisional, never hard-filter); kids
(kids-allow + not adult 1.0; adult/adult-block fail-closed never kids; text
lexicon capped 0.55, never label kids on text alone); tie-break
shopping > news > kids > other. Stage B model fallback (existing
optional-plugin pattern): only low-confidence non-safety rows; input host
+ title + ~500 chars text + compact signal JSON; accept only the 4 labels
with 0..1 confidence; never downgrade a 1.0 domain hit; kids requires
domain hit or >=0.85 confidence with no adult marker.

Query wiring (mirrors user-restrictions precedent): new owned filter key
`site_category` (hard-exclude, fail-closed on unknown value); add to
RESERVED/OWNED sets + `filters.ts` + FilterPanel dropdown + chips +
`idx_documents_site_category`; strict safety forces `site_category=kids` +
safety double-gate; labels join `RANKING_METADATA_KEYS` so reindex never
drops them.

## 5. Competitor mechanisms worth adopting (mapped to feature-list values)

1. EVENT GROUPING (§neutral-news "group same event"): Google News NLP entity
   extraction + story-understanding knowledge graph, non-personalized Full
   Coverage storylines; Bing high-frequency crawler (RSS/sitemaps/publisher
   APIs) + Storyline clustering with demand + publisher signals. Adopt:
   replace/extend bigram-Jaccard grouping (`news_reranker.py:100-149`) with
   entity-overlap + same-day windows + story pages.
2. PER-ARTICLE PERSPECTIVE + BIAS BAR (§fair, §news): Ground News
   publication-level bias averaging (AllSides + Ad Fontes + MBFC), Bias Bar
   counts, Blindspot one-side-underreported feed, factuality + ownership
   data. Adopt: Bias-Bar counts per story + missing-perspective surfacing
   (extends `story_groups`/`missing_perspectives`).
3. SPONSORED SEPARATION + TRANSPARENCY (§news, §shopping, §12): Google paid
   content separation rules, rel=sponsored/nofollow + disclosure,
   byline + date + News-vs-Opinion labels. Adopt: exclude paid content from
   news + opinion-label passthrough.
4. MISLEADING-HEADLINE DEMOTION (§news, §fair): policy bans on
   deceptive/clickbait previews, originality/authority/sourcing signals,
   prediction-headline demotion. Adopt: headline-trust signal feeding
   `news_trust`.
5. KID-SAFE 3-TIER + WHITELIST (§child-safe): Kiddle editor-picked top
   results + readable-safe tier + SafeSearch-filtered remainder;
   whitelist+editorial catalog (blacklist alone unmaintainable);
   Brave SafeSearch Off/Moderate(default)/Strict + DNS enforcement + abuse
   reporting; publisher RTA meta tags. Adopt: allow/blocklists +
   default-deny strict + RTA/meta + abuse-report path.
6. PRIVACY / ANTI-TRACKING posture (cross-cutting trust): Startpage
   anonymizing proxy + no-log, Mojeek independent index + country-only logs.
   No surveyed engine does query-intent PII/doxxing detection — keep that as
   the OpenLens differentiator.

## 6. Open / unconfirmed items

- DuckDuckGo SafeSearch Moderate-vs-Strict semantics and Kiddle/KidRex live
  status rest on snippets, not live help pages.
- Ground News methodology body unfetched (403); outlet counts vary by
  source date.
- MySQL/Postgres backend tokenizers uninspected — same issues assumed.
- Non-custom_index `normalize_query` path unconfirmed; end-to-end HTTP
  `exclude_sponsored` against a live crawler-flagged doc unverified; index
  sponsored-hit prevalence unconfirmed.
- No per-category gold label set; news-vs-blog and kids thresholds need a
  hand-labeled sample; category config location needs owner confirmation.

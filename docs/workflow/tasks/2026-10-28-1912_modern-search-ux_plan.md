# Modern Search UX Plan (instant search + verticals + personal library)

## Goal

Make OpenLens feel like a modern search product: instant-as-you-type
results with keyboard-navigable suggestions, vertical-aware rendering
for news/shopping/travel, and a small personal library (recent
searches, saved results) — reusing every backend field and the glass
shell, with zero axe critical violations.

## Success Criteria

- Typing shows a suggestions dropdown (from `GET /api/v1/suggest`,
  debounced, cancellable) with full keyboard flow (ArrowUp/Down,
  Enter, Escape) and an `aria-live` announcement; backend-absent
  suggest degrades to direct search, never an error wall.
- Spelling hints and related searches render from the new assistance
  fields only when present; nothing renders for unshipped fields.
- News/shopping/travel modes render vertical-aware cards (dates +
  source for news, price/attributes for shopping, location/facilities
  for travel) from fields the backend already returns.
- Recent searches and saved results persist in local browser state,
  work fully offline, and never send identifiers to the backend.
- Shell stays fast: suggestions debounced, diagnostics rail still its
  own lazy chunk, no eager imports added.
- All five frontend gates green: `npm test`, `npm run lint`,
  `npm run format:check`, `npm run build`,
  `npm audit --omit=dev`; axe zero-critical on all four regions.

## Approach

Workspace facts (verified 2026-10-28 against the tree):

- `frontend/src/App.tsx` owns `runSearch` (paged via
  `offsetForPage`), hero ↔ sticky-header swap, shell grid with
  collapsible `Sidebar`, center results, lazy `DiagnosticsRail`.
  Every branch has `data-testid` hooks; the plan keeps them all.
- Fetch lives in `api/client.ts` (`searchLens`, `buildSearchBody`,
  `validateQuery`); typed wire in `api/types.ts` + `api/guard.ts`;
  filters in `api/filters.ts` (byte-identical wire keys). New fetch
  helpers land in `client.ts` only — one owner to avoid merge
  collisions.
- `ResultCard.tsx` already renders provider/`also_from` badges,
  attributes, understanding line, score breakdown, AI digest +
  Sources, reasons/warnings disclosures; verticals regroup these
  readers, they do not invent new data.
- Glass shell (`index.css` tokens, `GlassPanel`, shell grid,
  360/768/1280 breakpoints, reduced-motion gating) plus the task-18
  a11y contract (announcer, focus management, Escape) are preserved;
  no new theme, no new font stack.
- Binding constraints from `frontend_context.md`: no `src/` edits,
  mirror `schemas.py`, no hardcoded hosts or secrets.

Key decisions:

| Decision | Recommendation | Why |
|---|---|---|
| Suggest transport | `GET /api/v1/suggest` via `client.ts`, 150ms debounce, abort stale | Single fetch owner; no race artifacts |
| Personal library | `localStorage`-backed recent/saved, no backend round-trip | Offline-first; no accounts this round |
| Pagination vs infinite | Keep numbered pagination, add prefetch-next | Matches backend `offset`; cheaper a11y story |
| Click signals | Fire-and-forget `POST /api/v1/feedback` with doc id only | No PII; failure never blocks rendering |

## User Flow + Feature Map

- Type → dropdown (highlight, keyboard select, Escape closes, announcer
  names result count) → Enter runs full search.
- Results → spelling hint when present ("Showing results for…"),
  related-search chips rerun search, vertical cards per mode.
- Save icon per card → library drawer section; recent searches as
  hero chips; both clearable, both offline.

## Steps

1. API contract mirror: `client.ts` + `types.ts` + `guard.ts`
   additions (suggest, assistance readers, feedback signal) with
   mocked-fetch tests and refreshed fixtures.
2. Search box: `SuggestDropdown.tsx` (new) + `SearchForm.tsx` rework
   (debounce, abort, keyboard map, loading affiliation); hero and
   header both reuse it.
3. Vertical results: `ResultCard.tsx`/`ResultList.tsx` regroup readers
   per mode + `LibraryDrawer` section (new, local state only).
4. Shell + motion + a11y: App wiring, prefetch-next pagination,
   reduced-motion-safe dropdown animation, axe zero-critical on all
   regions, keyboard walkthrough tests.
5. Integrate: wire waves, full gates, Docker smoke, DoD checklist.

## Validation Plan

- From `frontend/`: `npm test`, `npm run lint`,
  `npm run format:check`, `npm run build`, `npm audit --omit=dev`.
- New tests: dropdown keyboard map, stale-abort behavior,
  backend-absent degradation, vertical card fixtures per mode,
  library persistence round-trip, axe on all four regions.
- Manual (reported, not claimed): Docker smoke
  (`docker compose -f deploy/local/docker-compose.yml up --build -d`,
  `http://localhost:3000`: type → suggest → search → verticals →
  save → rail; then `down`). Sandbox-blocked docker is reported with
  exact commands, never claimed.

## Risks / Open Questions

- Dropdown noise on tiny local indexes: debounce + minimum-length
  gating; empty suggest hides the dropdown silently.
- Fixture drift against new backend fields: refresh through
  `guard.ts` validation; malformed-assistance cases render nothing.
- None open: no backend edits, no new deps, no theme change.

## Definition of Done

1. Suggestions dropdown with keyboard flow + announcer, tested.
2. Spelling + related searches rendered when present, absent-safe.
3. Vertical-aware cards for news/shopping/travel with fixtures.
4. Recent + saved fully local, clearable, tested.
5. Pagination prefetch without eager rail imports (budget check).
6. All five frontend gates green with exact commands evidenced.

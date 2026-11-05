# Admin Dashboard Revamp — Concrete Spec

Status: spec only (no code changed). Evidence base: three inspected prior results —
(1) admin-frontend inventory (App/FrontierDrawer/AnalyticsPanel/ConfigEditor/TokenBar/Tour/Logo,
orval client, state, styling, tests, dead code, merges);
(2) backend-vs-view feature-gap analysis (routes.py endpoints vs FrontierDrawer/AnalyticsPanel/ConfigEditor);
(3) UX/UI read-only review (App shell, hero/token, frontier, analytics, config, Drawer, Tour, Toast, typography).
Open gap noted by caller (`primary-0: incomplete`) was resolved during spec authoring by reading
`handoff-6d379ee0...952.txt` successfully; content is incorporated below as evidence (1).

Target file for this spec: `docs/plans/admin-dashboard-revamp.md` (this file).

## 0. Scope and non-goals

- In scope: `admin-frontend/` only (App shell, FrontierDrawer, AnalyticsPanel, ConfigEditor,
TokenBar, Tour, shared `components/ui`, `src/api/*` wiring, `index.css`, colocated vitest files).
- Non-goals: no backend route changes in this revamp (search/sort/filter gaps that need backend
params are handled client-side first; backend extensions are follow-ups, not this spec);
no react-router (tabs stay state-driven); no changes to `frontend/` (user app) or `src/`;
no hand-editing of `src/api/generated/**` (regen via orval only).

## 1. Files to change / create / delete

### 1.1 Create (new shared primitives + views)

| File | Purpose |
|---|---|
| `admin-frontend/src/components/ui/DataTable.tsx` | Shared table primitive: search + sort + pagination + selection (props §2.1). Replaces per-list bespoke pagers. |
| `admin-frontend/src/components/ui/DataTable.test.tsx` | Colocated vitest for DataTable (sort/search/pagination/selection/empty states). |
| `admin-frontend/src/components/ui/SearchInput.tsx` | Labeled search field with clear button (props §2.2). Used by every table + ConfigEditor key filter. |
| `admin-frontend/src/components/ui/SearchInput.test.tsx` | Debounce/clear/label association tests. |
| `admin-frontend/src/components/ui/ConfirmDialog.tsx` | Accessible replacement for all `window.confirm` (props §2.3). Focus trap, Escape, explicit action labels. |
| `admin-frontend/src/components/ui/ConfirmDialog.test.tsx` | Confirm/cancel/Escape/focus tests. |
| `admin-frontend/src/components/ui/StatusBadge.tsx` | Semantic badge (running/paused/live/stale/ok/error/neutral) replacing monochrome Badge for state. Props: `tone`, `icon?: 'dot'` (CSS shape, never emoji). |
| `admin-frontend/src/components/ui/EmptyState.tsx` | Consistent empty/error/loading-slot panel (props §2.4). Replaces bare `<p>Loading…` and ad-hoc empties. |
| `admin-frontend/src/components/ui/FieldError.tsx` | Field-coupled error text + `aria-invalid` wiring helper for seeds-priority, inspect, settings, ConfigEditor. |
| `admin-frontend/src/components/frontier/CrawlRequests.tsx` | Extracted from FrontierDrawer lines ~1246–1281: status filter + paginated + searchable table + bulk approve/decline. |
| `admin-frontend/src/components/frontier/CrawlRequests.test.tsx` | Status filter, pagination, bulk actions, confirm dialog wiring. |
| `admin-frontend/src/components/frontier/DomainTables.tsx` | Extracted failed/queued domain rollups + drill-downs (FrontierDrawer ~825–1244) onto DataTable. |
| `admin-frontend/src/components/frontier/WorkersTable.tsx` | Structured workers list replacing concatenated `break-all` line (FrontierDrawer ~794–823). |
| `admin-frontend/src/components/analytics/FeedbackCard.tsx` | New card wired to `GET /admin/feedback/summary` (currently unused surface). |
| `admin-frontend/src/components/analytics/ThroughputTable.tsx` | Keyboard-accessible data-table fallback for ThroughputChart. |

### 1.2 Change (existing files)

| File | Change |
|---|---|
| `admin-frontend/src/App.tsx` | Tab semantics (§3.1), hero/TokenBar layout (§3.2), hoist-friendly QueryClient removal (see main.tsx). |
| `admin-frontend/src/main.tsx` | Single `QueryClientProvider` (merge duplicated FrontierDrawer ~1680 + AnalyticsPanel ~195 configs). |
| `admin-frontend/src/components/FrontierDrawer.tsx` | Decompose (fleet header, DomainTables, CrawlRequests, inspect, settings); ConfirmDialog everywhere; DataTable adoption; pause/resume disabled states; live/stale toast; per-section error panels; heading ladder; pager label fixes; remove-button labels; settings dirty/revert (§3.3). Shrinks from 1693 lines toward <600 (re-exports + composition). |
| `admin-frontend/src/components/AnalyticsPanel.tsx` | Zero-flash loading, per-card errors, chart a11y, window-vs-range fix, sort controls, filter wiring, pager totals, heading ladder (§3.4). |
| `admin-frontend/src/components/ConfigEditor.tsx` | Dirty tracking + revert, read-only lock, key search, inline JSON error location, primary Save (§3.5). |
| `admin-frontend/src/components/TokenBar.tsx` | Hint text separation, show/hide toggle, Forget → Button system (§3.2). |
| `admin-frontend/src/components/Tour.tsx` + `tourSteps.ts` | Viewport clamp, focus trap, honest progress count, healthy-fleet step-5 fallback (§3.7). |
| `admin-frontend/src/components/ui/Toast.tsx` | Single live region + queue/coalesce, opaque surface, labeled dismiss (§3.8). |
| `admin-frontend/src/components/ui/Drawer.tsx` | Trigger weight, stable `aria-label` prop (drop regex derivation), full Escape handling, closed-state summary (§3.6). |
| `admin-frontend/src/components/ui/Badge.tsx` | Keep for counts only; state moves to StatusBadge. |
| `admin-frontend/src/components/ui/Button.tsx` | Add `destructive` variant; keep `ghost` for tertiary only (fix Flush-vs-Block inversion). |
| `admin-frontend/src/components/ui/index.ts` | Export new primitives; remove Chip/Select/Skeleton/TextInput only if deleted (see §1.3 — keep Skeleton, it gets used now). |
| `admin-frontend/src/index.css` | Tab responsive scroll, pager/row density, hero wrap fix, type-scale tightening; prune user-frontend rules only (§5.4). |
| `admin-frontend/src/api/client.ts` | Add `fetchFeedbackSummary` usage already defined (~line 780) to FeedbackCard; add crawl-request status param passthrough; keep fail-open-reads/throwing-mutations + `SearchApiError` + single `X-Admin-Token` gate; delete dead user-search block per §1.3. |
| `admin-frontend/src/api/guard.ts` | Delete dead search block (keep guards actually imported by admin paths). |
| `admin-frontend/src/api/types.ts` | Scope to admin-used shapes or re-export generated schemas; do not extend hand mirror. |

### 1.3 Delete (dead code — all verified zero-import by inventory)

- `admin-frontend/src/api/client.ts` user-search block only: `validateQuery/buildSearchBody/searchLens/totalPages/offsetForPage/pageForOffset/SUGGEST_DEBOUNCE_MS/debounce/fetchSuggestions/sendFeedback/prefetchPage/DEFAULT_LIMIT/MAX_QUERY_LENGTH/SearchOptions/SuggestOptions`. Keep `SearchApiError/API_BASE_URL`/frontier-admin fns.
- `admin-frontend/src/api/guard.ts` search block: `isSearchResult/isQueryProvenance/isDiagnostics/isCacheStatus/isAiCitation/isProviderPrefs/isSearchResponse/isSuggestResponse/readSuggestions/readRelated/readSpelling/readNextOffset` (client.ts imports of `isSearchResponse/isSuggestResponse` die with the client block).
- Legacy wrappers `fetchFrontierStatus/fetchFrontierSettings/fetchFrontierWorkers` — drawer loads via generated client (`loadFrontierSnapshot`); only `client.test.ts` references them. Delete and update `client.test.ts`, or rewire drawer to them — prefer deletion (generated client is the live path).
- `admin-frontend/src/components/ui/Chip.tsx`, `Select.tsx`, `TextInput.tsx` — zero imports. Delete files + drop from `ui/index.ts`. Keep `Skeleton.tsx` — this revamp finally uses it (do NOT delete).
- `index.css` user-frontend rules (`.ol-shell/.ol-header/.ol-landing/.ol-search-form/.ol-result*/.ol-pagination` etc.) and `GlassPanel` header/sidebar variants (used: hero in App, rail in FrontierDrawer only). Prune unreferenced rules after a `muse.search` for each class name.
- Do NOT delete: `POST /frontier/workers/heartbeat` UI-less (worker-originated, correct), `GET|PUT /prefs` (non-admin surface), kafka/cluster generated schemas (kept for future cluster view; just don't build the view now).

### 1.4 Merges (pre-steps, do first)

1. Hoist `QueryClient`: create one in `main.tsx` with the identical `retry:false`/no-refocus config, delete both per-panel providers.
2. Unify crawler defaults: `FrontierDrawer DEFAULT_SETTINGS` (max_queued_urls 1000, depth 3) vs `App FILE_DEFAULTS.indexing` (10000, depth 2) → single `configs/`-adjacent constant imported by both.
3. Types: hand `types.ts` mirror validated against `src/api/generated/schemas`; drop duplicates for orval-covered routes.

## 2. New shared primitives (props)

### 2.1 `DataTable<T>` (`ui/DataTable.tsx`)

```tsx
interface Column<T> {
  key: string; header: string; sortable?: boolean;
  render: (row: T) => React.ReactNode; sortValue?: (row: T) => string | number;
  align?: 'start' | 'end';
}
interface DataTableProps<T> {
  rows: T[]; columns: Column<T>[]; rowKey: (row: T) => string;
  search?: { value: string; onChange: (v: string) => void; placeholder: string; ariaLabel: string; keys: (keyof T | ((row: T) => string))[] };
  sort?: { value: string; direction: 'asc' | 'desc'; onChange: (key: string, dir: 'asc'|'desc') => void };
  pagination: { page: number; pageSize: number; total?: number; onPage: (p: number) => void }; // total unknown → show "Page N" + disable Next on short page (existing backend limit/offset behavior)
  selection?: { selected: string[]; onChange: (keys: string[]) => void; label: (n: number) => string };
  empty?: { title: string; hint?: string }; error?: string | null; loading?: boolean;
  ariaLabel: string; // table + each pager ("Failed domains pager", never bare "Go to")
}
```

Rules: client-side search/sort/filter over the fetched page (backend list endpoints take only
limit/offset — routes.py 1475–1634 — so server-side search/sort is a backend follow-up, not this
spec); server offset paging preserved via `pagination.onPage`; `aria-sort` on sortable `th`;
checkbox column with header select-all + per-row labels; loading renders `Skeleton` rows
(not zeros); error renders inline panel with retry, never `DEFAULT_SETTINGS`-as-real.

### 2.2 `SearchInput` — `{ value, onChange, placeholder, ariaLabel, debounceMs?: 150 }` with built-in
clear button; error text pairs via `FieldError`.

### 2.3 `ConfirmDialog` — `{ open, title, body, confirmLabel, cancelLabel?, tone: 'destructive'|'default', onConfirm, onCancel }`;
focus-trapped `role=alertdialog`, Escape cancels, confirm autofocus only when destructive.
Replaces every `window.confirm` (FrontierDrawer lines 412/516/533/558) and adds confirmations
where missing: pause, resume, crawl-request approve/decline, settings save, config save.
Requeue/skip/priority-bump stay one-click (reversible, non-destructive) but toast with Undo hint
where trivially possible; destructive flush/purge/block/clear/auto-triage always confirm.

### 2.4 `EmptyState` — `{ title, hint?, action?: React.ReactNode }`; `StatusBadge` tones map to
existing `--color-ol-*` tokens (no new hues).

## 3. Per-view changes

### 3.1 App shell / tabs (`App.tsx`, `index.css` .ol-admin-tabs)

- `nav aria-label="Admin sections"` → `role=tablist`; tab buttons → `role=tab aria-selected`
(keyboard: Left/Right/Home/End move + activate; keep click). Fixes `aria-pressed`-as-tabs (105–118).
- Active tab: keep slate wash + add underline bar (2px current-color) so state isn't weight-only.
- Tabs container: `overflow-x:auto` + `white-space:nowrap` instead of wrap-with-dividers; fixes 360px stacking.
- `main.ol-admin`: add `max-width: 72rem; margin-inline:auto; padding-inline: 1rem`.
- 8 tabs unchanged (frontier/analytics/models/providers/ranking/query/plugins/indexing).

### 3.2 Hero / TokenBar (`App.tsx` 86–103, `TokenBar.tsx` 39–70)

- Label `Admin token` + separate `<p class=hint>` for "(required only when …)"; `type=password`
with show/hide toggle button (`aria-pressed`, "Show token"/"Hide token").
- Forget → `Button variant=ghost size=sm` (keeps system hit target ≥32px).
- Hero wrap: token group `flex: 1 1 320px; max-width:560px`, Tour button `flex:none` so CTA order never flips.

### 3.3 Frontier (`FrontierDrawer.tsx` → composition + `frontier/*`)

- Layout: keep single rail; impose heading ladder `h2` per workflow area
(Fleet status / Failed URLs / Needs attention / Queued by domain / Crawl requests / Seeds /
Inspect / Settings); `<details>` summaries become real headings (`aria-labelledby`).
- Fleet header: Pause/Resume disabled from `status.paused` + inline state hint
("Paused — queue-growth halted" / "Running"); `Pause & clear` → `Button variant=destructive`.
- Status: `StatusBadge tone=running|paused|live|stale` for Running/Paused/Live-Snapshot; counts stay gray Badge.
- Live→Snapshot flip toasts once ("Live feed lost — showing snapshot…", 359–364 path).
- Loading: `Skeleton` rows + `aria-live=polite`; snapshot error → inline error panel with Retry,
never defaults-as-real (301–310 path).
- Failed URLs list (889–944): DataTable (client search over URL/last-error, sortable Last-seen/Attempts,
jump-pager labeled "Failed URLs pager"). Same for failed-domains (5-button rows 972–1068) and
queued-domains (1115–1242): DataTable + selection checkboxes → bulk Requeue/Flush/Skip/Block and
bulk priority ±; per-row buttons keep single-domain handlers (`handleDomainAction/handleQueuedPriority`
pass `[domain]`) as the degenerate case. Priority +/- stay `secondary`, Skip/Block move to
`secondary` too (fix ghost-inversion: destructive actions share weight), with `destructive` tone
on Flush/Block/Purge.
- Crawl requests (`CrawlRequests.tsx`): status tabs Pending/Approved/Declined
(`listCrawlRequests(token, status)` — backend already supports it, UI hardcodes 'pending' line 180);
paginated (pageSize 20, backend caps 200); searchable (requester/URL); bulk approve/decline via
selection + ConfirmDialog; per-row approve/decline confirm too (currently toast-only 382–395).
- Workers (`WorkersTable.tsx`): columns Worker / Claimed / Crawl / Rate / Totals / Discovery;
no `break-all` on IDs; numeric columns `align=end`, tabular-nums.
- Scroll: one page scroll; inner lists become DataTable bodies (max 10 visible rows + pager),
keeping at most one `max-h-*` box (queue diagnostics). Every `<ul>` gets `aria-label`.
- Inspect result (1349–1394): `h4` subheads Normalized/Queue/Indexed/Links + labeled link list.
- Settings (1395–1664): dirty indicator ("Unsaved changes") + sticky footer Save/Revert;
Save → ConfirmDialog ("Push settings to fleet?"); numeric inputs get `min/step`
(Priority `min=0` unless backend documents negatives — currently accepts negatives silently);
skip/allow/deny/feed `×` buttons get `aria-label="Remove {value} from {list}"`;
seeds-priority parse errors (438–442/463–467) set `FieldError` + `aria-invalid`, not toast-only.
- Kafka/cluster: no new view; keep `kafka_enabled`/lag text (718–770). Uncovered
`cluster-map`/`kafka/nodes`/`rebalance-plan` endpoints stay unused — documented follow-up.

### 3.4 Analytics (`AnalyticsPanel.tsx`)

- Zero-flash: while `anyLoading`, KpiCards render `Skeleton`, never `0`; deltas render `—` until loaded (348–369 path).
- Errors: per-card error slot; global `role=alert` banner only when counts+series both fail (not one breakdown).
- Deltas: keep color + add `▲/▼/—` marker + `aria-label="up 3 points"` (color never sole channel); legend explains `pp` vs `%`.
- ThroughputChart (115–180): bars become `button`-ish focusable with `aria-label="{bucket} {fetched} fetched {failed} failed"`;
add `ThroughputTable` toggle (chart/table, default chart); cap bars at 48 buckets (aggregate hours→days past that);
legend swatches `aria-hidden`; x-axis ≥5 labels.
- Window vs range: selecting a Window clears From/To and vice versa; Window shows "(custom range)" option
when From+To set; From>To → `FieldError` "Start must be before end".
- Lists: every ranked list gets sort control (Count A–Z/Z–A + value desc); failing-domains gains the same
domain-contains search box top-domains has (wired to `match`); reasons/outcomes gain offset paging
like domains (backend `top` supports offset already); pagers show "Page N" + disable-Next-on-short-page
(existing no-totals behavior) and empty text "No domains on this page".
- Headings: section `h2 Analytics`, blocks `h3`; outcomes/reasons empty states share the domains empty component.
- New `FeedbackCard`: `fetchFeedbackSummary` (client.ts ~780, currently uncalled) → totals by action;
fallback to analytics-counts text when endpoint 404s. Closes the unused-surface gap without inventing an API.
- Grid: KPI `grid-cols-2 sm:grid-cols-3`; card padding matches `ol-dash-group` rhythm.

### 3.5 Config editors (`ConfigEditor.tsx` 104–136)

- Load-race: textarea `disabled` + Skeleton until `loaded`; typed-during-load no longer possible (44–50 path).
- Read-only preview: set `readOnly` on the textarea whenever the preview toast shows it (41–67 path).
- Save → `Button variant=primary` + ConfirmDialog ("Replace {doc} on backend? Whole-document write.");
dirty dot + Revert button (restore last-loaded body).
- Key filter: `SearchInput` filters top-level keys (non-matching lines dimmed, count "3 of 42 keys");
JSON errors render inline with line number (`aria-invalid`, `FieldError`), still toasted at Save.

### 3.6 Drawer primitive (`Drawer.tsx` 18–44)

- Trigger → `Button variant=secondary size=sm` with `aria-expanded`; accept explicit `label` prop,
drop regex-strip derivation (lowercase-fragment labels).
- Escape: global keydown while open (not section-only `onKeyDown`); closed panel renders one-line
summary ("Frontier hidden — 3 failed domains need attention") instead of empty rail.

### 3.7 Tour (`Tour.tsx` 244–289, `tourSteps.ts` 12–48)

- Tooltip: `max-width:min(22rem, calc(100vw - 2rem))`, `max-height:60vh; overflow:auto`, full viewport clamp.
- Veil: `pointer-events:auto` + focus trap while open (background inoperable, matching `role=dialog`).
- Progress: "Step {visibleIndex} of {visibleTotal}" computed after skip; step 5 (`frontier-auto-triage`,
mounts only when `failedDomainTotal > 0`) gains `healthyFallback: 'frontier-needs-attention'`
so healthy fleets still tour triage context.

### 3.8 Toasts (`Toast.tsx` 18–52)

- One `aria-live=polite` region (errors `role=alert` inside it); queue max 3, coalesce repeats
("Saved ×3"); opaque surface (`bg-[#101828]` solid); dismiss `×` at full-paper color + `aria-label` kept.

## 4. API wiring (regen via orval, never hand-edit generated output)

- `npm run api:gen` after any backend change; commit `src/api/generated/**` + `openapi/README.md`.
- This revamp needs no schema change: crawl-request status filter, analytics `match`/`offset`,
feedback summary already exist in `src/api/generated/openlens.ts` (~43 endpoints).
- `client.ts` additions are thin wrappers only: `listCrawlRequests(token, status)`
passthrough, `fetchFeedbackSummary` reuse; keep fail-open reads / throwing mutations /
`frontierAdminHeaders` single-gate conventions.
- Correction (verified against `src/openlens/api/routes.py` + generated
`frontierListCrawlRequests…Params`): the list endpoint accepts only
`status` — no `limit`/`offset`. Pagination/search over crawl requests
stay client-side in `CrawlRequests.tsx` (pageSize 20); server params are
a backend follow-up, not this revamp.
- Backend follow-ups (explicitly out of scope, listed for the next plan): `limit/offset`-only
frontier lists gaining `q/sort/filter` params; analytics `top` totals; config per-key PATCH.

## 5. Constraints (hard — reviewer checklist)

1. No purple/indigo/violet accents or gradients; no gradient headline text.
2. No full-page cream background (keep flat white-card/slate Task-18 theme + `--color-ol-*` tokens).
3. No glow-everywhere cards; no glassmorphism beyond existing hero/rail GlassPanel use.
4. Typography: system stack (Tailwind default), never Inter-only; body text never gray-400/500 —
use existing `--color-ol-ink*` tokens; impose real hierarchy (hero > h2 section > h3 block > body/hint).
5. No identical 3-card icon grids; no single-radius-everywhere (keep existing radii per component);
no `hover:scale` cards; no emoji icons (CSS dot shapes only); no left-border accent cards.
6. `prefers-reduced-motion` respected for any added transition (match index.css 427–441/739–746 pattern).
7. Tailwind v4 pattern kept (`@import "tailwindcss"`, `@theme` tokens); legacy `--ol-*` classes
reused, not duplicated.
8. Typed error guards (`guard.ts` readers), no `any` leaks, no barrel imports beyond existing `ui/index.ts`.

## 6. Test updates (colocated vitest — `npm test` = `vitest run`)

- New: `DataTable.test.tsx`, `SearchInput.test.tsx`, `ConfirmDialog.test.tsx`, `CrawlRequests.test.tsx`.
- Update: `FrontierDrawer.test.tsx` (bulk selection, confirm-dialog mocks replacing `window.confirm`,
pause/resume disabled states, skeleton-vs-defaults); `AnalyticsPanel.test.tsx` (skeleton-not-zero,
per-card error, chart table toggle, window/range exclusivity); `App.test.tsx` (tablist semantics +
arrow keys; assert all 8 tabs incl. indexing — fixes the 7-vs-8 gap); `client.test.ts` (drop legacy
wrapper imports; cover status-param passthrough + feedback summary).
- Add: `ConfigEditor.test.tsx`, `TokenBar.test.tsx` (show/hide, hint text, Forget),
`guard.test.ts` (currently zero coverage) — all colocated.
- Keep: `Logo.test.tsx`, `Tour.test.tsx` (+ focus-trap/viewport cases), `ui/Toast.test.tsx` (+ coalescing).
- Accessibility: `axe-core` (already a devDep) assertions on App + each new primitive; no new runner needed.

## 7. Docs updates

- `admin-frontend/openapi/README.md`: regenerated with `api:gen` output (orval flow unchanged).
- `docs/analytics-events.md`: document FeedbackCard source (`feedback/summary` with analytics-counts fallback).
- Plan siblings `docs/plans/kafka-queue.md`, `docs/plans/redis-frontier.md`: link-only touch if queue
semantics referenced; no rewrites.
- This spec file is the revamp record; implementation PRs check off §1–§6 boxes.

## 8. Build / verify order

1. Merges (§1.4) + deletions (§1.3) — `npm test`, `npm run build`, `npm run lint`.
2. Primitives (`DataTable/SearchInput/ConfirmDialog/StatusBadge/EmptyState/FieldError`) + their tests.
3. Frontier decomposition → Analytics → ConfigEditor → TokenBar/Drawer/Tour/Toast.
4. Full gate: `npm test`, `npm run build`, `npm run lint`, `npm run format:check`, axe assertions green.
5. Manual pass: keyboard-only run (tabs/arrows, dialogs, pagers), 360px + 1440px widths,
healthy-fleet tour, live-drop toast, reduced-motion on.

# Enterprise UI Plan

## Goal

Evolve the skeleton frontend (`frontend/`, task 2) into an enterprise-grade
search UI that integrates every backend feature from tasks 8–15 and 17 as
they land — search modes and filters, ranking transparency, federated
providers, AI enrichments, and diagnostics — with professional UI/UX
(accessible, responsive, consistent design system) and all frontend quality
gates green.

Falsifiable bar: for each backend feature listed in Scope, the UI exposes it
through the existing HTTP contract only; every surface has component tests;
keyboard-only and screen-reader checks pass on the search flow; and
`npm run lint`, `npm run format:check`, `npm test`, `npm run build`, and
`npm audit --omit=dev` all pass (the CI `frontend` job).

## Reality baseline (verified 2026-10-28 against the tree)

- `frontend/` is a standalone Vite + React + TypeScript app; the ONLY
  coupling to the backend is the HTTP contract mirrored by hand in
  `frontend/src/api/types.ts` (never imported from `src/openlens/`).
  `guard.ts` validates payloads at runtime; `client.ts` calls
  `POST /api/v1/search` (same-origin `/api/` proxy in Docker, CORS in dev).
- Current UI is a skeleton: `App.tsx` (~116 lines), 8 components
  (`SearchForm`, `ModeSelector`, `FilterToggle`, `ResultCard`, `ResultList`,
  `AppliedFilters`, `DiagnosticsDrawer`, `States`), `index.css` is 14 lines —
  no design system, no tokens, no accessibility pass yet.
- The wire contract today (`types.ts`): query/mode/limit/offset/filters;
  results carry `original_score` + `final_score`, `reasons`, `warnings`,
  free-form `metadata`; diagnostics carry `fallback_used`,
  `provider_latencies_ms`, `plugin_executions`, `errors`. New backend fields
  (signals, provider prefs, AI labels, `request_id`, …) arrive only when
  tasks 8–15 land — this plan mirrors them into `types.ts` per feature,
  never ahead of the backend schema.
- There is NO admin/mutation API in the backend (indexing is T8's indexer
  entry point and T15's CLI). The UI therefore gets no crawl/reindex/config
  controls in this task — see Non-goals.
- Quality gates already wired in CI (`.github/workflows/ci.yml`, `frontend`
  job): `npm ci`, `npm run lint` (ESLint), `npm run format:check`
  (Prettier), `npm test` (Vitest), `npm run build` (`tsc && vite build`),
  `npm audit --omit=dev`. This task must keep every one green and adds
  accessibility checks to the test layer (step 2).

## Scope (one connected surface: the enterprise search workspace)

Sequenced after tasks 8–15/17 in row order; each numbered surface is built
only once its backend dependency has landed (feature-detect from the
response, never assume):

1. **Design system + app shell.** CSS design tokens (color, spacing,
   typography, radius, elevation) in `index.css`; shared primitives
   (button, input, select, chip, card, drawer, badge, skeleton, toast)
   under `components/ui/`; enterprise layout (header with product identity,
   primary search workspace, collapsible diagnostics rail); light theme,
   responsive breakpoints (desktop/tablet/mobile), visible focus states.
2. **Accessibility + UX quality.** WCAG 2.1 AA targets: semantic landmarks,
   labelled controls, full keyboard operability (search, filters, pagination,
   drawer), focus management on route/result updates, `aria-live` result
   announcements, contrast-checked tokens, reduced-motion respect. UX
   states for every surface: loading (skeletons), empty, error (envelope
   `code`/`message`, never raw JSON), partial (fallback/degraded badges).
3. **Search workspace.** Query input with mode selector (existing modes);
   intent/mode suggestion chip when T9 diagnostics provide one (explicit
   mode always wins); advanced-filters panel generated from the T10 filter
   schema (user restrictions, block/allow indicators) including the
   consumer keys `prefer_*`, `max_price`, `location`, `facilities`;
   applied-filter chips with per-filter attribution (T10
   `applied_filters`); server-side pagination controls bound to
   `limit`/`offset`/`total`.
4. **Result transparency.** Result cards show title/snippet/url, provider
   badge, `final_score` with an expandable score breakdown (original vs
   final score, T11 `signals` — trust/freshness/preference/relevance),
   `reasons` and `warnings` (trust warnings visually distinct), T12
   diversity/story grouping (group headers by `story_id`, commerce
   attributes: price, facilities), T14 AI summary/perspectives block
   clearly labelled with `ai_model` (AI content is marked as AI-generated,
   absent entirely when AI is disabled).
5. **Federation + diagnostics.** Provider-preference control (T13
   `provider_prefs`, only enabled providers offered); per-result
   `also_from` provider badges; diagnostics drawer upgraded to a timeline:
   `request_id` (T15, copyable), provider latencies + result
   counts/estimated costs (T13), plugin executions with stage/status/
   latency, fallback indicator, error list. Raw metadata stays behind an
   explicit “developer details” disclosure — end users see plain language.
6. **Contract + tests.** Every new field is mirrored in
   `frontend/src/api/types.ts` and validated in `guard.ts` in the same
   change that first renders it; fixture tests extend
   `response.fixture.test.ts`; each surface ships Vitest component tests
   (render, interaction, states); the gated live smoke test
   (`OPENLENS_SMOKE_URL`) is extended to the new surfaces.

## Steps

1. Design tokens + `components/ui/` primitives + app shell; migrate the 8
   existing components onto primitives without behavior change. Tests:
   existing `App.test.tsx` stays green; snapshot-free render tests for
   primitives.
2. Accessibility pass + test wiring: add `axe-core` (dev only) checks in
   Vitest for the search flow (no critical violations), keyboard-navigation
   tests (tab order, Enter/Escape in drawer/panel), `aria-live`
   announcements; manual screen-reader checklist recorded in the PR.
3. Search workspace: advanced-filters panel driven by a typed filter-schema
   module mirroring T10 keys; suggestion chip (T9); pagination. Tests per
   control (set/clear filters → request body asserted via client mock).
4. Result transparency: score-breakdown disclosure, signals bars/labels,
   story-group headers, commerce attributes, AI block with `ai_model`
   label. Tests with fixture responses per backend shape; AI-absent
   fixture proves no AI chrome renders.
5. Federation + diagnostics rail: provider prefs control, `also_from`
   badges, request-id/latency/cost/plugin timeline. Tests: unknown or
   missing diagnostics keys degrade gracefully (guard rejects malformed,
   UI never crashes on absent optional keys).
6. Hardening: bundle budget check (initial chunk stays within the budget
   set in step 1; lazy-load the diagnostics rail), responsive pass at
   360/768/1280px, error-envelope mapping for every backend `code`
   (`config_error`, `plugin_error`, validation) to plain-language UI
   states. Full gate run + Docker stack check (`/api/` proxy path).

## Validation Plan

- `npm --prefix frontend run lint`, `npm --prefix frontend run format:check`,
  `npm --prefix frontend test`, `npm --prefix frontend run build` all green;
  `npm --prefix frontend audit --omit=dev` with no unacknowledged findings.
- Accessibility: axe checks in `npm test` report zero critical violations on
  the search flow; keyboard-only walkthrough (search → filter → open
  diagnostics → paginate) completable without a mouse.
- Live check against the Docker stack: each landed backend feature (per
  Scope dependency) is visible and correct in the UI with real responses;
  with AI disabled and with a single provider, the UI shows the degraded
  states, not errors.
- Backend untouched: `git status` shows changes only under `frontend/`
  (plus `tasks_status.md`); backend suite unaffected.

## Scalability notes

| Bottleneck | Breaks at | Fix shape (folded in or flagged) |
|---|---|---|
| Rendering unbounded result lists | 100+ hits/page | Server-side pagination only; render at most `limit` cards; no client-side accumulation across pages (in scope) |
| Bundle growth from per-feature surfaces | Each new surface | Lazy-load diagnostics rail + admin-style disclosures via dynamic `import()`; bundle budget enforced in step 6 (in scope) |
| Re-render churn on keystroke/filter changes | 10x QPS of UI interactions | Debounced query submit (explicit search action, no per-keystroke requests); memoized result cards (in scope) |
| Diagnostics payload size (plugin executions, metadata) | Federated + AI on | Drawer fetches nothing extra — it renders the already-returned response; raw metadata behind collapsed disclosure, not parsed eagerly (in scope) |
| Long sessions / memory | Hours-long use | No client-side result caching beyond the current response; state resets per search (in scope) |

Rewrite-forcing risk (flagged, not built): multi-user tenancy, auth/RBAC,
saved searches, and personalization need backend identity/session APIs that
do not exist — they are future backend+UI plans, not this task.

## Non-goals

- Any backend change (`src/`, `configs/`), and any UI field that the backend
  does not yet return (no speculative rendering ahead of tasks 8–15/17).
- Admin mutations: crawl/reindex/config editing from the UI (no backend API
  exists; T8/T15 own those surfaces via indexer/CLI).
- Secrets or config in the frontend: no API keys, endpoints, or provider
  config ever reach the browser (backend proxy only).
- Auth/login, multi-tenancy, saved searches, personalization (no backend
  identity surface — flagged above).
- SSR, a component-library dependency, or a framework migration: stay on
  Vite + React + TypeScript with hand-rolled primitives.

## Dependencies

- Backend row order: T8 (index status shapes) → T9 (`query` diagnostics) →
  T10 (filter schema + `facilities`/`prefer_*` keys) → T11 (`signals`) →
  T12 (`story_id`, commerce attributes) → T13 (`provider_prefs`,
  `also_from`, provider counts/costs) → T14 (`ai_summary`,
  `ai_perspectives`, `ai_model`) → T15 (`request_id`) → T17 (no UI surface;
  backend-only). Implement this task last (row 18); surfaces for unlanded
  backend features stay hidden, never stubbed with fake data.

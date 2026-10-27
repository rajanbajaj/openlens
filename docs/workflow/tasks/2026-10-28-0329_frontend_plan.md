# OpenLens — Frontend Plan

## Goal

Build the OpenLens web frontend as an app independent of the backend: its own
directory, toolchain, and dependencies. The only coupling to the backend is the
HTTP contract in `src/openlens/api/schemas.py` (`POST /api/v1/search`), mirrored
as TypeScript types. No shared code, no backend imports, no secrets in frontend
code or config.

## Placement and Stack

- Lives in `frontend/` at the repo root (monorepo, separate app).
- Vite + React + TypeScript, `npm` toolchain; `vitest` + Testing Library.
- Backend stays untouched: if the frontend needs a contract change, it reports
  the need instead of editing `src/`.

## Contract (from `src/openlens/api/schemas.py`, mirrored, not imported)

- `POST {API_URL}/api/v1/search` with `{query, mode, limit, offset, filters}`.
  Query: 1–1000 chars, non-blank. Mode: `default` plus `child-safe`, `news`,
  `shopping`, `travel`. Limit 1–100, default 10; offset ≥ 0.
- Response: `{query, mode, results[], total, applied_filters[], diagnostics{fallback_used, provider_latencies_ms, plugin_executions, errors}}`.
- Result: `{id, title, snippet, original_score, final_score, url?, provider, reasons[], warnings[], metadata{}}`.
- Errors: `{error: {code, message}}` envelope on every failure.

## Steps

1. **Scaffold.** Vite React-TS app in `frontend/`; `VITE_OPENLENS_API_URL`
   env config (dev default `http://localhost:8000`); TS interfaces mirroring
   the contract above; runtime response guard for the envelope shape.
2. **Search experience.** Query input with validation (non-blank, ≤1000 chars),
   mode selector, one filter toggle (e.g. exclude sponsored → `filters` dict),
   paginated search with `limit`/`offset` and pre-pagination `total`.
3. **Results.** Title link, snippet, final score, expandable `reasons` /
   `warnings`, `applied_filters` line, empty-result and error-envelope states.
4. **Diagnostics drawer.** Collapsible dev panel rendering `diagnostics`
   (fallback flag, per-provider latency, plugin executions, errors).
5. **Tests.** Vitest suites with mocked `fetch`: request shaping, response and
   error-envelope parsing, pagination math, mode/filter wiring. One smoke test
   against a live backend (skipped without `OPENLENS_SMOKE_URL`).

## Validation Plan

- `npm run build` clean (typecheck included), `npm test` green offline.
- Manual: point at the backend, run one shopping-mode query, confirm results
  show reasons, applied filters, and diagnostics; stop the backend and confirm
  the error envelope renders instead of a crash.
- Contract proof: change nothing in `src/` during frontend work; paste one real
  backend response into the fixture test and keep it parsing.

## Non-goals / Risks

- No auth, no SSR, no admin UI in this plan.
- Backend contract drift is the top risk: if `schemas.py` changes, frontend
  types must be re-mirrored by hand; the fixture test is the tripwire.
- Open questions: none.

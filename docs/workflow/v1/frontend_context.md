# OpenLens Frontend Build — Shared Agent Context (workflow v1)

Every agent in this workflow inherits these conventions. They are binding.

## Sources of Truth

- `docs/workflow/tasks/frontend_plan.md` — what to build and why.
- `src/openlens/api/schemas.py` — the HTTP contract. Mirror it, never import
  it: no backend code, types, or tooling in `frontend/`.

## Code Rules

- Vite + React + TypeScript, `npm` toolchain. `npm run build` must include a
  strict typecheck; no `any` leaks at the API boundary.
- The API base URL comes only from `VITE_OPENLENS_API_URL`
  (dev default `http://localhost:8000`). Never hardcode hosts, never embed
  secrets or API keys anywhere in `frontend/`.
- Fetch lives in `src/api/client.ts` only; components take typed props.
- Backend contract changes are out of scope: if `schemas.py` disagrees with
  the plan, report it instead of editing `src/`.

## File Ownership

- Write ONLY files listed in your brief. `src/` (backend) is read-only for
  every frontend agent.
- If you need a change outside your list, report it in your result summary
  instead of making it — the integrate agent resolves cross-cutting fixes.

## Tests

- Vitest + Testing Library, `fetch` mocked. Offline green is the bar; the
  live-backend smoke test runs only with `OPENLENS_SMOKE_URL` set.

## Result Format

End with a short summary: files written, tests run with exact commands and
outcomes, deviations from your brief, and anything the integrate agent must
know.

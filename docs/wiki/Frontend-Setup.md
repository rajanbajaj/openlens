# Frontend Setup (developers)

Requires Node.js 20 and npm. Two apps share the toolchain
(same scripts, Tailwind tokens, per-app orval output):

| App | Dev | Compose |
|---|---|---|
| User search (`frontend/`) | `npm ci && npm run dev` | port `3000` |
| Admin (`admin-frontend/`) | `npm run dev -- --port 5174` | port `3001` |

The dev server talks to `VITE_OPENLENS_API_URL` (default
`http://localhost:8000`); production builds use same-origin `/api/`.

## Commands (per app)

`npm test` (vitest) · `npm run lint` (eslint) ·
`npm run format:check` (prettier) · `npm run build` (`tsc` + vite) ·
`npm run api:gen` (orval — run after any backend route/schema
change and commit the output).

## Conventions

- Components colocate with `*.test.tsx`; behavior first, roles and
  labels asserted, fetch stubbed per test.
- New wire fields are additive: extend `api/types.ts`, narrow in
  `api/guard.ts` with degrade-to-absent, never crash on malformed
  envelopes (see `response.fixture.test.ts`, re-captured from a real
  backend envelope).
- No gradients, no emoji iconography; icon buttons carry labels;
  autofocus on the landing hero only.

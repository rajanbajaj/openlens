# AGENTS.md — OpenLens

> Scope: this file guides coding agents working in this repo.
> Precedence: human instructions > nearest nested AGENTS.md > this root file.
> Keep this file lean and principle-based; route long-form detail to linked docs.
> Do not copy version numbers, counts, or endpoint lists here — they go stale.
> Resolve pinned versions from the cited config files instead.

## 1. Read-first ladder

- Start with `README.md` for setup, config, and storage overview.
- Then `docs/wiki/Contributing.md` for contributor workflow and edge coverage.
- Then `docs/product-development-best-practices.md` for lifecycle and done-gates.
- Then `docs/wiki/Testing-and-CI.md` for exact gate commands.
- For provider work, also read `docs/openlens/custom-search-providers.md`.
- For category work, also read `docs/openlens/adding-new-category.md`.
- For analytics work, also read `docs/analytics-events.md`.
- Read interfaces, tests, and callsites before editing (`docs/wiki/Contributing.md`).

## 2. Repo map (stable pointers)

- Backend: `src/openlens/api/` (thin routes, schemas, error mapping) + `src/openlens/core/` (dataclass contracts, engine interfaces) (`src/openlens/core/interfaces.py`, `src/openlens/api/errors.py`).
- Tests: backend `tests/unit/` + `tests/integration/` (`pyproject.toml` testpaths); frontend colocated `*.test.ts(x)` beside modules (`frontend/vite.config.ts`).
- Frontends: `frontend/` and `admin-frontend/` each own `package.json`, `orval.config.ts`, `vite.config.ts`, `eslint.config.mjs`, `tsconfig.json`.
- Docs: `docs/wiki/`, `docs/workflow/`, `docs/openlens/`, `docs/analytics-events.md`.
- CI: `.github/workflows/ci.yml` (backend, frontend, secrets jobs); commit gates in `docs/workflow/commit.md` + `docs/workflow/commit-hook.sh`.

## 3. Setup and build

- Backend deps via `uv sync` groups declared in `pyproject.toml`; never use system python (`.github/workflows/ci.yml`).
- Frontend install and build via scripts in `frontend/package.json` (`tsc && vite build`); same shape in `admin-frontend/package.json`.
- Regen the API client with the `api:gen` script (`frontend/orval.config.ts`); never hand-edit generated output.
- Prefer the narrowest documented command (e.g. per-app script) over repo-wide runs.

## 4. Testing

- Targeted-first: run the smallest affected suite, then widen to the full gate.
- Backend: pytest with offline fakes (`docs/wiki/Testing-and-CI.md`, `.github/workflows/ci.yml`).
- Frontend: `vitest run` colocated tests (`frontend/package.json`, `frontend/vite.config.ts`).
- Bug fixes start from a failing regression test that passes only with the fix.
- Rebuild or regen before retesting when tests resolve built or generated output.

## 5. Lint, format, types

- Backend: `ruff check`, `ruff format --check`, `mypy src` per `pyproject.toml` + `.github/workflows/ci.yml`.
- Frontend: `eslint`, `prettier --check`, `tsc` build per `frontend/package.json` + `frontend/eslint.config.mjs` + `frontend/.prettierrc` + `frontend/tsconfig.json`.
- Keep the full backend + frontend + secrets gates green before declaring done.
- Smallest root-cause fix; cover edges; do not weaken tests to pass.

## 6. Code conventions

- Backend errors use one envelope branched only on `error.code` (`src/openlens/api/errors.py`); typed taxonomy in `src/openlens/core/interfaces.py`; fail closed on plugin/config errors.
- Keep routes thin: validate wire format, delegate to Engine, map errors centrally (`src/openlens/api/routes.py`).
- Dataclasses in core only; no Pydantic outside `api/` (`src/openlens/core/interfaces.py`).
- Frontend branches only on typed error codes with narrowing guards, no `any` leaks (`frontend/src/api/client.ts`, `frontend/src/api/guard.ts`).
- Mirror wire types from backend schemas; regen via orval rather than hand-diverging (`frontend/src/api/types.ts`, `frontend/orval.config.ts`).
- Reuse shared utils before hand-rolling helpers; no barrel imports or global singletons.

### UI/UX (frontends)

- Theme contract first: use the `ol-*` utilities (`--color-ol-*`, `--spacing-ol-*`, `--text-ol-*`, `--radius-ol-*`, `--shadow-ol-*` in `admin-frontend/src/index.css`); never add hardcoded hex for a color the palette already covers. Single light theme: no `dark:` variants exist, do not introduce them.
- Check `<app>/src/components/ui/` before building anything (e.g. `Button`, `DataTable`, `SearchInput`, `StatusBadge`, `ConfirmDialog`, `Toast`); reuse primitives rather than raw HTML/CSS wrappers, and mirror neighboring composition.
- Motion stays minimal: short fades only, per the `index.css` header. No jarring transitions.
- Responsiveness: design mobile-first with Tailwind breakpoints (`grid-cols-2 sm:grid-cols-3`, `lg:grid-cols-2` as in `AnalyticsPanel.tsx`); narrow widths must stack or scroll, never clip or overlap. Verify at phone and desktop widths before calling a UI change done.
- Human eyes gate: a UI change is not done until the rendered result has been seen — a screenshot you inspected or an explicit human look. Tests alone never close a visual task.
- Honest UX claims: never state conformance numbers, ratios, or criteria from memory — the colocated axe smoke test is the authority. Label what you verified versus what you assumed, and never claim user validation that did not happen. Honor `prefers-reduced-motion` (existing pattern in `admin-frontend/src/components/Tour.tsx`).
- Accessibility: keyboard-reachable controls with visible focus states and explicit `aria-` labels where text is missing; extend the colocated axe smoke coverage (`admin-frontend/src/components/ui/a11y.test.tsx`) for every new primitive.
- Done means: no broken layout at the widths the surface supports, no raw unstyled controls, and the owning app's gates green (`lint`, `format:check`, `tsc` build, `vitest run` per `admin-frontend/package.json`).

## 7. Security and secrets

- Treat model, MCP, and external output as untrusted; redact at every boundary.
- Secrets via environment only; never commit secrets; respect `gitleaks` staged + full-history gates (`.gitleaks.toml`, `.github/workflows/ci.yml`, `docs/workflow/commit.md`).
- Never log queries or identities; hash where the analytics contract requires it (`docs/analytics-events.md`).
- Use synthetic fixtures in tests; report what you checked and what remains open.

## 8. Commits and PRs

- Stage exact paths only; one concern per commit with imperative messages (`docs/workflow/commit.md`).
- Never commit, push, tag, amend, or force-push unless explicitly asked.
- Never use `--no-verify` except by the documented mechanical-commit bypass trailer.
- If the change has an issue, link it; check for duplicate or overlapping work first.
- Update the docs that describe the behavior you changed.
- New wiki page → index it in `docs/wiki/Home.md` and `docs/wiki/_Sidebar.md` (whichever section fits; Home is the discovery index, the sidebar is navigation).
- Release commits set `project.version` in `pyproject.toml` to the tag (no `v` prefix); tags never move — a missed bump is a follow-up sync commit, never a retag (`docs/wiki/Release-Process.md`).

## 9. Never do

- Never hand-edit generated clients, route trees, lockfiles, or dist output; regen via the owning tool.
- Never weaken, delete, or skip tests to make a change pass.
- Never commit secret material or lockfile noise unrelated to the change.
- Never declare ready on tests alone when a built artifact exists — run it.

## 10. Scoped guidance

- Nearest nested AGENTS.md wins over this root; nested files add, never contradict.
- Keep this root capped; push per-area detail into `docs/` or scoped agent files.
- Agent scratch stays out of the repo or under ignored paths; keep output short and factual with file:line evidence.

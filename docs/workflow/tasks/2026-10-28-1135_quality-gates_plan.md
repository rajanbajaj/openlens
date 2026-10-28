# OpenLens — Code Quality Gates Plan

## Goal

Give OpenLens backend and frontend enforced, repeatable quality gates — lint, format, types, dependency vulnerabilities, code-security rules, and secret scanning — wire them into the commit hook, CI, and the multi-agent workflow contexts, and bring the current skeleton to a clean baseline under all of them.

## Success Criteria

- `ruff check`, `ruff format --check`, and `mypy` are clean on `src/` and `tests/`; `pip-audit` reports no unacknowledged vulnerabilities.
- `eslint`, `prettier --check`, and `tsc` are clean in `frontend/`; `npm audit` has no unacknowledged high/critical findings.
- A planted test secret is blocked by the commit hook and found by the repo-wide scan (then removed).
- CI runs every gate on push; multi-agent contexts require conforming code from future waves.
- The current skeleton passes all of the above with fixes committed — no grandfathered violations.

## Approach

The repo has no lint, type, audit, secret-scan, or CI configuration (`pyproject.toml` carries only pytest; `frontend/package.json` has only dev/build/test scripts; no `.github/`, no gitleaks config). The existing `docs/workflow/commit-hook.sh` staged-path-aware hook is the enforcement point to extend. Key decisions:

| Area | Choice | Why |
|---|---|---|
| Python lint + format | Ruff: `select` defaults plus `S` family, `[tool.ruff]` in `pyproject.toml`, `target-version = "py310"` to match `requires-python` | One binary replaces a black/flake8/bandit trio; `S` rules are flake8-bandit (verified live via `ruff rule S101`); target must be set explicitly because Ruff otherwise assumes a newer Python |
| Python types | mypy, pragmatic config (`python_version = "3.10"`), fix-or-justify per error | Codebase is already annotated, so cost is bounded; strict mode deferred |
| Python dep vulns | `pip-audit`, triaged fixes (never blind `--fix` on majors) | PyPA's auditor for environments, with an auto-fix mode used only after review |
| JS lint | ESLint flat config (`eslint.config.mjs`) with the typescript-eslint recommended set | Flat config is the current ESLint system; quickstart packages are `eslint @eslint/js typescript typescript-eslint` |
| JS format | Prettier, `--save-exact`, `.prettierrc` + `.prettierignore`, `prettier --check` in gates | Pinned version avoids format drift between installs |
| JS dep vulns | `npm audit`, triage then fix | Native to the toolchain in use; assumption: patch/minor bumps allowed, majors need explicit approval |
| Secrets | gitleaks (`protect --staged` in hook, full `detect` in CI) plus the existing secret-leak tests | Scanner catches history and staged content; tests prove runtime non-leakage |
| Enforcement | Extend `commit-hook.sh` + new `.github/workflows/ci.yml` + quality rules in both workflow contexts | Hook keeps commits fast (staged paths only); CI is the backstop; context rules make future agents emit conforming code |

## Steps

1. **Backend ruff.** Add `ruff` to the `dev` group, add `[tool.ruff]` (`target py310`, defaults plus `S`), run `ruff check --fix` then `ruff format` on `src/` and `tests/`, triage each remaining flag with a justified per-rule ignore or a fix.
2. **Backend mypy.** Add `mypy`, configure `python_version = "3.10"`, run `mypy src`, fix or justify every error.
3. **Backend audit.** Run `pip-audit`, triage each finding, apply fixes (reviewed, not blind), record any residual as accepted risk.
4. **Frontend lint + format.** Install the typescript-eslint quickstart set plus exact-pinned Prettier; add `eslint.config.mjs`, `.prettierrc`, `.prettierignore`, and `lint` / `format:check` scripts; run `eslint --fix` and `prettier --write`, triage the rest.
5. **Frontend audit.** Run `npm audit`, triage, fix per the version policy above.
6. **Secrets gate.** Install gitleaks, run a full-tree scan, remediate or allowlist with justification, add `gitleaks protect --staged` to the hook.
7. **Skeleton audit pass.** Re-run every gate on the current tree until all green; record before/after finding counts as the baseline.
8. **Workflow wiring.** Extend `commit-hook.sh` (ruff check + format --check on staged Python, eslint on staged frontend, gitleaks on staged content), add `.github/workflows/ci.yml` (backend job: ruff, mypy, pytest, pip-audit; frontend job: eslint, prettier check, test, build, npm audit), add quality rules to `docs/workflow/v1/context.md` and `frontend_context.md`, document the gates in `docs/workflow/commit.md`.

## Validation Plan

- `ruff check src tests`, `ruff format --check src tests`, `mypy src`, `pip-audit` — all clean.
- `npm --prefix frontend run lint`, `npx --prefix frontend prettier --check .`, `npm --prefix frontend run build` — all clean; `npm audit` triaged.
- `gitleaks detect --source .` clean; hook rejects a planted secret, then the secret is removed.
- Full suites still green (`pytest tests`, frontend `npm test`); new CI workflow passes on push.
- Highest-risk check: the skeleton audit pass must end at zero grandfathered violations — every `noqa`, ignore, or allowlist entry needs a written justification, not silence.

## Risks / Open Questions

- The first `ruff format` will touch many files; review it as a formatting-only diff before mixing in fixes.
- mypy on inherited skeleton code may surface deep annotation issues; the rule is fix-or-narrow, never weaken a test to obtain green.
- `pip-audit --fix` and `npm audit fix` can pull breaking majors; both run in triage-then-fix order per the steps.
- Hook latency: gates stay staged-path-aware so unrelated commits stay fast; CI covers the whole tree.
- Open questions: none — version-bump policy is stated as an assumption above.

## Sources

- [https://docs.astral.sh/ruff/configuration/](https://docs.astral.sh/ruff/configuration/)
- [https://github.com/pypa/pip-audit](https://github.com/pypa/pip-audit)
- [https://eslint.org/docs/latest/use/configure/configuration-files](https://eslint.org/docs/latest/use/configure/configuration-files)
- [https://typescript-eslint.io/getting-started/](https://typescript-eslint.io/getting-started/)
- [https://prettier.io/docs/en/install](https://prettier.io/docs/en/install)
- [https://mypy.readthedocs.io/en/stable/running_mypy.html](https://mypy.readthedocs.io/en/stable/running_mypy.html)
- [https://github.com/gitleaks/gitleaks](https://github.com/gitleaks/gitleaks)

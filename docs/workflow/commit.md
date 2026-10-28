# OpenLens — Commit Workflow

Follow this before every commit. The git `pre-commit` hook (installed in
`.git/hooks/`, not tracked) enforces the test steps automatically; the rest
is on the committer.

## Steps

1. **Stage only intended files.** `git add` the exact paths you changed.
   Never `git add -A` on a tree with unrelated edits; split those into their
   own commits.
2. **Affected suites run automatically.** The hook inspects staged paths:
   - `src/`, `tests/`, `configs/`, `pyproject.toml`, `uv.lock` staged →
     full backend suite (`.venv/bin/python -m pytest tests -q`,
     falling back to `uv run pytest tests -q` when no `.venv` exists).
   - `frontend/` staged → `npm --prefix frontend test` and
     `npm --prefix frontend run build`.
   - Staged `*.py` files → `ruff check` + `ruff format --check` on those files.
   - Staged frontend sources → `eslint` on those files.
   - Docs/config-only commits → no suite runs.
3. **Cross-cutting changes get the full gate.** If the commit touches both
   backend and frontend, or core interfaces, run both suites by hand before
   committing even if the hook would split them.
4. **No secrets in the diff.** The hook runs
   `gitleaks protect --staged` on every commit (gitleaks must be on PATH;
   install from [gitleaks releases](https://github.com/gitleaks/gitleaks)).
   It fails the commit when staged content matches a secret rule.
   Baseline exceptions live in `.gitleaks.toml`, each with a written
   justification — currently only the intentional sentinel fixtures in
   `tests/integration/test_secret_leak.py`. `git diff --cached` must show no
   other API keys, tokens, or credentials. Test sentinels are allowlisted;
   real values fail both the hook scan and the secret-leak suite — treat any
   failure as a stop-ship. CI runs the full-tree `gitleaks detect --source .`
   backstop, which must also be clean.
5. **One concern per commit.** Message in imperative mood, naming what and
   why (`Fix UI 405: ...`, not `fixes` or `wip`).

## On failure

Fix the code or tests, never weaken a failing test to obtain green, and never
commit with `--no-verify` to dodge a red suite. `--no-verify` is reserved for
mechanical commits the suites cannot judge (pure renames, doc-only moves
already validated) — state the reason in the commit message trailer
(`Bypass: <reason>`).

## Installing on a fresh clone

Hooks live outside version control. After cloning, copy the tracked template:

```bash
cp docs/workflow/commit-hook.sh .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit
```

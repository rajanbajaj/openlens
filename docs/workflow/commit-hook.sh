#!/bin/sh
# OpenLens pre-commit hook (tracked template; see docs/workflow/commit.md).
# Install: cp docs/workflow/commit-hook.sh .git/hooks/pre-commit
# Runs the suites affected by staged paths. Fails the commit on red.
set -e

STAGED=$(git diff --cached --name-only)

run_backend=0
run_frontend=0
if echo "$STAGED" | grep -qE '^(src|tests|configs|pyproject\.toml|uv\.lock)/?'; then
  run_backend=1
fi
if echo "$STAGED" | grep -qE '^frontend/'; then
  run_frontend=1
fi

# Staged-file fast gates (plan step 8): lint/format only what changed.
staged_py=$(echo "$STAGED" | grep -E '\.py$' || true)
staged_fe=$(echo "$STAGED" | grep -E '^frontend/.*\.(ts|tsx|js|jsx|mjs|cjs)$' | sed 's|^frontend/||' || true)

if [ "$run_backend" -eq 1 ]; then
  echo "[pre-commit] backend suite..."
  if [ -x .venv/bin/python ]; then
    .venv/bin/python -m pytest tests -q
  else
    uv run pytest tests -q
  fi
fi

if [ "$run_frontend" -eq 1 ]; then
  echo "[pre-commit] frontend tests..."
  npm --prefix frontend test
  echo "[pre-commit] frontend build..."
  npm --prefix frontend run build
fi

if [ -n "$staged_py" ]; then
  echo "[pre-commit] ruff (staged Python)..."
  # shellcheck disable=SC2086: intentional word splitting on file list
  if [ -x .venv/bin/python ] && .venv/bin/python -m ruff --version >/dev/null 2>&1; then
    # shellcheck disable=SC2086
    .venv/bin/python -m ruff check $staged_py && .venv/bin/python -m ruff format --check $staged_py
  elif command -v ruff >/dev/null 2>&1; then
    # shellcheck disable=SC2086
    ruff check $staged_py && ruff format --check $staged_py
  else
    echo "[pre-commit] warning: ruff not available; skipping staged lint (CI is the backstop)."
  fi
fi

if [ -n "$staged_fe" ]; then
  echo "[pre-commit] eslint (staged frontend)..."
  if [ -x frontend/node_modules/.bin/eslint ]; then
    # shellcheck disable=SC2086: intentional word splitting on file list
    (cd frontend && ./node_modules/.bin/eslint $staged_fe)
  else
    echo "[pre-commit] warning: frontend eslint not installed; skipping staged lint (CI is the backstop)."
  fi
fi

echo "[pre-commit] secret scan (gitleaks protect --staged)..."
if command -v gitleaks >/dev/null 2>&1; then
  if [ -f .gitleaks.toml ]; then
    gitleaks protect --staged --verbose --redact --config .gitleaks.toml
  else
    gitleaks protect --staged --verbose --redact
  fi
else
  echo "[pre-commit] warning: gitleaks not on PATH; skipping staged secret scan."
  echo "[pre-commit] warning: install gitleaks (https://github.com/gitleaks/gitleaks)"
  echo "[pre-commit] warning: CI runs the full-tree 'gitleaks detect' backstop."
fi

echo "[pre-commit] ok"

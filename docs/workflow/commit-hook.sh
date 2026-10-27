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

echo "[pre-commit] ok"

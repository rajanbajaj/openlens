# OpenLens Build — Shared Agent Context (workflow v1)

Every agent in this workflow inherits these conventions. They are binding.

## Sources of Truth

- `docs/workflow/tasks/2026-10-28-0249_skeleton_plan.md` — what to build and why.
- `src/openlens/core/interfaces.py` + `core/context.py` (after Wave 0) —
  the only type shapes you may use. Do not invent parallel models.
- `pyproject.toml` — hatchling, `src` layout, `testpaths=["tests"]`.

## Code Rules

- Absolute imports only: `from openlens.core.interfaces import ...`.
- Dataclasses for all models. No Pydantic in `core/`, `providers/`,
  `retrieval/`, `ranking/`, `storage/`, `indexing/`, `plugins/`
  (Pydantic lives in `api/schemas.py` only).
- Error taxonomy: providers raise `ProviderError` (wraps timeouts, auth,
  rate limits; carries `provider_name`, `retryable: bool`). Plugins raise
  `PluginError`. Mandatory-filter failures propagate; optional-plugin
  failures are caught, logged, and recorded in `SearchContext` diagnostics.
- Never log or return secrets. Config resolves `${ENV_VAR}` at load time;
  unresolved secrets fail startup validation loudly.
- Quality gates (CI runs all of these; keep them green, never weaken a test
  to obtain green):
  - `uv run ruff check src tests` and `uv run ruff format --check src tests`
    clean. Ruff config lives in `pyproject.toml` (`target-version py310`,
    `E` + `F` + `S`).
  - `uv run mypy src` clean (`python_version = "3.10"` in `pyproject.toml`).
    Blanket-error suppressions (`BLE`-style `noqa`, bare `type: ignore`)
    are forbidden; every `noqa`/`type: ignore` needs an inline comment
    stating why the flagged construct is safe.
  - `uv run pytest tests -q` green, offline only (no network, keys, cluster).
  - `uv run pip-audit` with no unacknowledged findings; never blind `--fix`
    a major version — triage first, record residuals as accepted risk.
- Keep `original_score` (provider signal) separate from `final_score`
  (OpenLens ranking) on every result, with human-readable `reasons`.

## File Ownership

- Write ONLY files listed in your brief. All other files are read-only.
- Only the contracts agent edits repo-root files (`pyproject.toml`,
  `README.md`, `.gitignore`). Every other agent requests changes in its
  result summary instead.
- If you need a change outside your list, report it in your result summary
  instead of making it — the Wave-3 agent resolves cross-cutting fixes.

## Tests

- Co-locate unit tests under `tests/unit/test_<area>.py` (Wave 2F owns the
  conformance suites; you own smoke tests for your area).
- Offline only: no network, no API keys, no OpenSearch cluster. Use fakes.

## Result Format

End with a short summary: files written, tests run with exact commands and
outcomes, deviations from your brief, and anything Wave 3 must know.

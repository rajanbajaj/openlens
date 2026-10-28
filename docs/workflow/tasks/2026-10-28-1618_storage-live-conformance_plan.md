# Task 19 — Storage live conformance + packaging/CI/README contracts

Follow-up to task 17 (persistent DB connectors). T17 shipped the code and is
fully green offline (834 passed, 41 skipped; ruff/format/mypy clean). What
remains is everything that needs live servers, drivers, CI, or docs owned by
the contracts owner. This task closes those gaps; it makes no changes to the
storage contract, backends, or engine wiring.

## Scope

1. **pyproject optional extras** — `mysql` (pymysql) and `postgres` (psycopg v3)
   driver-only extras. No driver becomes a hard dependency; SQLite keeps zero
   extra deps and both server backends keep lazy imports (missing driver must
   still raise `ProviderError(UNAVAILABLE)`, never `ImportError`).
2. **CI service containers** — MySQL + Postgres services in
   `.github/workflows/ci.yml` that install the matching extra, set
   `OPENLENS_TEST_MYSQL_DSN` / `OPENLENS_TEST_PG_DSN`, and run the env-gated
   suites with **zero skips expected**.
3. **Live MySQL conformance** — `tests/integration/test_storage_mysql.py`
   DSN-set mode green: all 11 gated tests pass (contract shape, add/get,
   totals, upsert-once, paging, blank query, delete+count, chunking, close,
   `SHOW INDEX` PK+FULLTEXT).
4. **Live Postgres conformance** — `tests/integration/test_storage_postgres.py`
   DSN-set mode green: all 13 gated tests pass, including the GIN/`EXPLAIN`
   index-usage assertion with no Seq Scan.
5. **README storage docs** — document the top-level `storage:` section
   (backend switch, `sqlite`/`mysql`/`postgres` fields, `OPENLENS_MYSQL_*` /
   `OPENLENS_PG_*` env vars, driver extras), plus the known lossy FTS5 →
   `FULLTEXT` / `plainto_tsquery` translation caveat and the WAL/pool notes.

## Out of scope

- No changes to `storage/base.py`, any backend, `custom_index.py`,
  `main.py`, or `configs/default.yaml` except what CI/pytest fixtures require.
- No new backends, no semantic/vector retrieval (orphan O1), no
  OpenSearch-as-storage (orphan O2).
- Local dev containers out of scope; CI service containers only
  (`deploy/local/docker-compose.yml` untouched).

## Definition of done

1. `uv run --extra mysql` / `--extra postgres` (or the repo's chosen
   extra-install spelling) installs the driver without touching the default
   lock; default install still imports neither driver.
2. CI runs both storage suites against live services with zero skips.
3. Live suites green: 11/11 MySQL, 13/13 Postgres (GIN assertion included).
4. Missing-driver behavior unchanged: unit tests asserting
   `ProviderError(UNAVAILABLE)` still pass with drivers absent.
5. Full offline suite still green (no regressions), ruff/format/mypy clean.
6. README documents the `storage:` section, env vars, extras, and the
   lossy-translation caveat.

## Verification

- `OPENLENS_TEST_MYSQL_DSN=... uv run --extra mysql pytest tests/integration/test_storage_mysql.py -q` → all pass, 0 skipped.
- `OPENLENS_TEST_PG_DSN=... uv run --extra postgres pytest tests/integration/test_storage_postgres.py -q` → all pass, 0 skipped.
- Offline: full `pytest`, `ruff check`, `ruff format --check`, `mypy src`,
  plus the driver-absence unit tests with drivers uninstalled.
- CI run green on push (service containers + DSNs wired).

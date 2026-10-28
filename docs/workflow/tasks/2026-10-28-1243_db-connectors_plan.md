# Persistent DB Connectors Plan

## Goal

Make the document store swappable: a single storage interface with
SQLite, MySQL, and Postgres connectors behind it, so switching
databases later is a config change, not a rewrite.

Falsifiable: pointing `storage.backend` at each of the three backends
in turn, indexing the same fixture corpus, and running identical
queries yields the same document id sets (ranking order may differ per
engine default); the conformance suite passes on SQLite always and on
MySQL/Postgres whenever reachable.

## Reality baseline (verified against the tree — read before implementing)

- `DocumentRepository` (`src/openlens/storage/repository.py`) provides
  ONLY `add_document(doc)` / `add_documents(docs) -> int` (upsert via
  `INSERT OR REPLACE` into `documents` plus DELETE+INSERT on the
  standalone FTS5 table `documents_fts(doc_id UNINDEXED, title, content)`),
  `get(doc_id)`, `count()`, `search(fts_query, limit=10, offset=0) ->
  (page, total)`, and `close()`. There is NO `update` or `delete`
  method — the earlier premise that they exist was wrong. `update`
  semantics ≈ the existing upsert (same id overwrites); `delete` is
  new work (documents row + FTS row, atomically) that every backend
  must implement. Search scoring is `-bm25(documents_fts)` (higher is
  better) and total comes from a separate `COUNT(*) ... MATCH ?` query
  under the same lock hold.
- Single caller chain: `CustomIndexProvider`
  (`src/openlens/providers/custom_index.py`) constructs
  `DocumentRepository(db_path)` in `__init__` and again in `configure`
  (the second construction leaks the first connection — `close()` has
  no call site anywhere in `src/`), and `main.build_engine`
  (`src/openlens/main.py`) pre-creates the SQLite parent dir for any
  provider entry carrying a string `db_path`. There is NO `storage:`
  config section yet — it is new (`configs/default.yaml` /
  `providers.yaml` carry `db_path` inside the provider entry only).
  Coordination point (left for synthesis, review-plan D1/S6): whether
  backend selection lives in a new top-level `storage:` section (this
  plan's preference, single switch `storage.backend`) or inside the
  provider entry, who owns the indexer entry point (crawler-index plan
  T8), and the port order — T8 lands `delete`, the `content_hash`
  column, and URL/hash indexes first on SQLite; this plan ports (never
  re-implements) them into the `storage/base.py` contract and the other
  backends. Until synthesis decides, `db_path` keeps working as the
  SQLite backend's field.
- SQLite access is fully serialized: one `sqlite3` connection
  (`check_same_thread=False`) + one `threading.Lock` around every read
  and write, including `health_check`'s `count()`
  (`CustomIndexProvider.health_check` → `repository.count()`). No WAL
  mode, no `busy_timeout`, no secondary indexes beyond the `id` primary
  key and the FTS5 index are configured today (`_init_schema` runs two
  `CREATE ... IF NOT EXISTS` statements). Pagination (`limit`/`offset`)
  and `-bm25` scoring already exist; `CustomIndexProvider.search`
  currently discards the repository total (`scored, _total`) — the API
  `total` is the post-pipeline result count, so the repository total
  contract matters for future callers, not today's response shape.
- Seeding side effect: `CustomIndexProvider.configure(seed=True)` (the
  default) calls `DocumentIndexer.index_seed()` on every configure, so
  backend-switch tests must pass `seed: false` or use isolated stores
  or they will measure seed writes, not connector behavior.
- Dependency constraint: `pyproject.toml` declares NO MySQL/Postgres
  drivers (core deps are `fastapi`, `uvicorn`, `pyyaml`, `httpx`,
  `opensearch-py`). Drivers must be lazy-imported (unconfigured backends
  add no import-time dependency — mirror the lazy `import httpx` in
  `providers/http.py:_fetch` and `register_builtin_models` in
  `models/registry.py`) AND declared as optional extras (e.g.
  `project.optional-dependencies: mysql`, `postgres`) — the extras
  declaration itself is a contracts-agent edit; this plan requests it
  and codes against the lazy-import seam. CI service containers for
  MySQL/Postgres are likewise requested, not created, here.

## Scope (all connected: one storage backend surface)

- Storage interface: abstract contract in `src/openlens/storage/base.py`
  (ABC matching the `core/interfaces.py` style) with exactly the
  existing SQLite signatures — `add_document(doc)`,
  `add_documents(docs) -> int` (upsert), `delete(doc_id) -> bool`
  (True iff a row was removed; deleting a missing id is not an error),
  `get(doc_id) -> Document | None`,
  `search(fts_query, limit=10, offset=0) -> (list[(Document, float)], int)`,
  `count() -> int`, `close()`. SQLite `DocumentRepository` becomes the
  reference implementation (FTS5 `-bm25` ranking preserved);
  `delete` is added to it as part of step 1 (or ported from T8 if T8
  landed first, per D1 — check `tasks_status.md` before writing it).
  The `search` input stays the already-parsed FTS-style query string
  produced by `retrieval/query_parser.parse_query` via
  `CustomIndexProvider`; each backend translates it to its native form
  (FTS5 `MATCH`, MySQL `MATCH ... AGAINST`, Postgres `to_tsquery`) and
  the translation is documented as lossy — cross-backend query
  normalization is a non-goal. `Document.metadata` round-trips as a
  parsed dict (JSON text column on SQLite/MySQL, `JSONB` or text on
  Postgres — conformance compares parsed dicts, not raw storage).
- MySQL connector: same contract over MySQL (`FULLTEXT` index on
  title/content created in schema setup), config-driven connection
  (host/port/db/user/password via env, `${VAR}` resolution through the
  existing `load_config` in `src/openlens/config.py`).
- Postgres connector: same contract over Postgres (`tsvector` column +
  GIN index created in schema setup), config-driven connection, same
  env conventions.
- Backend selection via new config (`storage.backend: sqlite | mysql |
  postgres` + per-backend connection fields, exact section placement
  per the synthesis decision in the Reality baseline); startup
  validation fails loudly (`ConfigError` for unknown backend names and
  malformed sections; connection failures at boot surface as
  `ProviderError(UNAVAILABLE)` from the repository constructor, never
  raw driver exceptions) on unknown backends or
  unreachable-but-configured DBs; driver imports lazy so unconfigured
  backends add no import-time dependency. SQLite remains the default
  (`storage.backend: sqlite`, `db_path` default `data/openlens.db`
  preserved from `configs/default.yaml`). Re-configuring a provider
  closes the previous repository before opening the new one (fixes the
  leak in the Reality baseline).
- Backend conformance suite: one parametrized suite, run against all
  three backends. MySQL/Postgres cases skip cleanly without a reachable
  DB (env-gated, e.g. `OPENLENS_TEST_MYSQL_DSN` / `OPENLENS_TEST_PG_DSN`
  unset → skip with reason); CI runs them against service containers
  (requested from the contracts/CI owner). Each backend runs against an
  isolated store (temp file / temp schema) with `seed: false`.

## Steps

1. Extract the storage interface (`src/openlens/storage/base.py`, ABC —
   match the codebase's ABC style in `core/interfaces.py`, not Protocol);
   add `delete(doc_id) -> bool` to the SQLite implementation (single
   transaction deleting from both `documents` and `documents_fts`;
   if T8 already landed `delete`/`content_hash`/indexes, port that
   implementation instead of writing a second one — check
   `tasks_status.md` first, per D1); adapt existing tests to the
   interface (no behavior change; `test_custom_index.py` and the
   repository's current callers must stay green unmodified in
   behavior). `tests/unit/test_storage_base.py`: SQLite passes the new
   conformance suite (add/upsert/get/search-page+total/count/delete/
   close); `delete` removes both the documents row and the FTS row
   (deleted docs never reappear in search and `count` decrements);
   deleting a missing id returns `False` without raising;
   upsert-overwrite still works (same id, new content, search sees the
   new text exactly once).
2. MySQL connector (`src/openlens/storage/mysql_backend.py` or the
   module name chosen to mirror `repository.py` — record it here when
   decided) + conformance run (env-gated).
   `tests/integration/test_storage_mysql.py`: the same conformance suite
   against MySQL when `OPENLENS_TEST_MYSQL_DSN` is set, clean skip with
   reason otherwise; schema setup creates the PK + `FULLTEXT` index
   (assert via `SHOW INDEX` or `EXPLAIN` in review, not just behavior);
   connection failure raises `ProviderError` with code `UNAVAILABLE`
   (never raw driver exceptions), surfaced at startup/construction,
   not mid-request; the driver import is lazy (module imports cleanly
   without the driver installed, failing only on instantiation).
3. Postgres connector + conformance run (env-gated).
   `tests/integration/test_storage_postgres.py`: same shape with
   `OPENLENS_TEST_PG_DSN`; schema setup creates the PK + `tsvector`
   GIN index; same lazy-import and error-mapping assertions.
4. Config selection + startup validation + lifecycle + docs (connection
   fields, env var names, driver extras to install). Wire backend
   selection into `CustomIndexProvider` (or its successor per synthesis
   S6) so `storage.backend` (default `sqlite`) is the single switch and
   the provider-entry `db_path` becomes the SQLite backend's field;
   `configure` closes the previous repository before opening the new
   backend. `tests/unit/test_storage_config.py`: unknown backend →
   `ConfigError`; unreachable-but-configured backend → `ProviderError`
   at construction; unconfigured-backend driver never imported (assert
   via `sys.modules` before/after importing the backend module and
   instantiating SQLite); SQLite default boots with no extra config
   and no `storage:` section at all (back-compat with today's
   `configs/default.yaml`). Update `README.md` config table only via
   the contracts-agent path if it owns that file in this wave —
   otherwise list the README edit as a requested change.

## Validation Plan

- `uv run pytest tests/unit/test_storage_base.py tests/unit/test_storage_config.py tests/integration/test_storage_mysql.py tests/integration/test_storage_postgres.py -q` green (MySQL/Postgres suites green when reachable, cleanly skipped otherwise — verify BOTH modes in this order: (1) with `OPENLENS_TEST_MYSQL_DSN`/`OPENLENS_TEST_PG_DSN` unset, assert clean skips with reasons and no import of the drivers; (2) with local MySQL/Postgres available, assert the same conformance suite passes with zero skips).
- `uv run pytest tests -q` green (offline only, DSNs unset); `uv run ruff check
  src tests` and `uv run ruff format --check src tests` clean;
  `uv run mypy src` clean; `uv run pip-audit` with no new unacknowledged
  findings (driver extras are optional — the default install must not
  gain a required driver).
- Manual: point `storage.backend` at each backend in turn, index the
  fixture corpus (`tests/fixtures/corpus.py`, `seed: false`, isolated
  store per backend), run identical queries through
  `CustomIndexProvider.search`, and confirm matching document *id sets*
  (ranking order may differ per engine default — assert set equality,
  not order or score equality); `uv run python -c "import openlens.storage.mysql_backend"`
  (substituting the real module names from steps 2–3) succeeds WITHOUT
  the driver installed (lazy import proven), failing only when the
  backend class is instantiated/configured; confirm
  `uv run python -c "import openlens.storage.postgres_backend"` likewise.
  Spot-check one hot-path query per server backend with `EXPLAIN`
  (no sequential scan on the FTS/tsvector lookup).

## Scalability notes

- Bottleneck 1 — single global lock on the SQLite path (binds first,
  at ~10x concurrent QPS). Every operation — `search` (COUNT + SELECT
  under one hold), `get`, `count` (including every `health_check`), and
  `add_documents` — serializes on one `threading.Lock` around one
  connection (`repository.py`); concurrent searches queue behind each
  other and behind indexing, so read latency grows linearly with
  concurrency even though each query is indexed. Fix (folded into
  scope): keep transactions short (chunk bulk `add_documents` — no
  unbounded single-transaction ingest, see bottleneck 2), and enable
  WAL mode (`PRAGMA journal_mode=WAL` + a bounded `busy_timeout`) at
  schema init so readers don't block the single writer; the lock stays
  (it is the documented safe pattern for a shared `sqlite3` handle)
  but holds for milliseconds, not batch durations. Fix shape for
  server backends (folded into connector design): bounded connection
  pool / per-thread connections with explicit pool size, checkout
  timeout, and recycle — do not copy the single-connection +
  global-lock pattern to MySQL/Postgres.
- Bottleneck 2 — per-doc FTS maintenance loop. `add_documents` issues
  DELETE+INSERT on the FTS table per document inside one transaction
  (`repository.py:62-80`): fine at fixture scale, but lock-hold time
  and WAL growth grow linearly with batch size, stalling all readers
  for the duration of a large ingest. Fix (folded into scope): chunked
  batches (default 500 documents per transaction, configurable via the
  interface contract so all backends share the bound; each chunk
  commits independently and `add_documents` returns the total indexed).
  Callers needing all-or-nothing ingest must layer it themselves — the
  contract does not promise cross-chunk atomicity.
- Bottleneck 3 — `search` does COUNT(*) + paged SELECT per request.
  The count query re-runs the full MATCH on every page (`repository.py:122-136`);
  acceptable while FTS indexes fit in memory, degrades with corpus
  growth and deep pagination (`OFFSET` scans grow with page depth).
  Fix shape (flagged, not built): exact-total only for the first N
  pages, estimated/absent totals beyond; keep `limit`/`offset`
  pagination (already present — never add an unbounded "return all"
  path). Note `CustomIndexProvider` currently discards this total, so
  the cost is paid for a value no caller reads today — the conformance
  suite still pins it because the interface promises it.
- Bottleneck 4 — unindexed lookups on new backends (fails immediately
  at corpus scale if missed, not at 10x). SQLite `get` rides the PK;
  MySQL/Postgres ports must create the PK + FULLTEXT/GIN indexes in
  schema setup (part of steps 2–3 acceptance: `EXPLAIN` a spot query
  in review, no sequential scan on the hot path). Flagged: no secondary
  metadata/URL indexes in v1 beyond what T8 ports (D1) — metadata
  filtering or URL-dedup lookups at scale will need them later, and
  adding them per-backend without an interface decision would fork the
  contract.
- Bottleneck 5 — connection lifecycle on reconfigure (binds at any
  scale under config churn / tests). `CustomIndexProvider.configure`
  leaks the prior connection today (no `close()` call site in `src/`);
  repeated backend switches would exhaust file handles / server
  connections. Fix (folded into scope, step 4): `configure` closes the
  previous repository before opening the new one, and `close()` is
  idempotent on every backend.
- Rewrite-forcing risk: single-writer SQLite as the production store.
  At ~100x write concurrency (or multi-process serving, where the
  in-process lock gives no protection at all) the answer is "run
  Postgres" (which this plan enables) — not SQLite tuning. Cross-backend
  replication, online migration tooling, or a distributed index would
  force new infrastructure and stays a non-goal.

## Non-goals

- ORM adoption. Cross-backend replication, online migration tooling,
  or a distributed database (original-architecture §13 explicitly
  defers it; Postgres-behind-the-interface is the scale-out answer).
- Full-text parity tuning beyond each engine's default ranking
  (id-set parity on identical queries is the bar; order and raw scores
  may differ — conformance never asserts score equality).
- Cross-backend query-syntax normalization (each backend keeps its
  native MATCH/tsquery translation of the parsed query string in v1;
  callers keep producing the SQLite-shaped string via `parse_query`).
- An unbounded "return all" search path, cursor/keyset pagination
  (v1 keeps the existing `limit`/`offset` contract), and secondary
  metadata/URL indexes beyond the T8 port (D1).
- Deleting/rewriting the crawler-index plan's indexer entry point —
  the interface seam is coordinated in synthesis (S6/D1); this plan
  also does not own the OpenSearch storage backend (review-plan orphan
  O2, deferred by consensus).

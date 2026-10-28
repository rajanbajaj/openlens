# Observability & CLI Plan

## Goal

Close Phase 4: operators can run, observe, and debug OpenLens without
reading response JSON — structured logs and a metrics endpoint expose
provider/plugin behavior, and a CLI covers search, index management,
config validation, and provider health (original plan §3/§15-Phase-4).

Falsifiable: a CLI search emits one JSON log line carrying the request
id plus provider latencies; `/metrics` scrapes Prometheus text with
per-provider counters; `config validate` rejects a bad config with a
nonzero exit without booting the server.

## Reality baseline (verified against the tree — read before implementing)

- NO logging, metrics, or CLI code exists in `src/` (verified: no
  `import logging`, no `argparse`/`click`/`typer`, no `/metrics` route
  anywhere under `src/openlens/`). `main.create_app` (`src/openlens/main.py`)
  exposes only the search router (`src/openlens/api/routes.py`,
  `POST /api/v1/search`) plus `GET /healthz`. Everything below is new.
  The only `logging` use in the repo is `caplog` assertions inside
  `tests/integration/test_secret_leak.py` — there is no production
  logger to extend.
- The instrumentation seam already exists: `SearchContext`
  (`src/openlens/core/context.py`) records `provider_latencies_ms`,
  `plugin_executions` (name/stage/status/latency/error), and `errors`,
  surfaced via `diagnostics()` with exactly four keys
  (`fallback_used`, `provider_latencies_ms`, `plugin_executions`,
  `errors`). Logs and metrics MUST build on this payload — do not
  invent parallel hooks in providers/plugins. Adding a request id
  (Scope below) therefore means extending `diagnostics()` *and*
  `DiagnosticsSchema` (`src/openlens/api/schemas.py`, which whitelists
  the same four keys in `from_core`) — it is not a logging-only change.
- `SearchEngine.search` (`src/openlens/core/engine.py`) is synchronous
  and `Pipeline.run` records one `PluginExecution` per plugin; provider
  latencies are recorded by both `ProviderManager._search_one` and the
  engine fallback path. Metrics fed "at response time" must hook
  exactly one of those points (API route after `engine.search` returns,
  reading `SearchResponse.diagnostics`) — feeding from both double-counts.
- `tests/integration/test_secret_leak.py` exists and already asserts
  secrets appear in no response body, diagnostics payload, or captured
  log record: extend it to the new log output and metric labels rather
  than writing a separate suite. Sentinel pattern to reuse:
  `SENTINEL_KEY`/`SENTINEL_PASSWORD` + `_assert_no_secrets`.
- Dependency constraint: `pyproject.toml` declares only `fastapi`,
  `uvicorn`, `pyyaml`, `httpx`, `opensearch-py` (plus dev tools). No
  CLI or metrics libraries (no click/typer/prometheus_client).
  Implement the CLI with stdlib `argparse` and emit Prometheus text by
  hand (the format is a few lines; no new dependency). Only the
  contracts agent may add deps (shared context, File Ownership).
- Engine construction seam: `build_engine(config)` + `load_app_config()`
  in `main.py` — the CLI reuses these, never a second boot path.
  Caveats the CLI must respect: importing `openlens.main` executes
  `app = create_app()` and `_boot_default_engine()` at module scope
  (a failed boot yields `engine=None` → HTTP 503, never an import
  crash), and `build_engine` pre-creates SQLite parent dirs as a side
  effect. `config validate` must therefore validate *without*
  importing the serving path's side effects into a pass/fail signal —
  see step 3.
- Index-command gap: there is no file/URL ingestion path to wrap.
  `DocumentIndexer` (`src/openlens/indexing/indexer.py`) accepts only
  already-built `Document` objects (`index_documents` → repository
  `add_documents`); `CustomIndexProvider.index_documents` is the only
  caller-facing entry, and the repository
  (`src/openlens/storage/repository.py`) offers `add/get/count/search`
  — no `list`, no `delete`, no reindex primitive. `index add <files-or-urls>`
  therefore cannot be implemented end-to-end by this plan alone; it
  depends on the crawler-index plan's indexer entry point (review-plan
  seam S7: this plan owns the CLI surface, T8 owns indexer behavior).
  Step 3 sequences accordingly instead of assuming the seam exists.
- `--provider` gap: `SearchRequest` (`src/openlens/core/interfaces.py`)
  has no provider field and `SearchEngine` always tries its ordered
  provider list (primary, then fallback) — a CLI `--provider` flag
  cannot be honored without the federated plan's `provider_prefs`
  (T13). The flag is either dropped in v1 or forwarded only when T13
  has landed; it must not be parsed-and-silently-ignored.

## Scope (all connected: one operations surface)

- Structured logging: one JSON log record per request (request id,
  query mode/limit — never the raw query at info level — provider
  latencies, plugin executions, errors) plus startup/shutdown events;
  log-level config (`logging.level`, default INFO, new key under the
  app config — no such key exists today); request id generated per
  request (`X-Request-Id` accepted if supplied, else `uuid4` hex),
  echoed in the search response diagnostics as `diagnostics.request_id`
  and in the `X-Request-Id` response header. The CORS allow-headers
  list in `main.create_app` (`Content-Type`, `Authorization`) must gain
  `X-Request-Id` or browser clients cannot send/read it. No secrets in
  logs (extends the secret-leak suite to log output). Debug-level
  per-query logging (raw query text) is level-gated and off by default;
  INFO records carry mode/limit/counts only.
- Metrics endpoint: in-process counters/timings with a FIXED label set
  (provider name, plugin name, stage, status — never query text, doc
  ids, or other unbounded values): per-provider latency sum/count (plus
  fixed-bucket histogram counts if trivial), per-provider error counts,
  per-plugin timings and exclusion counts, request totals. Served as
  Prometheus text on `GET /metrics`, behind a disabled-by-default toggle
  (`metrics.enabled: false` default, new config key); when disabled the
  route 404s (registered conditionally in `create_app`, not registered-
  then-erroring). The registry is fed from exactly one point — the API
  route after `engine.search` returns, reading `SearchResponse.diagnostics`
  — and guarded by a `threading.Lock` (FastAPI/TestClient serve from
  worker threads). Label values are escaped per the Prometheus text
  format; the registry rejects unknown label names by construction.
- CLI (`python -m openlens.cli`, stdlib `argparse` only — single module
  `src/openlens/cli.py` with `def main(argv=None) -> int`):
  `search "query" [--mode --limit --json|--pretty]` (`--provider`
  only if T13 `provider_prefs` has landed, otherwise the flag does not
  exist — see Reality baseline),
  `index (add|update|reindex|list) <files-or-urls>` (surface owned
  here; behavior delegates to the crawler-index plan's indexer entry
  point per seam S7 — until that entry point exists the subcommands
  exit nonzero with an explicit "indexer not available" message rather
  than a stub that silently succeeds),
  `config validate [--config PATH]` (startup validation without
  serving; nonzero exit + human-readable errors on failure),
  `providers health` (per-provider reachability table via
  `SearchEngine.health_check` / `ProviderManager.health_check`).
  `index` commands operate through the same repository/provider seam
  the engine uses — no direct SQLite file manipulation in the CLI.
- Docs: README operations section (run, observe, troubleshoot) with
  the exact commands from the Validation Plan, including how to enable
  `/metrics` and read a log line.

## Steps

1. Logging + request id: request-scoped logger (stdlib `logging` +
   JSON formatter, one handler, no third-party formatter), request-id
   middleware/dependency, level config (`logging.level`), secret-scrub
   tests. Extend `SearchContext.diagnostics()` and
   `DiagnosticsSchema.from_core` with `request_id` in the same change
   (both whitelists must move together or the id is silently dropped).
   Add `X-Request-Id` to the CORS `allow_headers` in `create_app`.
   `tests/unit/test_logging.py`: one search emits exactly one JSON
   INFO record carrying `request_id`, mode, limit, provider latencies,
   and result count; the raw query string never appears at INFO;
   secrets (`api_key`-shaped config values, sentinel pattern from
   `test_secret_leak.py`) never appear in the record; `logging.level:
   WARNING` suppresses the INFO record; a supplied `X-Request-Id`
   header is echoed, an absent one is generated.
2. Metrics: fixed-label in-process registry fed from `SearchResponse.diagnostics`
   at the API route (single feed point — see Reality baseline),
   `GET /metrics` Prometheus-text route registered only when
   `metrics.enabled: true`, hand-written exposition with label escaping.
   `tests/unit/test_metrics.py` + `tests/integration/test_metrics.py`:
   disabled (default) → 404; enabled → scrape after searches shows
   request/provider/plugin series with only the fixed label names;
   concurrent searches from threads do not lose counts (lock asserted
   by a threaded test); fuzz test: adversarial query text (quotes,
   newlines, label-like substrings) never becomes a label name or value.
3. CLI commands (search/index/validate/health) + tests via direct
   `main(argv)` calls (no `typer`, no network; `subprocess` only for
   the exit-code assertion). `src/openlens/cli.py` reuses
   `load_app_config`/`build_engine` — never a second boot path.
   `tests/unit/test_cli.py`: `search` JSON shape matches
   `SearchResponseSchema` and `--pretty` is human-readable without
   changing payload semantics; `config validate` passes (exit 0) on
   `configs/default.yaml` and fails (nonzero exit, human-readable
   stderr, no traceback) on a malformed config written to `tmp_path`
   (there is no committed `configs/bad.yaml` — do not add one);
   `config validate` does not start a server and does not create
   `data/` as a validation side effect beyond what `build_engine`
   already does (assert the distinction, or validate via
   `load_config` + manager construction without serving);
   `providers health` reports the enabled providers offline via
   `health_check` without network; `index` subcommands delegate to the
   T8 indexer entry point when present and otherwise exit nonzero with
   the explicit not-available message (both branches tested with a fake
   indexer injected).
4. Docs: README operations section (run, observe, troubleshoot) with
   the exact commands from the Validation Plan, the `metrics.enabled`
   toggle, and one annotated example log line + `/metrics` excerpt.

## Validation Plan

- `uv run pytest tests/unit/test_logging.py tests/unit/test_metrics.py tests/integration/test_metrics.py tests/unit/test_cli.py tests/integration/test_secret_leak.py -q` green (offline only).
- `uv run pytest tests -q` green (offline only); `uv run ruff check
  src tests` and `uv run ruff format --check src tests` clean;
  `uv run mypy src` clean; `uv run pip-audit` with no new findings
  (no new dependency is expected).
- Manual (run with `uv run` so the project env is used):
  `uv run python -m openlens.cli search "cats" --json` emits a JSON
  log line with `request_id` + latencies and no secrets;
  `curl localhost:8000/metrics` scrapes provider/plugin series (with
  `metrics.enabled: true`) and 404s when disabled (default);
  `uv run python -m openlens.cli config validate --config <malformed-tmp-config>`
  exits nonzero without booting a server (use a temp file — no
  committed bad config); `uv run python -m openlens.cli providers
  health` lists per-provider status offline.

## Scalability notes

- Bottleneck 1 — log volume at ~100x QPS. One JSON record per request
  is fine at low volume; at high volume disk/egress and formatter CPU
  dominate. Fix (folded into scope): INFO-level records stay small and
  fixed-shape (no snippets, no full diagnostics dump — counts and
  latencies only, fixed key set); verbose per-query/per-result detail
  is DEBUG-gated and off by default. Flagged, not built: sampling
  (log 1/N success records, always log errors) and async log shipping —
  both need deployment decisions, not library code.
- Bottleneck 2 — metric cardinality explosion (fails as soon as a label
  carries request data, at any scale). A `query=` or `doc_id=` label
  creates an unbounded series set and OOMs the process and any scraper.
  Fix (folded into scope): fixed label set enforced by construction
  (registry accepts only known label names — provider, plugin, stage,
  status — and rejects others; fuzz test proves query text can't leak
  into labels). In-process counters stay O(providers × plugins × statuses),
  i.e. dozens of series, independent of corpus or query volume.
- Bottleneck 3 — `/metrics` scrape cost + registry contention.
  Rendering exposition on every scrape is O(series) string work and
  the registry lock is shared with the request path (every search
  increments counters). Fix (folded into scope): hold the lock only to
  snapshot counters, render text after releasing it; scrape is a plain
  GET with no engine call and no provider I/O. Flagged: at high scrape
  frequency × worker count, move exposition to a cached string refreshed
  on a timer — not now.
- Bottleneck 4 — multi-worker split (binds when uvicorn runs >1 worker
  or the app is replicated). In-process counters and per-process log
  streams each see only their worker's traffic; `/metrics` scraped
  from one worker under-reports by the worker count and request ids
  are unique but not globally ordered. Fix shape (flagged, not built):
  scrape every worker / aggregate in Prometheus, or move counters to a
  shared store — that is the distributed-observability step the
  Non-goals defer. The fixed label set keeps that future aggregation
  cheap.
- Rewrite-forcing risk: none at 10x within a single process — logs and
  pull-metrics scale with the monolith. The forcing step is horizontal
  scale-out (multiple workers/hosts), which needs external aggregation;
  distributed tracing / external APM is the explicit non-goal that
  would force that infrastructure.

## Non-goals

- Distributed tracing, external APM integrations, dashboards, and
  cross-worker metric aggregation (single-process counters only in v1).
- Admin/management UI, and any write path through the API (the CLI is
  the only operator write surface; `/metrics` is read-only).
- New CLI/metrics dependencies (stdlib `argparse` + hand-written
  Prometheus text only; dep requests go to the contracts agent).
- Log sampling/retention infrastructure, log shipping backends, and
  structured-log *querying* (operators grep files / scrape `/metrics`).
- Owning indexer behavior: file parsing, crawling, and reindex
  semantics belong to the crawler-index plan (T8, seam S7); this plan
  owns only the CLI surface and its delegation contract.
- Per-request provider selection (`--provider`) until the federated
  plan's `provider_prefs` exists (T13) — no parsed-and-ignored flags.

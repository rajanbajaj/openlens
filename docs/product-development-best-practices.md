# Product development best practices (OpenLens lifecycle)

How this app is built, gated, released, and operated. Each practice
names the file or workflow that enforces it, so a new contributor can
verify the claim instead of trusting this page.

## 1. Releases ride git tags, never branch pushes

- CI (`.github/workflows/ci.yml`) runs on every push and pull request.
  It never deploys.
- Only a pushed tag matching `v*` triggers
  (`.github/workflows/release.yml`): check out the tag, install the
  server-side secrets file, `docker compose up --build`, then health
  gates (backend `/healthz`, frontier status, a real search, both
  frontends) before the release counts as green.
- Releases are serialized (`concurrency: release-production`,
  `cancel-in-progress: false`) with a 45-minute timeout.
- Release plans live in `docs/releases/` (e.g. the search-gaps plan);
  the deploy mechanics live in `deploy/local/README.md`.
- Docs never carry infrastructure specifics: no server addresses,
  paths, or domains. Redaction is a release-blocking habit, not a
  cleanup step.

## 2. CI is the definition of done

Backend (`uv`, Python 3.12, `uv sync --group dev`):

1. `ruff check src tests` and `ruff format --check src tests`
2. `mypy src` (96+ source files, zero-error policy)
3. `pytest tests -q` (full suite, offline — see §5)
4. `pip-audit` for Python dependencies

Frontend (`frontend/`, Node 20, `npm ci`):

1. `npm run lint` (eslint, flat config in `eslint.config.mjs`)
2. `npm run format:check` (prettier, `.prettierrc`)
3. `npm test` (vitest, run once — no watch in CI)
4. `npm run build` (`tsc` + vite, so type errors fail the build)
5. `npm audit --omit=dev` (dev-only findings are recorded as
   acknowledged risk with the reason inline, never silently ignored)

Secrets (`fetch-depth: 0` + `gitleaks/gitleaks-action`): every commit
message and diff is scanned for leaked credentials.

## 3. Contracts first, plugins behind them

- `src/openlens/core/interfaces.py` owns the stable shapes
  (`SearchRequest`, `SearchResult`, `SearchResponse`); the pipeline
  owns the stages. Providers, plugins, and the ranker meet at these
  interfaces, never at each other's internals.
- The 10-stage pipeline (`core/pipeline.py`) declares per plugin:
  stage, type, dependencies, and whether it is mandatory
  (fail closed) or optional (failures isolated into diagnostics).
- The agent layer (`retrieval/agent.py`) reuses the same in-process
  engine the search route uses — no second retrieval path to drift.

## 4. Fail closed, degrade openly

- Mandatory filters run before anything user-visible; nothing
  unfiltered ever renders because a cache or shortcut fired first.
- Errors are typed (`ProviderError` with codes like `UNAVAILABLE`,
  `TIMEOUT`), wrapped at boundaries, and mapped to HTTP envelopes in
  one place (`api/errors.py`). Raw backend messages never leak.
- AI features degrade to honest non-AI output: agent chat serves
  standard results with an `agent_status` notice when no model
  backend is enabled, and extractive fallbacks when synthesis fails.
- Admin reads stay open in local dev but every mutation requires the
  single `X-Admin-Token` gate — including the frontier websocket,
  which accepts the token as a header or `Sec-WebSocket-Protocol`
  value because browsers cannot set handshake headers.
- Abuse is budgeted, not trusted: per-IP fixed-window rate limiting
  (429 past budget, `api/rate_limit.py`) and a global request-body cap
  (413 past `OPENLENS_MAX_BODY_BYTES`, default 1 MiB,
  `api/body_limit.py`). Both are standard-library only — no new
  dependency for the blast door.

## 5. Tests prove behavior, offline

- `uv run pytest tests -q` needs no network, keys, or cluster:
  providers and models are fakes, Kafka/Postgres paths have
  in-process doubles.
- Fix the root cause, not the symptom; cover the error and boundary
  inputs (empty/malformed/blocked), not just the happy path.
- A pinned failing test is a contract: update the expectation only
  when the task explicitly changes the contract (e.g. a new wire
  field), never to make a red suite green.
- Wire changes are additive: new response fields degrade to absent
  on old clients (`PerspectiveViewSchema.from_mapping`,
  frontend `readPerspectives`), and the checked-in fixture test
  (`response.fixture.test.ts`, re-captured from a real backend
  envelope) trips on contract drift.
- Durable collateral rule: a bug fix ships with the smallest focused
  regression test in the repo's normal layout; one broad suite run
  after the final edit, not after every step.

## 6. The crawl is the only truth

- No external search dependency: the Postgres-backed custom index
  is the searchable corpus. Documents carry metadata JSON plus
  promoted columns — never an opaque blob the UI must parse.
- Ingest normalizes aggressively (URL canonicalization, junk-link
  filtering, push-time domain/policy/robots gates, per-page outlink
  caps) and validates content (validity/quality gate) before it can
  rank.
- Priority is explicit and decaying (seed priority 5 → children
  4 → 3 → 2 …), same-domain scoped, with owner leases on claimed
  rows so a dead worker never loses or duplicates work.
- Scale is a consumer-group dial, not a redesign: Kafka topic
  `crawl.urls` + `--scale crawler=N`; offsets commit only after a
  URL is crawled and indexed (at-least-once + store dedupe).
- Freshness is two-track: requeue stale rows for age refresh,
  sitemap/RSS discovery feeds for new URLs.
- Seed lists and `scripts/cleanup.sh` (full reset) are checked in,
  so any environment can be rebuilt or wiped deterministically.
- Throughput is budgeted against the broker: `max_pages_per_poll`
  (default 25) keeps worst-case poll work under ~1/4 of
  `max_poll_interval_ms`, so a busy consumer is never mistaken for a
  dead one and rebalanced; link fan-out is one broker flush per drain
  (`KafkaQueue.publish_many`), not one round-trip per link.
- The worker degrades instead of dying: per-link publish, eviction,
  discovery, and reporting failures are suppressed at the boundary —
  the store row is the durable record — so a full queue or a down
  Kafka stalls progress but never crash-loops the fleet.
- Workers are visible from second one: a startup banner plus a forced
  heartbeat before the first drain, so a long initial backlog reads
  as alive-with-work instead of silent-and-missing.
- The database is sized on paper first: per-process pools × replicas
  against `POSTGRES_MAX_CONNECTIONS`, and container `/dev/shm`
  (Docker default 64 MB) against Postgres parallel-query needs
  (`shm_size: 256mb`) — each bitten once in production, each now in
  `deploy/local/docker-compose.yml` with the math inline.

## 7. Secrets never touch the repo

- `.env.example` files document every variable; real values live in
  gitignored `.env` files or the server-side secrets file the
  release workflow copies in. The loader reads process env only
  (no dotenv dependency).
- Secret-shaped config values are redacted at every boundary:
  startup validation, admin config reads (`REDACTED_SECRET`
  sentinel on write), logs, diagnostics, and metrics.
- Missing required secrets fail startup loudly (`ConfigError`),
  never with a half-booted server.

## 8. Frontend: generated client, guarded types, tested UI

- The API client is generated, not handwritten: backend
  `openapi.json` snapshots are checked in per app, and
  `npm run api:gen` (orval) regenerates `src/api/generated/`.
  Regenerate after every backend route/schema change and commit
  the output.
- Hand-mirrored `api/types.ts` pairs with runtime guards in
  `api/guard.ts` that narrow unknown payloads and degrade
  malformed fields to absent — the page never crashes on a bad
  envelope.
- Tests colocate with components (`*.test.tsx`): behavior first
  (renders, empty states, disclosures), a11y roles and labels
  asserted, fetch stubbed per test.
- Accessibility is a feature gate: labelled inputs, focus
  management (autofocus on the landing hero only — never on the
  results header), `aria-live` announcements, keyboard maps on the
  combobox, and capability-gated enhancements (voice input renders
  nothing where the Web Speech API is absent).
- One control, one labelling path: a wrapping `<label>` plus a
  redundant `aria-label` on the same input resolves twice in Testing
  Library and breaks `getByLabelText` — associate once. Both `Select`
  and `FilterToggle` learned this from red CI, not from review.
- Query what the user would wait for: the results section remounts
  on every refetch, so post-refetch assertions use `findBy*` (waits
  for the remount) instead of assuming the row survived the
  loading gap. CI runners are ~4x slower than laptops; timing
  assumptions that pass locally fail there first.
- Styling uses design tokens + Tailwind utilities; no gradients,
  no emoji iconography, no nested-card or glow-card stacks.
  Prettier and eslint run in CI, so formatting debates end there.

## 9. Observe everything, log no queries

- One JSON log line per request (INFO): latencies, plugin
  executions, error counts, request id — never the raw query
  (DEBUG only).
- Every response carries its request id twice
  (`diagnostics.request_id` + `X-Request-Id` header echo).
- `GET /metrics` (opt-in) exposes Prometheus counters/histograms
  with fixed label names — query text, doc ids, and error strings
  never become labels.
- Dashboards over polling: the admin live socket pushes frontier
  status; pgAdmin and the Kafka UI ship in compose for storage
  and queue inspection.
- `docs/troubleshoots/` records each production incident and its
  fix next to the code it disciplines.

## 10. Scale reads and writes independently

- Search reads (`search`, `list_titles`, `list_domains`, `count`,
  `get`, …) route to an async streaming replica
  (`postgres-replica`, `PostgresDocumentStore(replica=…)`); every
  write stays on the primary, so crawl write pressure cannot starve
  search past the 5 s provider timeout that first proved the need.
- The replica is best-effort with three fallback layers: lazy pool
  build (the backend boots while the replica is still basebacking
  up), primary fallback on pool failure or saturation, and a
  3-strike circuit breaker for mid-use failures. A sick replica
  degrades to primary-only search, never to failed searches.
- Freshness has a price: async lag (usually ms–s) delays newly
  crawled docs in results — monitored via
  `pg_last_xact_replay_timestamp()`, never assumed zero. Crawlers
  stay primary-only on purpose, so dedup checks never read stale
  rows.

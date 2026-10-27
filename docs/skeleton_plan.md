# OpenLens — Implementation Plan

## Goal

Build the OpenLens initial skeleton: a working single-provider Python search engine where search backends (custom index, OpenSearch, external APIs like Google/Bing) and AI backends (Muse, local/self-hosted models, Claude) are both plug-and-play via config, with filtering, ranking, and enrichment owned by independent plugins behind stable interfaces.

## Success Criteria

The skeleton is done when all 11 items from the spec's Definition of Done hold, verified by tests: working search through a configured provider, custom index behind the provider interface, config-driven provider selection, shared data model, defined pipeline stages, plugins toggleable without core edits, a filter that excludes, a scorer that reorders, a normalized API response with diagnostics, and unit plus integration coverage of the provider and plugin contracts.

## Approach

Workspace facts shaping this plan: the scaffold is fully empty (0-byte `main.py` and `README.md`, no files under any `src/openlens/*/` subpackage, no `__init__.py` files, empty `configs/` and `tests/`), so there is no existing engine to refactor — Phase 1 of the spec becomes a greenfield build, not a refactor. `pyproject.toml` is initialized (hatchling, `src` layout, pytest `testpaths=["tests"]`). Toolchain: system Python 3.14.7 with `uv` available; the checked-in `.venv` has a broken `pip` shim and must be recreated, not repaired. There is no git repo, so no history constraints.

Key decisions:

| Decision | Recommendation | Why |
|---|---|---|
| MVP storage | SQLite FTS5 via stdlib `sqlite3` | Zero new infra; spec allows it; custom inverted index and crawler deferred to later |
| API framework | FastAPI, per spec | Already specified; single `/api/v1/search` route |
| Plugin discovery | Config-listed `module:Class` import paths, validated at startup | No new dependency; entry-points add packaging friction for a monolith |
| Score fusion | Reciprocal rank fusion, per spec | Avoids calibrating raw cross-provider scores |
| AI backends | New `AIModelProvider` interface, separate from `SearchProvider` | Spec under-specifies this; your plug-and-play requirement for Muse/local/Claude needs its own registry, config section, and fake stub for offline tests |

Deliberate additions to the spec: (1) an `AIModelProvider` interface (`complete`, `embed`, `classify`, `configure`, `health_check`) with a `models.yaml` config section and a `FakeModelProvider` for tests; (2) a generic `ExternalSearchProvider` base class so Google/Bing/custom REST APIs differ only by request/response mapping, with only one concrete adapter built in the skeleton; (3) explicit `__init__.py` files in every package. Non-goals for the skeleton: web crawler, OpenSearch cluster dependency (adapter ships, disabled by default), federated search (interfaces ready, strategy added later), real AI model calls (interface plus fake only).

## Steps

1. **Repo hygiene.** Recreate `.venv` with `uv`, add `__init__.py` files, and declare all runtime dependencies upfront in `pyproject.toml`: `fastapi`, `uvicorn`, `pyyaml`, `httpx`, `opensearch-py` (plus existing `pytest` dev group). Only this step edits `pyproject.toml`; later steps request additions in their reports instead of editing it. No code behavior here.
2. **Core contracts (`core/interfaces.py`, `core/context.py`, `config.py`).** Dataclasses for `Document`, `SearchRequest`, `SearchResult`, `SearchResponse`, `SearchContext`; abstract `SearchProvider` (`search`, `health_check`, `configure`) and `SearchPlugin` (name, type, stage, config schema, dependencies, mandatory/optional failure); stage enum matching the spec's 10-stage pipeline; `Engine` public signatures; and the shared config-loader interface (`load_config` with `${ENV_VAR}` resolution, startup validation error) implemented here in `config.py` so provider, plugin, and AI waves all code against one loader.
3. **Custom index provider first (`storage/`, `indexing/`, `providers/custom_index.py`).** SQLite FTS5 repository, minimal indexer over local seed documents, `CustomIndexProvider` implementing the provider interface, query parser plus normalizer and deduplicator in `retrieval/`. This makes search work end-to-end before any abstraction beyond it.
4. **Provider system (`providers/manager.py`, `providers/registry.py`, `providers/capabilities.py`, `providers/base.py`).** Build on the step-2 config loader: startup validation instantiating only enabled providers, bounded timeouts, single-provider strategy, health checks. Ship `opensearch.py` adapter and generic `ExternalSearchProvider` base plus one concrete external adapter, both disabled by default so tests need no network or keys.
5. **Plugin pipeline (`core/pipeline.py`, `core/engine.py`, `core/registry.py`, `plugins/`).** Deterministic stage runner with dependency and ordering validation, fail-closed mandatory filters versus isolated optional-plugin failures, baseline relevance scorer, one exclusion filter (child-safe/blocklist), one trust-warning enricher, explanation assembly preserving `original_score` vs `final_score` plus per-result `reasons`/`warnings`.
6. **Ranking profiles (`ranking/ranker.py`, `ranking/profiles.py`, `configs/ranking.yaml`).** Named per-mode profiles (default, child-safe, news, shopping, travel) combining named signals; mode selected from `SearchRequest`.
7. **API and observability (`api/schemas.py`, `api/routes.py`, `main.py`, `configs/*.yaml`).** `POST /api/v1/search` with validation, pagination, applied-filter reporting, consistent errors; per-provider latency, failure, and plugin-execution diagnostics in the response; fallback strategy (primary plus bounded fallback, distinguishing failure from empty).
8. **AI seams (`models/` interface, `models.yaml`, `FakeModelProvider`, one AI plugin stub).** `AIModelProvider` registry mirroring the search-provider pattern; one AI plugin (e.g. summarization or classification) wired through the interface but disabled by default; no live model calls in the skeleton.
9. **Contract and quality tests.** Provider-conformance tests every provider must pass (normalization shape, pagination, timeout, failure mapping), plugin-conformance tests (stage declaration, mandatory fail-closed, optional isolation), cross-provider dedup test, config-toggle tests (enable/disable without core changes), secret-leak test (no keys in responses/logs), and a small repeatable fixture corpus under `tests/fixtures/` for ranking assertions. `tests/features/` exists in the scaffold but is out of scope for the skeleton.

## Validation Plan

- `uv run pytest tests/unit tests/integration` green; each step adds its tests before moving on.
- Manual: seed the SQLite index, run `uvicorn openlens.main:app`, `POST /api/v1/search` with a shopping-mode query, and confirm normalized results with `reasons`, `applied_filters`, and diagnostics.
- Contract proof: run the provider-conformance suite against `CustomIndexProvider` and the fake external provider; toggle a provider and a plugin purely via YAML and re-run to prove no core edits.
- Highest-risk check: mandatory-filter fail-closed behavior under a simulated filter outage and provider timeout — if either leaks unfiltered results or hangs past the bounded timeout, the skeleton fails its safety requirement.

## Risks / Open Questions

- SQLite FTS5 ranking signals are weaker than OpenSearch BM25; acceptable for the skeleton, revisit when the OpenSearch adapter is enabled against a real cluster.
- Which external API to concretize first (Bing vs Google Programmable Search) is deferred to the federated-search phase; the generic base keeps this reversible. Assumption: Bing first, per the spec.
- The `LISCENSE.md` filename typo predates this work; renaming it is out of scope unless you want it fixed.
- Open questions: none — all material choices resolve to the spec or the workspace facts above.

# Task 21 — Caching layer audit (switchable cache providers, like storage)

## Problem

Every search currently pays full price on every request: provider fan-out,
filter/scoring/rerank plugin execution, and AI calls re-run for identical or
near-identical queries. Task 17 gave storage a swappable-backend pattern
(`DocumentStore` ABC + `storage.backend` switch); caching needs the same
treatment before ad-hoc caches bake in.

## Goal

A detailed audit that answers: what to cache, where it hooks into the
pipeline, what the invalidation story is, and what a switchable provider
design looks like — followed by implementation of that design.

## Audit questions (must all be answered in the audit section)

1. **Cacheable units** — provider raw results? normalized candidates?
   post-filter ranked lists? AI outputs (understanding labels, summaries)?
   Rank each by hit-rate potential vs staleness risk.
2. **Key design** — what identifies a cache entry (normalized query +
   mode + filters hash + provider set + ranking profile version)? What must
   bust the key (config change, index write, ranking weight edit)?
3. **Invalidation** — TTL defaults per unit; write-through/invalidate on
   index (`add_documents`/`delete`) for provider-result caches; manual purge
   entry point (CLI + config flag).
4. **Failure semantics** — cache MUST fail open: a dead/unreachable cache
   degrades to uncached search, never blocks or empties results (explicit
   contrast with safety filters, which fail closed). Every cache read/write
   is wrapped; failures emit metrics + diagnostics, not errors.
5. **Privacy** — cached entries inherit query/result sensitivity: no cache
   key or value may leak secrets to logs; per-user restrictions
   (`user_restrictions` filters) must be part of the key, never shared
   across users.
6. **Backends** — at minimum: `memory` (bounded LRU, zero deps, default),
   `redis`, and one file-backed (`sqlite`) option; driver/dependency rules
   mirror task 17 (lazy imports, missing driver → disabled cache with a
   startup warning, never a crash).

## Design (mirrors task 17)

- `CacheProvider` ABC (`get/set/invalidate/clear/health_check/close`) in
  `src/openlens/cache/` (new top-level area, not inside `storage/`).
- Top-level `cache:` config section with a single `backend` switch
  (`memory` default), per-backend sections, TTLs, size bounds.
- Exactly one pipeline hook point with a bounded key (no unbounded key
  growth: hash long queries, cap key length); cache sits after mandatory
  filtering (never cache pre-safety candidates) and records hit/miss in
  diagnostics + `/metrics` (fixed labels, gated feed point per task 15).
- Env-gated conformance suite per backend, same pattern as
  `test_storage_mysql.py` / `test_storage_postgres.py`.

## Out of scope

- No semantic/fuzzy cache matching (exact normalized keys only; semantic
  cache is a future task if hit rates justify it).
- No cache warming, no distributed invalidation beyond single-instance TTL.
- No changes to ranking weights or provider logic to "help" the cache.

## Definition of done

1. Audit section in the plan answers all six questions with concrete
   choices (units, key schema, TTLs, backends).
2. `CacheProvider` ABC + `memory` backend + one external backend land behind
   the `cache.backend` switch; default config behavior unchanged
   (memory backend on, or cache off-by-default — audit decides, config
   documents it).
3. Post-filter hook only; safety filters never see cached pre-filter data
   (regression test proves a blocked result stays blocked on cache hit).
4. Cache-down test: search succeeds uncached with a hit/miss/free metric
   recorded; user-scoped keys never collide across restriction sets.
5. Full suite green, ruff/format/mypy clean, pip-audit clean (new deps
   declared as extras like task 19).

## Verification

- Hit test: identical query twice → second served from cache (diagnostics
  flag), byte-identical results.
- Staleness test: index write invalidates affected entries (miss after
  write); TTL expiry covered with short-TTL config.
- Fail-open test: cache backend killed/unreachable → search 200, uncached,
  metric recorded.
- Safety test: child-safe-blocked document never returned via cache hit.
- Live-backend conformance green with zero skips where CI provides servers.

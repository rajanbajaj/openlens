"""Switchable search-result cache (Task 21).

Audit answers (concrete choices):

1. Cacheable unit: the final post-explanation ranked list (results +
   total + applied_filters), stored pre-slice so pages share entries.
   Ranking: highest hit-rate (skips provider fan-out, every plugin, the
   ranker, and AI enrichment in one lookup) with staleness bounded by
   TTL + write invalidation. Per-plugin or AI-only units were rejected:
   two cache layers would double the key/invalidation surface for less
   saving, since the pipeline dominates cost.
2. Key: ``olcache:v1:<sha256>`` over canonical JSON (sorted keys) of
   normalized query + effective mode + sorted filters + provider prefs +
   configured provider names + ranking profile + fetch depth. Long
   queries are always hashed: keys are fixed-length, never unbounded.
   User-restriction filters are part of the key, so entries never cross
   users; secrets never appear (the key is a hash, values hold only
   already-filtered results, and nothing cache-shaped is ever logged).
3. Invalidation: TTL per entry (default 120 s) is the primary guard —
   cross-process index writes (the CLI indexer) cannot reach an
   in-process memory cache, so TTL bounds their staleness.
   Same-process writes call :meth:`SearchEngine.invalidate_cache`;
   operators purge manually via ``python -m openlens.cli cache purge``.
4. Failure semantics: fail OPEN. Every cache read/write in the engine
   hook is wrapped; a dead backend degrades to uncached search and
   records a miss/error metric plus a diagnostics note — never an error.
   (Explicit contrast with safety filters, which fail closed.)
5. Privacy: per-user restriction sets are in the key; cached values are
   post-mandatory-filter results, so a blocked document can never enter
   the cache. Keys, values, and TTLs stay out of logs and metrics
   (fixed ``result`` label only).
6. Backends: ``memory`` (bounded LRU, zero deps, default), ``sqlite``
   (file-backed, stdlib), ``redis`` (lazy driver, env-gated tests).
   Missing/unreachable drivers disable caching with a startup warning,
   never a crash — the task-17 driver rules.
"""

from openlens.cache.base import (
    CACHE_KEY_PREFIX,
    CACHE_KEY_VERSION,
    CacheProvider,
    build_cache_key,
    restore_cached_page,
    serialize_cached_page,
)
from openlens.cache.factory import SUPPORTED_CACHE_BACKENDS, build_cache_provider

__all__ = [
    "CACHE_KEY_PREFIX",
    "CACHE_KEY_VERSION",
    "SUPPORTED_CACHE_BACKENDS",
    "CacheProvider",
    "build_cache_key",
    "build_cache_provider",
    "restore_cached_page",
    "serialize_cached_page",
]

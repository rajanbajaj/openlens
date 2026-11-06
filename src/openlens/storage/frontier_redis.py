"""Redis-backed persistent URL frontier (hot path for shared-queue crawls).

Mirrors :class:`openlens.storage.frontier.FrontierStore` over Redis so
multi-replica crawlers share one queue without Postgres write pressure:

- ``{p}:seen`` SET deduplicates normalized URLs (``SADD``; ``0`` means
  known, like the Postgres primary-key dedup).
- ``{p}:ready`` / ``{p}:active`` ZSETs order the queue. Score is
  ``priority * 1_000_000 - depth`` plus a sub-unit fraction that grows
  with the discovery sequence, so ties break by discovery order without
  a table scan (plan step 2: ``priority DESC, depth ASC``).
- ``{p}:meta:<sha1>`` HASH carries the row (url, depth, parent_url,
  priority, domain, status, attempts, last_error, etag, last_modified,
  owner, leased_at, heartbeat_at, lease_ttl_s, discovered_at, seq).
- ``{p}:claim:<sha1>`` STRING holds the owner with ``PX lease_ttl_s``
  (``SET NX PX`` on acquire, ``PEXPIRE`` on heartbeat); expiry is
  automatic, so crashed claims self-clean server-side.
- ``{p}:backoff`` ZSET (score = unix-second ``not_before``) parks slow
  retries; the main dequeue/claim path never takes them.
- ``{p}:stats:<event>`` STRING counters (``enqueued, claimed, done,
  failed, skipped, backoff``) feed :meth:`snapshot_stats` for the
  PASS-2 mirror flusher to diff.

The driver imports lazily: a missing ``redis`` package or an
unreachable server raises :class:`ProviderError` with ``UNAVAILABLE``
at factory time, exactly like the cache Redis precedent. Input
validation fails closed exactly like the sibling backends (bad
limit/offset clamps to empty, malformed domains return ``[]``/``0``,
blank worker ids raise ``INVALID_REQUEST``) before any Redis call.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

from openlens.core.interfaces import ConfigError, ProviderError
from openlens.storage.frontier import (
    DEFAULT_CRAWL_ID,
    DEFAULT_HEARTBEAT_INTERVAL_S,
    DEFAULT_LEASE_TTL_S,
    DEFAULT_SETTINGS_ID,
    PHASE_RUNNING,
    STATUS_ACTIVE,
    STATUS_BACKOFF,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_SKIPPED,
    VALID_CRAWL_REQUEST_DECISIONS,
    CrawlCheckpoint,
    FrontierEntry,
    FrontierStore,
    _checkpoint_from_parts,
    _cutoff_iso,
    _domain_of_url,
    _utcnow,
    _utcnow_precise,
    host_matches_skip_domains,
    normalize_frontier_url,
    normalize_priority,
    normalize_skip_domain,
    normalize_skip_domains,
)

_PROVIDER_NAME = "custom_index"

#: Backend names accepted by the ``frontier.backend`` switch.
SUPPORTED_FRONTIER_BACKENDS = frozenset({"sqlite", "postgres", "redis"})

#: Redis URL default. DB index ``1`` on purpose: the cache Redis uses
#: DB ``0`` (plan step 6 requires a separate index).
DEFAULT_REDIS_URL = "redis://localhost:6379/1"

#: Default key namespace; every key below is ``{prefix}:...``.
DEFAULT_KEY_PREFIX = "fw"

#: Default mirror-flush cadence (seconds) consumed by PASS-2.
DEFAULT_FLUSH_INTERVAL_S = 5.0

#: Event counters maintained under ``{prefix}:stats:<event>``.
_STAT_EVENTS = ("enqueued", "claimed", "done", "failed", "skipped", "backoff")

#: Row statuses tracked by the ready/active/backoff ZSETs + metas.
_STATUSES = (
    STATUS_QUEUED,
    STATUS_ACTIVE,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_SKIPPED,
    STATUS_BACKOFF,
)

#: Terminal statuses revivable by :meth:`reseed`.
_TERMINAL_STATUSES = frozenset({STATUS_FAILED, STATUS_SKIPPED, STATUS_DONE})

#: Score quantum: priority steps of this size dominate depth + tiebreak.
_PRIORITY_QUANTUM = 1_000_000.0

#: Active-claim meta TTL multiplier (a crashed claim's HASH self-cleans;
#: terminal rows persist for the mirror). Always >= the lease TTL.
_META_TTL_MULTIPLIER = 2

#: HGETALL commands per pipeline EXECUTE in :meth:`_iter_metas`. A full
#: scan at backlog scale (250k+ URLs) in one pipeline can never fit the
#: 2s client socket timeout under load, killing the caller; chunked
#: executes degrade to slow instead of dying.
_META_PIPELINE_BATCH = 500


def _score(priority: int, depth: int, seq: int) -> float:
    """Queue score: ``priority DESC, depth ASC``, ties by discovery order.

    Queues pop with ``ZPOPMAX`` (highest score first). The fractional
    part ``1/(seq+1) - 1`` stays in ``[-1, 0)`` and shrinks with the
    discovery sequence, so rows sharing ``(priority, depth)`` pop
    oldest-first while never crossing into the next integer bucket.
    """
    frac = 1.0 / (float(seq) + 1.0) - 1.0
    return float(priority) * _PRIORITY_QUANTUM - float(depth) + frac


def _sha(url_norm: str) -> str:
    # Non-security key derivation (Redis key shortening only).
    return hashlib.sha1(url_norm.encode("utf-8"), usedforsecurity=False).hexdigest()


def _host_bucket(url_norm: str) -> str:
    """Host rollup bucket mirroring the SQL failed/queued domain views.

    ``http``/``https`` URLs bucket by lowercase host; anything else (or
    no host) buckets as ``(invalid)``, like the SQL ``CASE`` branches.
    """
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url_norm.strip())
    except ValueError:
        return "(invalid)"
    if parts.scheme.lower() not in ("http", "https"):
        return "(invalid)"
    host = (parts.hostname or "").lower()
    return host or "(invalid)"


@dataclass
class FlushRecord:
    """One buffered frontier mutation for the PASS-2 mirror flusher.

    The store appends terminal transitions + discovery inserts (and
    checkpoint/settings saves) to an in-memory deque alongside the
    Redis write; :meth:`RedisFrontierStore.drain_pending_flush` hands
    them to the flusher in batches. ``kind`` is one of ``done``,
    ``failed``, ``skipped``, ``enqueued``, ``checkpoint``, ``settings``.
    """

    kind: str
    url: str = ""
    depth: int = 0
    parent_url: str | None = None
    priority: int = 0
    attempts: int = 0
    last_error: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    owner: str | None = None
    ts: str = ""


def validate_frontier_section(section: Any) -> dict[str, Any]:
    """Normalize the ``frontier:`` config section.

    Raises :class:`ConfigError` on malformed sections or unknown
    backend names (mirrors ``cache/factory.py:validate_cache_section``)
    so boot fails loudly at startup. Absent section means fall-through
    defaults (never Redis).
    """
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise ConfigError("Config section 'frontier' must be a mapping.")
    backend = section.get("backend", "postgres")
    if not isinstance(backend, str) or backend not in SUPPORTED_FRONTIER_BACKENDS:
        raise ConfigError(
            f"Unknown frontier backend {backend!r}. "
            f"Supported: {sorted(SUPPORTED_FRONTIER_BACKENDS)}. "
            "Startup validation failed."
        )
    return dict(section)


def _validate_redis_section(section: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize ``frontier.redis``; fail closed on malformed values."""
    redis_cfg = section.get("redis", {})
    if redis_cfg is None:
        redis_cfg = {}
    if not isinstance(redis_cfg, dict):
        raise ConfigError("Config section 'frontier.redis' must be a mapping.")
    url = redis_cfg.get("url", DEFAULT_REDIS_URL)
    if not isinstance(url, str) or not url:
        raise ProviderError(
            "Config section 'frontier.redis.url' must be a non-empty string.",
            provider_name=_PROVIDER_NAME,
            code=ProviderError.UNAVAILABLE,
        )
    prefix = redis_cfg.get("key_prefix", DEFAULT_KEY_PREFIX)
    if not isinstance(prefix, str) or not prefix:
        raise ConfigError(
            "Config section 'frontier.redis.key_prefix' must be a non-empty string."
        )
    # Numeric values arrive as strings when env-interpolated
    # (``${VAR:-5}``); coerce, mirroring the ``int(...)`` use-sites of
    # the sibling ``frontier.lease_ttl_s`` values.
    try:
        flush_s = float(redis_cfg.get("flush_interval_s", DEFAULT_FLUSH_INTERVAL_S))
    except (TypeError, ValueError):
        raise ConfigError(
            "Config section 'frontier.redis.flush_interval_s' "
            "must be a positive number."
        ) from None
    if (
        isinstance(redis_cfg.get("flush_interval_s"), bool)
        or not math.isfinite(flush_s)
        or flush_s <= 0
    ):
        raise ConfigError(
            "Config section 'frontier.redis.flush_interval_s' "
            "must be a positive number."
        )
    try:
        lease_f = float(redis_cfg.get("lease_ttl_s", DEFAULT_LEASE_TTL_S))
    except (TypeError, ValueError):
        raise ConfigError(
            "Config section 'frontier.redis.lease_ttl_s' must be a positive int."
        ) from None
    if (
        isinstance(redis_cfg.get("lease_ttl_s"), bool)
        or not math.isfinite(lease_f)
        or not lease_f.is_integer()
        or lease_f <= 0
    ):
        raise ConfigError(
            "Config section 'frontier.redis.lease_ttl_s' must be a positive int."
        )
    try:
        heartbeat_f = float(
            redis_cfg.get("heartbeat_interval_s", DEFAULT_HEARTBEAT_INTERVAL_S)
        )
    except (TypeError, ValueError):
        raise ConfigError(
            "Config section 'frontier.redis.heartbeat_interval_s' "
            "must be a positive number."
        ) from None
    if (
        isinstance(redis_cfg.get("heartbeat_interval_s"), bool)
        or not math.isfinite(heartbeat_f)
        or heartbeat_f <= 0
    ):
        raise ConfigError(
            "Config section 'frontier.redis.heartbeat_interval_s' "
            "must be a positive number."
        )
    return {
        "url": url,
        "key_prefix": prefix,
        "flush_interval_s": flush_s,
        "lease_ttl_s": int(lease_f),
        "heartbeat_interval_s": heartbeat_f,
    }


def build_redis_frontier_store(section: Mapping[str, Any] | None) -> RedisFrontierStore:
    """Build a :class:`RedisFrontierStore` from a ``frontier:`` section.

    Unknown backends raise :class:`ConfigError`; a missing/empty URL or
    an unreachable server raises :class:`ProviderError` (``UNAVAILABLE``).
    """
    normalized = validate_frontier_section(section)
    redis_cfg = _validate_redis_section(normalized)
    return RedisFrontierStore(
        redis_cfg["url"],
        key_prefix=redis_cfg["key_prefix"],
        flush_interval_s=redis_cfg["flush_interval_s"],
        lease_ttl_s=redis_cfg["lease_ttl_s"],
        heartbeat_interval_s=redis_cfg["heartbeat_interval_s"],
    )


def open_frontier_store_for_crawl(config: Mapping[str, Any]) -> FrontierStore:
    """Dispatch the crawl-path frontier store off ``frontier.backend``.

    When ``frontier.backend == redis``, constructs
    :class:`RedisFrontierStore` from ``frontier.redis``; otherwise falls
    through to :func:`open_frontier_store` (the ``storage:``-keyed
    SQLite/Postgres store) unchanged. The fall-through import stays
    function-local so test doubles patching
    ``openlens.storage.frontier.open_frontier_store`` keep working.
    """
    from openlens.storage.frontier import open_frontier_store

    if not isinstance(config, dict):
        return open_frontier_store(config)
    section = config.get("frontier", None)
    if section is None:
        return open_frontier_store(config)
    normalized = validate_frontier_section(section)
    if normalized.get("backend", "postgres") != "redis":
        return open_frontier_store(config)
    return build_redis_frontier_store(normalized)


class RedisFrontierStore(FrontierStore):
    """Redis-backed frontier: shared leases/queues over the §3 key layout.

    Single-worker behavior matches :class:`SqliteFrontierStore`
    (dedup, attempt budget, priority/depth/discovery ordering, lease
    ownership + expiry); coordination is server-side so concurrent
    processes share it. Multi-step transitions hold a per-process lock
    (same convention as the SQLite store); cross-process claim races
    resolve server-side (``ZPOPMAX`` pops atomically, claim keys use
    ``SET NX PX``).
    """

    def __init__(
        self,
        url: str,
        *,
        key_prefix: str = DEFAULT_KEY_PREFIX,
        flush_interval_s: float = DEFAULT_FLUSH_INTERVAL_S,
        lease_ttl_s: int = DEFAULT_LEASE_TTL_S,
        heartbeat_interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S,
        socket_timeout_s: float = 2.0,
    ) -> None:
        if not isinstance(url, str) or not url:
            raise ProviderError(
                "RedisFrontierStore 'url' must be a non-empty string.",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.UNAVAILABLE,
            )
        if not isinstance(key_prefix, str) or not key_prefix:
            raise ConfigError(
                "RedisFrontierStore 'key_prefix' must be a non-empty string."
            )
        try:
            import redis  # type: ignore[import-not-found]  # lazy import
        except ImportError as exc:
            raise ProviderError(
                "frontier redis backend needs the 'redis' package",
                provider_name=_PROVIDER_NAME,
                retryable=False,
                code=ProviderError.UNAVAILABLE,
            ) from exc
        self._url = url
        self._prefix = key_prefix
        self._flush_interval_s = float(flush_interval_s)
        self._lease_ttl_s = max(1, int(lease_ttl_s or DEFAULT_LEASE_TTL_S))
        self._heartbeat_interval_s = float(
            heartbeat_interval_s or DEFAULT_HEARTBEAT_INTERVAL_S
        )
        self._lock = threading.Lock()
        self._pending: deque[FlushRecord] = deque()
        self._closed = False
        self._client: Any = redis.Redis.from_url(
            url, socket_timeout=socket_timeout_s, decode_responses=True
        )
        try:
            self._client.ping()
        except Exception as exc:
            with contextlib.suppress(Exception):
                self._client.close()
            raise ProviderError(
                f"frontier redis backend unreachable: {exc}",
                provider_name=_PROVIDER_NAME,
                retryable=False,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    # -- key layout -----------------------------------------------------
    def _k(self, *parts: str) -> str:
        return ":".join((self._prefix, *parts))

    @property
    def _seen(self) -> str:
        return self._k("seen")

    @property
    def _ready(self) -> str:
        return self._k("ready")

    @property
    def _active(self) -> str:
        return self._k("active")

    @property
    def _backoff(self) -> str:
        return self._k("backoff")

    def _meta(self, norm: str) -> str:
        return self._k("meta", _sha(norm))

    def _claim_key(self, norm: str) -> str:
        return self._k("claim", _sha(norm))

    def _stat(self, event: str) -> str:
        return self._k("stats", event)

    def _checkpoint_key(self, crawl_id: str) -> str:
        return self._k("checkpoint", crawl_id)

    def _settings_key(self, settings_id: str) -> str:
        return self._k("settings", settings_id)

    def _crawlreq_key(self, request_id: int) -> str:
        return self._k("crawlreq", str(request_id))

    @property
    def _crawlreq_ids(self) -> str:
        return self._k("crawlreq", "ids")

    @property
    def _crawlreq_seq(self) -> str:
        return self._k("crawlreq", "seq")

    def _crawlreq_byurl(self, norm: str) -> str:
        return self._k("crawlreq", "byurl", _sha(norm))

    @property
    def _workers(self) -> str:
        return self._k("workers")

    def _polite_key(self, host: str) -> str:
        return self._k("polite", host)

    @property
    def _seq(self) -> str:
        return self._k("seq")

    # -- small helpers --------------------------------------------------
    def _fail(self, op: str, exc: Exception) -> ProviderError:
        return ProviderError(
            f"failed to {op}: {exc}",
            provider_name=_PROVIDER_NAME,
            code=ProviderError.UNAVAILABLE,
        )

    def _entry_from_meta(self, meta: dict[str, Any]) -> FrontierEntry:
        def _int(key: str, default: int = 0) -> int:
            try:
                return int(meta.get(key, default) or default)
            except (TypeError, ValueError):
                return default

        owner = meta.get("owner")
        return FrontierEntry(
            url=str(meta.get("url", "")),
            depth=_int("depth"),
            status=str(meta.get("status") or STATUS_QUEUED),
            parent_url=meta.get("parent_url"),
            priority=_int("priority"),
            attempts=_int("attempts"),
            last_error=meta.get("last_error"),
            etag=meta.get("etag"),
            last_modified=meta.get("last_modified"),
            owner=str(owner) if owner else None,
            leased_at=meta.get("leased_at"),
            heartbeat_at=meta.get("heartbeat_at"),
            lease_ttl_s=_int("lease_ttl_s", DEFAULT_LEASE_TTL_S) or DEFAULT_LEASE_TTL_S,
        )

    def _read_meta(self, norm: str) -> dict[str, Any]:
        try:
            meta = self._client.hgetall(self._meta(norm))
        except Exception as exc:
            raise self._fail("read frontier URL", exc) from exc
        return dict(meta) if meta else {}

    def _scan_active(self, op: str) -> list[str]:
        """Members of the ``active`` ZSET (``ZSCAN``; ``SSCAN`` is set-only)."""
        try:
            return [
                str(member)
                for member, _ in self._client.zscan_iter(self._active, count=500)
            ]
        except Exception as exc:
            raise self._fail(op, exc) from exc

    def _iter_metas(self) -> Iterable[tuple[str, dict[str, Any]]]:
        """Yield ``(norm, meta)`` for every known URL (bounded scan).

        Pipelines go out in :data:`_META_PIPELINE_BATCH`-sized chunks:
        one EXECUTE per chunk bounds both the socket read and the
        server-side reply, so full scans degrade to slow instead of
        tripping the client timeout and killing the caller.
        """
        try:
            members = [str(m) for m in self._client.sscan_iter(self._seen, count=500)]
        except Exception as exc:
            raise self._fail("scan frontier URLs", exc) from exc
        out: list[tuple[str, dict[str, Any]]] = []
        for start in range(0, len(members), _META_PIPELINE_BATCH):
            batch = members[start : start + _META_PIPELINE_BATCH]
            pipe = self._client.pipeline()
            for norm in batch:
                pipe.hgetall(self._meta(norm))
            try:
                metas = pipe.execute()
            except Exception as exc:
                raise self._fail("read frontier URLs", exc) from exc
            for norm, meta in zip(batch, metas):
                if meta:
                    out.append((norm, dict(meta)))
        return out

    def _drop_stale_member(self, norm: str) -> None:
        """Forget a set member whose meta HASH is gone (TTL self-clean)."""
        pipe = self._client.pipeline()
        pipe.zrem(self._ready, norm)
        pipe.zrem(self._active, norm)
        pipe.zrem(self._backoff, norm)
        pipe.delete(self._claim_key(norm))
        pipe.srem(self._seen, norm)
        try:
            pipe.execute()
        except Exception as exc:
            raise self._fail("forget stale frontier URL", exc) from exc

    def _move_to(
        self,
        norm: str,
        meta: dict[str, Any],
        status: str,
        *,
        owner: str | None = None,
        clear_owner: bool = False,
        attempts: int | None = None,
        last_error: str | None = None,
        clear_error: bool = False,
    ) -> dict[str, Any]:
        """Move one row to ``status`` (sets + meta, one pipe)."""
        score = _score(
            int(meta.get("priority") or 0),
            int(meta.get("depth") or 0),
            int(meta.get("seq") or 0),
        )
        fields: dict[str, Any] = {"status": status}
        if clear_owner:
            fields["owner"] = ""
        elif owner is not None:
            fields["owner"] = owner
        if attempts is not None:
            fields["attempts"] = attempts
        if clear_error:
            fields["last_error"] = ""
        elif last_error is not None:
            fields["last_error"] = last_error
        pipe = self._client.pipeline()
        pipe.zrem(self._ready, norm)
        pipe.zrem(self._active, norm)
        pipe.zrem(self._backoff, norm)
        if status == STATUS_QUEUED:
            pipe.zadd(self._ready, {norm: score})
        elif status == STATUS_ACTIVE:
            pipe.zadd(self._active, {norm: score})
        elif status == STATUS_BACKOFF:
            pipe.zadd(self._backoff, {norm: time.time()})
        else:
            pipe.delete(self._claim_key(norm))
        pipe.hset(self._meta(norm), mapping=fields)
        if status == STATUS_ACTIVE:
            pipe.expire(
                self._meta(norm), max(1, self._lease_ttl_s * _META_TTL_MULTIPLIER)
            )
        try:
            pipe.execute()
        except Exception as exc:
            raise self._fail("move frontier URL", exc) from exc
        meta.update({k: str(v) for k, v in fields.items()})
        return meta

    def _queue_size(self) -> int:
        try:
            return int(self._client.zcard(self._ready) or 0)
        except Exception as exc:
            raise self._fail("count queued frontier URLs", exc) from exc

    def _activate(
        self, norm: str, meta: dict[str, Any], owner: str, ttl: int
    ) -> FrontierEntry:
        """Mark a popped/claimed row active + owned (claim key SET NX PX)."""
        stamp = _utcnow_precise()
        score = _score(
            int(meta.get("priority") or 0),
            int(meta.get("depth") or 0),
            int(meta.get("seq") or 0),
        )
        pipe = self._client.pipeline()
        pipe.zrem(self._ready, norm)
        pipe.zadd(self._active, {norm: score})
        pipe.hset(
            self._meta(norm),
            mapping={
                "status": STATUS_ACTIVE,
                "owner": owner,
                "leased_at": stamp,
                "heartbeat_at": stamp,
                "lease_ttl_s": ttl,
            },
        )
        pipe.expire(self._meta(norm), max(1, ttl * _META_TTL_MULTIPLIER))
        pipe.set(self._claim_key(norm), owner, px=max(1, ttl * 1000))
        pipe.incr(self._stat("claimed"))
        try:
            pipe.execute()
        except Exception as exc:
            raise self._fail("claim frontier URL", exc) from exc
        return self._entry_from_meta(
            {
                **meta,
                "status": STATUS_ACTIVE,
                "owner": owner,
                "leased_at": stamp,
                "heartbeat_at": stamp,
                "lease_ttl_s": ttl,
            }
        )

    def _expired_active_candidate(self, ttl: int) -> tuple[str, dict[str, Any]] | None:
        """Best expired ``active`` row for claim-stealing, or None.

        Ownerless rows and rows whose heartbeat predates ``ttl`` are
        expired; live heartbeats are never stolen. Ties break by
        ``priority DESC, depth ASC, discovery ASC`` like the main path.
        """
        cutoff = _cutoff_iso(ttl)
        best: tuple[tuple[int, int, int], str, dict[str, Any]] | None = None
        members = self._scan_active("scan active frontier URLs")
        for norm in members:
            meta = self._read_meta(norm)
            if not meta:
                self._drop_stale_member(norm)
                continue
            if str(meta.get("status") or "") != STATUS_ACTIVE:
                try:
                    self._client.zrem(self._active, norm)
                except Exception as exc:
                    raise self._fail("scan active frontier URLs", exc) from exc
                continue
            owner = str(meta.get("owner") or "")
            heartbeat = str(meta.get("heartbeat_at") or "")
            if owner and heartbeat and heartbeat >= cutoff:
                continue
            try:
                rank = (
                    -int(meta.get("priority") or 0),
                    int(meta.get("depth") or 0),
                    int(meta.get("seq") or 0),
                )
            except (TypeError, ValueError):
                rank = (0, 0, 0)
            if best is None or rank < best[0]:
                best = (rank, norm, meta)
        if best is None:
            return None
        return best[1], best[2]

    # -- queue hot path -------------------------------------------------
    def enqueue(
        self,
        url: str,
        depth: int = 0,
        parent_url: str | None = None,
        priority: int | None = None,
    ) -> bool:
        if not isinstance(url, str) or not url.strip():
            return False
        try:
            norm = normalize_frontier_url(url)
        except (AttributeError, ValueError):
            return False
        if depth is None or not isinstance(depth, int) or depth < 0:
            depth = 0
        prio = normalize_priority(priority)
        with self._lock:
            try:
                added = self._client.sadd(self._seen, norm)
            except Exception as exc:
                raise self._fail("enqueue frontier URL", exc) from exc
            if not added:
                if prio is not None:
                    try:
                        meta_key = self._meta(norm)
                        self._client.hset(meta_key, mapping={"priority": prio})
                        if self._client.zscore(self._ready, norm) is not None:
                            meta = self._read_meta(norm)
                            self._client.zadd(
                                self._ready,
                                {
                                    norm: _score(
                                        prio,
                                        int(meta.get("depth") or 0),
                                        int(meta.get("seq") or 0),
                                    )
                                },
                            )
                    except ProviderError:
                        raise
                    except Exception as exc:
                        raise self._fail("enqueue frontier URL", exc) from exc
                return False
            try:
                seq = int(self._client.incr(self._seq) or 1)
            except Exception as exc:
                raise self._fail("enqueue frontier URL", exc) from exc
            stored_prio = prio if prio is not None else 0
            fields: dict[str, Any] = {
                "url": norm,
                "depth": depth,
                "status": STATUS_QUEUED,
                "parent_url": parent_url or "",
                "priority": stored_prio,
                "domain": _domain_of_url(norm) or "",
                "attempts": 0,
                "last_error": "",
                "etag": "",
                "last_modified": "",
                "owner": "",
                "leased_at": "",
                "heartbeat_at": "",
                "lease_ttl_s": self._lease_ttl_s,
                "discovered_at": _utcnow_precise(),
                "seq": seq,
            }
            pipe = self._client.pipeline()
            pipe.hset(self._meta(norm), mapping=fields)
            pipe.zadd(self._ready, {norm: _score(stored_prio, depth, seq)})
            pipe.incr(self._stat("enqueued"))
            try:
                pipe.execute()
            except Exception as exc:
                raise self._fail("enqueue frontier URL", exc) from exc
            self._pending.append(
                FlushRecord(
                    kind="enqueued",
                    url=norm,
                    depth=depth,
                    parent_url=parent_url,
                    priority=stored_prio,
                    ts=_utcnow(),
                )
            )
            return True

    def dequeue(self) -> FrontierEntry | None:
        with self._lock:
            try:
                popped = self._client.zpopmax(self._ready, count=1)
            except Exception as exc:
                raise self._fail("dequeue frontier URL", exc) from exc
            if not popped:
                return None
            norm = str(popped[0][0])
            meta = self._read_meta(norm)
            if not meta:
                self._drop_stale_member(norm)
                return None
            stamp = _utcnow_precise()
            score = _score(
                int(meta.get("priority") or 0),
                int(meta.get("depth") or 0),
                int(meta.get("seq") or 0),
            )
            pipe = self._client.pipeline()
            pipe.zadd(self._active, {norm: score})
            pipe.hset(
                self._meta(norm),
                mapping={
                    "status": STATUS_ACTIVE,
                    "owner": "",
                    "leased_at": stamp,
                    "heartbeat_at": stamp,
                    "lease_ttl_s": self._lease_ttl_s,
                },
            )
            pipe.expire(
                self._meta(norm),
                max(1, self._lease_ttl_s * _META_TTL_MULTIPLIER),
            )
            try:
                pipe.execute()
            except Exception as exc:
                raise self._fail("dequeue frontier URL", exc) from exc
            return self._entry_from_meta(
                {
                    **meta,
                    "status": STATUS_ACTIVE,
                    "owner": "",
                    "leased_at": stamp,
                    "heartbeat_at": stamp,
                }
            )

    def dequeue_backoff(self) -> FrontierEntry | None:
        now = time.time()
        with self._lock:
            try:
                due = self._client.zrangebyscore(
                    self._backoff, "-inf", now, start=0, num=1
                )
            except Exception as exc:
                raise self._fail("dequeue backoff frontier URL", exc) from exc
            if not due:
                return None
            norm = str(due[0])
            try:
                removed = self._client.zrem(self._backoff, norm)
            except Exception as exc:
                raise self._fail("dequeue backoff frontier URL", exc) from exc
            if not removed:
                return None
            meta = self._read_meta(norm)
            if not meta:
                self._drop_stale_member(norm)
                return None
            stamp = _utcnow_precise()
            score = _score(
                int(meta.get("priority") or 0),
                int(meta.get("depth") or 0),
                int(meta.get("seq") or 0),
            )
            pipe = self._client.pipeline()
            pipe.zadd(self._active, {norm: score})
            pipe.hset(
                self._meta(norm),
                mapping={
                    "status": STATUS_ACTIVE,
                    "owner": "",
                    "leased_at": stamp,
                    "heartbeat_at": stamp,
                    "lease_ttl_s": self._lease_ttl_s,
                },
            )
            pipe.expire(
                self._meta(norm),
                max(1, self._lease_ttl_s * _META_TTL_MULTIPLIER),
            )
            try:
                pipe.execute()
            except Exception as exc:
                raise self._fail("dequeue backoff frontier URL", exc) from exc
            return self._entry_from_meta(
                {
                    **meta,
                    "status": STATUS_ACTIVE,
                    "owner": "",
                    "leased_at": stamp,
                    "heartbeat_at": stamp,
                }
            )

    def claim(
        self,
        worker_id: str,
        *,
        lease_ttl_s: int = DEFAULT_LEASE_TTL_S,
    ) -> FrontierEntry | None:
        if not worker_id or not worker_id.strip():
            raise ProviderError(
                "claim requires a non-empty worker_id",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.INVALID_REQUEST,
            )
        owner = worker_id.strip()
        ttl = max(1, int(lease_ttl_s or DEFAULT_LEASE_TTL_S))
        with self._lock:
            try:
                popped = self._client.zpopmax(self._ready, count=1)
            except Exception as exc:
                raise self._fail("claim frontier URL", exc) from exc
            if popped:
                norm = str(popped[0][0])
                meta = self._read_meta(norm)
                if not meta:
                    self._drop_stale_member(norm)
                    return None
                return self._activate(norm, meta, owner, ttl)
            found = self._expired_active_candidate(ttl)
            if found is None:
                return None
            norm, meta = found
            return self._activate(norm, meta, owner, ttl)

    def heartbeat(
        self,
        worker_id: str,
        *,
        lease_ttl_s: int = DEFAULT_LEASE_TTL_S,
    ) -> int:
        ttl = max(1, int(lease_ttl_s or DEFAULT_LEASE_TTL_S))
        stamp = _utcnow_precise()
        touched = 0
        with self._lock:
            members = self._scan_active("heartbeat frontier lease")
            for norm in members:
                meta = self._read_meta(norm)
                if not meta or str(meta.get("owner") or "") != worker_id:
                    continue
                pipe = self._client.pipeline()
                pipe.hset(
                    self._meta(norm),
                    mapping={"heartbeat_at": stamp, "lease_ttl_s": ttl},
                )
                pipe.expire(self._meta(norm), max(1, ttl * _META_TTL_MULTIPLIER))
                pipe.set(self._claim_key(norm), worker_id, px=max(1, ttl * 1000))
                try:
                    pipe.execute()
                except Exception as exc:
                    raise self._fail("heartbeat frontier lease", exc) from exc
                touched += 1
        return touched

    def reap_expired_leases(
        self,
        *,
        max_age_s: float = DEFAULT_LEASE_TTL_S,
        now: float | None = None,
    ) -> int:
        cutoff = _cutoff_iso(max(0, max_age_s), now)
        moved = 0
        with self._lock:
            members = self._scan_active("reap expired frontier leases")
            for norm in members:
                meta = self._read_meta(norm)
                if not meta:
                    self._drop_stale_member(norm)
                    continue
                heartbeat = str(meta.get("heartbeat_at") or "")
                if heartbeat and heartbeat >= cutoff:
                    continue
                self._move_to(norm, meta, STATUS_QUEUED, clear_owner=True)
                moved += 1
        return moved

    def requeue_owned(self, worker_id: str) -> int:
        moved = 0
        with self._lock:
            members = self._scan_active("requeue owned frontier URLs")
            for norm in members:
                meta = self._read_meta(norm)
                if not meta:
                    self._drop_stale_member(norm)
                    continue
                if str(meta.get("owner") or "") != worker_id:
                    continue
                self._move_to(norm, meta, STATUS_QUEUED, clear_owner=True)
                moved += 1
        return moved

    def owned(self, worker_id: str) -> list[FrontierEntry]:
        """Rows this worker holds active (bounded by the active set).

        The old implementation scanned every known URL; with a 250k-row
        backlog the 5s reporter beat turned into a full-queue scan per
        worker and saturated single-threaded Redis. The active set is
        the only place owned rows can be, so iterate that (near-empty
        when idle) and read just those metas.
        """
        out: list[FrontierEntry] = []
        with self._lock:
            members = self._scan_active("list owned frontier URLs")
            for norm in members:
                meta = self._read_meta(norm)
                if not meta:
                    self._drop_stale_member(norm)
                    continue
                if str(meta.get("status") or "") != STATUS_ACTIVE:
                    continue
                if str(meta.get("owner") or "") != worker_id:
                    continue
                out.append(self._entry_from_meta(meta))
        out.sort(key=lambda e: (e.depth, e.url))
        return out

    # -- terminal transitions -------------------------------------------
    def _finish(
        self,
        norm: str,
        meta: dict[str, Any],
        status: str,
        *,
        attempts: int | None = None,
        last_error: str | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> dict[str, Any]:
        """Shared terminal-transition writer (done/failed/skipped/backoff)."""
        if attempts is None:
            try:
                attempts = int(meta.get("attempts") or 0)
            except (TypeError, ValueError):
                attempts = 0
        fields: dict[str, Any] = {"status": status, "attempts": attempts}
        if last_error is not None:
            fields["last_error"] = last_error[:500]
        if etag is not None:
            fields["etag"] = etag
        if last_modified is not None:
            fields["last_modified"] = last_modified
        if status == STATUS_DONE:
            from datetime import datetime, timezone

            fields["fetched_at"] = datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        pipe = self._client.pipeline()
        pipe.zrem(self._ready, norm)
        pipe.zrem(self._active, norm)
        if status == STATUS_BACKOFF:
            pipe.zadd(self._backoff, {norm: time.time()})
        else:
            pipe.zrem(self._backoff, norm)
            pipe.delete(self._claim_key(norm))
        pipe.hset(self._meta(norm), mapping=fields)
        pipe.incr(self._stat(status))
        try:
            pipe.execute()
        except Exception as exc:
            raise self._fail(f"mark frontier URL {status}", exc) from exc
        meta.update({k: str(v) for k, v in fields.items()})
        return meta

    def mark_done(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> None:
        try:
            norm = normalize_frontier_url(url)
        except (AttributeError, ValueError):
            return
        with self._lock:
            meta = self._read_meta(norm)
            if not meta:
                return
            fields: dict[str, Any] = {}
            if etag is not None:
                fields["etag"] = etag
            if last_modified is not None:
                fields["last_modified"] = last_modified
            updated = self._finish(norm, meta, STATUS_DONE, **fields)
            self._pending.append(
                FlushRecord(
                    kind="done",
                    url=norm,
                    depth=int(updated.get("depth") or 0),
                    parent_url=updated.get("parent_url") or None,
                    priority=int(updated.get("priority") or 0),
                    attempts=int(updated.get("attempts") or 0),
                    etag=updated.get("etag") or None,
                    last_modified=updated.get("last_modified") or None,
                    ts=_utcnow(),
                )
            )

    def mark_failed(self, url: str, error: str) -> None:
        try:
            norm = normalize_frontier_url(url)
        except (AttributeError, ValueError):
            return
        with self._lock:
            meta = self._read_meta(norm)
            if not meta:
                return
            try:
                attempts = int(meta.get("attempts") or 0) + 1
            except (TypeError, ValueError):
                attempts = 1
            updated = self._finish(
                norm, meta, STATUS_FAILED, attempts=attempts, last_error=error or ""
            )
            self._pending.append(
                FlushRecord(
                    kind="failed",
                    url=norm,
                    depth=int(updated.get("depth") or 0),
                    parent_url=updated.get("parent_url") or None,
                    priority=int(updated.get("priority") or 0),
                    attempts=attempts,
                    last_error=str(updated.get("last_error") or ""),
                    owner=updated.get("owner") or None,
                    ts=_utcnow(),
                )
            )

    def mark_backoff(self, url: str, error: str) -> None:
        try:
            norm = normalize_frontier_url(url)
        except (AttributeError, ValueError):
            return
        with self._lock:
            meta = self._read_meta(norm)
            if not meta:
                return
            try:
                attempts = int(meta.get("attempts") or 0) + 1
            except (TypeError, ValueError):
                attempts = 1
            self._finish(
                norm, meta, STATUS_BACKOFF, attempts=attempts, last_error=error or ""
            )

    def mark_skipped(self, url: str) -> int:
        try:
            norm = normalize_frontier_url(url)
        except (AttributeError, ValueError):
            return 0
        with self._lock:
            meta = self._read_meta(norm)
            if not meta:
                return 0
            updated = self._finish(norm, meta, STATUS_SKIPPED)
            self._pending.append(
                FlushRecord(
                    kind="skipped",
                    url=norm,
                    depth=int(updated.get("depth") or 0),
                    priority=int(updated.get("priority") or 0),
                    attempts=int(updated.get("attempts") or 0),
                    ts=_utcnow(),
                )
            )
            return 1

    def skip_queued_domains(self, domains: Any) -> int:
        targets = normalize_skip_domains(domains)
        if not targets:
            return 0
        moved = 0
        with self._lock:
            try:
                members = list(self._client.zrange(self._ready, 0, -1))
            except Exception as exc:
                raise self._fail("skip queued domains", exc) from exc
            for raw in members:
                norm = str(raw)
                meta = self._read_meta(norm)
                if not meta:
                    self._drop_stale_member(norm)
                    continue
                domain = str(meta.get("domain") or _domain_of_url(norm) or "")
                matched: str | None = None
                for target in targets:
                    if host_matches_skip_domains(domain, [target]):
                        matched = target
                        break
                if matched is None:
                    continue
                self._finish(
                    norm,
                    meta,
                    STATUS_SKIPPED,
                    last_error=f"skipped: domain {matched}",
                )
                moved += 1
        return moved

    def requeue_active(self) -> int:
        moved = 0
        with self._lock:
            members = self._scan_active("requeue active frontier URLs")
            for norm in members:
                meta = self._read_meta(norm)
                if not meta:
                    self._drop_stale_member(norm)
                    continue
                self._move_to(norm, meta, STATUS_QUEUED)
                moved += 1
        return moved

    def requeue(self, url: str) -> bool:
        try:
            norm = normalize_frontier_url(url)
        except (AttributeError, ValueError):
            return False
        with self._lock:
            meta = self._read_meta(norm)
            if not meta:
                return False
            if str(meta.get("status") or "") not in (STATUS_ACTIVE, STATUS_FAILED):
                return False
            self._move_to(norm, meta, STATUS_QUEUED)
            return True

    def requeue_stale(self, max_age_days: int, limit: int) -> int:
        if (
            not isinstance(max_age_days, int)
            or isinstance(max_age_days, bool)
            or max_age_days < 0
            or not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit <= 0
        ):
            return 0
        from datetime import datetime, timedelta, timezone

        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        with self._lock:
            stale: list[tuple[str, str]] = []
            for norm, meta in self._iter_metas():
                if str(meta.get("status") or "") != STATUS_DONE:
                    continue
                fetched = str(meta.get("fetched_at") or "")
                if fetched and fetched >= cutoff:
                    continue
                stale.append((fetched, norm))
            stale.sort()
            moved = 0
            for _, norm in stale[:limit]:
                meta = self._read_meta(norm)
                if not meta:
                    continue
                self._move_to(
                    norm,
                    meta,
                    STATUS_QUEUED,
                    clear_owner=True,
                    attempts=0,
                    clear_error=True,
                )
                moved += 1
        return moved

    def clear_queued(self) -> int:
        with self._lock:
            try:
                members = [str(m) for m in self._client.zrange(self._ready, 0, -1)]
            except Exception as exc:
                raise self._fail("clear queued frontier URLs", exc) from exc
            if not members:
                return 0
            pipe = self._client.pipeline()
            for norm in members:
                pipe.delete(self._meta(norm))
                pipe.delete(self._claim_key(norm))
                pipe.srem(self._seen, norm)
            pipe.delete(self._ready)
            try:
                pipe.execute()
            except Exception as exc:
                raise self._fail("clear queued frontier URLs", exc) from exc
            return len(members)

    # -- reads ----------------------------------------------------------
    def is_known(self, url: str) -> bool:
        try:
            norm = normalize_frontier_url(url)
        except (AttributeError, ValueError):
            return False
        try:
            return bool(self._client.sismember(self._seen, norm))
        except Exception as exc:
            raise self._fail("look up frontier URL", exc) from exc

    def status_of(self, url: str) -> str | None:
        try:
            norm = normalize_frontier_url(url)
        except (AttributeError, ValueError):
            return None
        meta = self._read_meta(norm)
        if not meta:
            return None
        return str(meta.get("status") or STATUS_QUEUED)

    def statuses_of(self, urls: Iterable[str]) -> dict[str, str]:
        norms: list[str] = []
        for url in urls or []:
            try:
                norm = normalize_frontier_url(url)
            except (AttributeError, ValueError):
                continue
            if norm not in norms:
                norms.append(norm)
        out: dict[str, str] = {}
        if not norms:
            return out
        pipe = self._client.pipeline()
        for norm in norms:
            pipe.hget(self._meta(norm), "status")
        try:
            values = pipe.execute()
        except Exception as exc:
            raise self._fail("look up frontier URL statuses", exc) from exc
        for norm, status in zip(norms, values):
            if status is not None:
                out[norm] = str(status)
        return out

    def entry_of(self, url: str) -> FrontierEntry | None:
        try:
            norm = normalize_frontier_url(url)
        except (AttributeError, ValueError):
            return None
        meta = self._read_meta(norm)
        if not meta:
            return None
        return self._entry_from_meta(meta)

    def validators(self) -> dict[str, dict[str, str]]:
        """Stored ETag/Last-Modified validators keyed by URL (SQLite parity)."""
        out: dict[str, dict[str, str]] = {}
        with self._lock:
            for _, meta in self._iter_metas():
                entry: dict[str, str] = {}
                if meta.get("etag"):
                    entry["etag"] = str(meta["etag"])
                if meta.get("last_modified"):
                    entry["last_modified"] = str(meta["last_modified"])
                if entry:
                    out[str(meta.get("url", ""))] = entry
        return out

    def reseed(self, url: str) -> bool:
        try:
            norm = normalize_frontier_url(url)
        except (AttributeError, ValueError):
            return False
        with self._lock:
            meta = self._read_meta(norm)
            if not meta:
                return False
            if str(meta.get("status") or "") not in _TERMINAL_STATUSES:
                return False
            self._move_to(
                norm,
                meta,
                STATUS_QUEUED,
                clear_owner=True,
                attempts=0,
                clear_error=True,
            )
            return True

    def reseed_many(self, urls: Iterable[str]) -> int:
        norms: list[str] = []
        for url in urls or []:
            try:
                norm = normalize_frontier_url(url)
            except (AttributeError, ValueError):
                continue
            if norm not in norms:
                norms.append(norm)
        revived = 0
        with self._lock:
            for norm in norms:
                meta = self._read_meta(norm)
                if not meta:
                    continue
                if str(meta.get("status") or "") not in _TERMINAL_STATUSES:
                    continue
                self._move_to(
                    norm,
                    meta,
                    STATUS_QUEUED,
                    clear_owner=True,
                    attempts=0,
                    clear_error=True,
                )
                revived += 1
        return revived

    def pending(self, limit: int = 500, offset: int = 0) -> list[FrontierEntry]:
        capped = max(0, limit)
        skipped = max(0, offset)
        want = capped + skipped
        if want <= 0:
            return []
        with self._lock:
            try:
                # Fetch from the TOP of each set (highest scores): that is
                # the end workers drain via ZPOPMAX, and therefore what the
                # queue view must show. Index 0..want is the stagnant tail
                # on a deep backlog — the admin list froze while the depth
                # count moved.
                queued = (
                    [str(m) for m in self._client.zrange(self._ready, -want, -1)]
                    if want
                    else []
                )
                active = (
                    [str(m) for m in self._client.zrange(self._active, -want, -1)]
                    if want
                    else []
                )
            except Exception as exc:
                raise self._fail("list pending frontier URLs", exc) from exc
            pipe = self._client.pipeline()
            for norm in queued + active:
                pipe.hgetall(self._meta(norm))
            try:
                metas = pipe.execute()
            except Exception as exc:
                raise self._fail("list pending frontier URLs", exc) from exc
            # Merge the two ordered branches exactly (same convention as
            # _merge_pending): each branch holds its status's top `want`
            # rows, so sorting the merged rows by the full sort key and
            # slicing is exact.
            ranked: list[tuple[tuple[int, int, int], FrontierEntry]] = []
            for meta in metas:
                if not meta:
                    continue
                entry = self._entry_from_meta(dict(meta))
                try:
                    seq = int(meta.get("seq") or 0)
                except (TypeError, ValueError):
                    seq = 0
                ranked.append(((-entry.priority, entry.depth, seq), entry))
            ranked.sort(key=lambda item: item[0])
            return [entry for _, entry in ranked[skipped : skipped + capped]]

    def failed(self, limit: int = 100, offset: int = 0) -> list[FrontierEntry]:
        capped = max(0, limit)
        skipped = max(0, offset)
        with self._lock:
            rows = [
                (
                    str(meta.get("url", "")),
                    int(meta.get("attempts") or 0),
                    int(meta.get("seq") or 0),
                    meta,
                )
                for _, meta in self._iter_metas()
                if str(meta.get("status") or "") == STATUS_FAILED
            ]
        rows.sort(key=lambda r: (-r[1], -r[2]))
        return [
            self._entry_from_meta(m) for _, _, _, m in rows[skipped : skipped + capped]
        ]

    def _domain_rows(self, status: str) -> list[tuple[str, dict[str, Any]]]:
        return [
            (_host_bucket(str(meta.get("url", ""))), meta)
            for _, meta in self._iter_metas()
            if str(meta.get("status") or "") == status
        ]

    def failed_domains(
        self, limit: int = 100, offset: int = 0
    ) -> list[tuple[str, int, str]]:
        capped = max(0, int(limit or 0))
        skipped = max(0, int(offset or 0))
        with self._lock:
            rows = self._domain_rows(STATUS_FAILED)
        by_domain: dict[str, list[str]] = {}
        for domain, meta in rows:
            by_domain.setdefault(domain, []).append(str(meta.get("last_error") or ""))
        out: list[tuple[str, int, str]] = []
        for domain in sorted(by_domain, key=lambda d: (-len(by_domain[d]), d)):
            reasons = by_domain[domain]
            counts: dict[str, int] = {}
            for reason in reasons:
                counts[reason] = counts.get(reason, 0) + 1
            top = sorted(counts, key=lambda r: (-counts[r], r))[0]
            out.append((domain, len(reasons), top))
        return out[skipped : skipped + capped]

    def failed_domain_count(self) -> int:
        with self._lock:
            rows = self._domain_rows(STATUS_FAILED)
        return len({domain for domain, _ in rows})

    def _domain_entries(
        self,
        domain: str,
        status: str,
        limit: int,
        offset: int,
        *,
        order: str,
    ) -> tuple[list[FrontierEntry], int]:
        host = normalize_skip_domain(domain)
        if host is None:
            return [], 0
        capped = max(0, limit)
        skipped = max(0, offset)
        with self._lock:
            rows = [
                meta
                for _, meta in self._iter_metas()
                if str(meta.get("status") or "") == status
                and host_matches_skip_domains(
                    str(
                        meta.get("domain")
                        or _domain_of_url(str(meta.get("url", "")))
                        or ""
                    ),
                    [host],
                )
            ]
        if order == "failed":
            rows.sort(
                key=lambda m: (-int(m.get("attempts") or 0), -int(m.get("seq") or 0))
            )
        else:
            rows.sort(
                key=lambda m: (-int(m.get("priority") or 0), -int(m.get("seq") or 0))
            )
        total = len(rows)
        return (
            [self._entry_from_meta(m) for m in rows[skipped : skipped + capped]],
            total,
        )

    def failed_domain_urls(
        self, domain: str, limit: int = 100, offset: int = 0
    ) -> list[FrontierEntry]:
        entries, _ = self._domain_entries(
            domain, STATUS_FAILED, limit, offset, order="failed"
        )
        return entries

    def failed_domain_url_count(self, domain: str) -> int:
        _, total = self._domain_entries(domain, STATUS_FAILED, 0, 0, order="failed")
        return total

    def queued_domain_urls(
        self, domain: str, limit: int = 100, offset: int = 0
    ) -> list[FrontierEntry]:
        entries, _ = self._domain_entries(
            domain, STATUS_QUEUED, limit, offset, order="queued"
        )
        return entries

    def queued_domain_url_count(self, domain: str) -> int:
        _, total = self._domain_entries(domain, STATUS_QUEUED, 0, 0, order="queued")
        return total

    def requeue_failed_domains(self, domains: Any) -> int:
        targets = normalize_skip_domains(domains)
        if not targets:
            return 0
        moved = 0
        with self._lock:
            for _, meta in self._iter_metas():
                if str(meta.get("status") or "") != STATUS_FAILED:
                    continue
                domain = str(
                    meta.get("domain") or _domain_of_url(str(meta.get("url", ""))) or ""
                )
                if not any(
                    host_matches_skip_domains(domain, [target]) for target in targets
                ):
                    continue
                self._move_to(
                    str(meta.get("url", "")),
                    meta,
                    STATUS_QUEUED,
                    attempts=0,
                    clear_error=True,
                )
                moved += 1
        return moved

    def purge_failed_domains(self, domains: Any) -> int:
        targets = normalize_skip_domains(domains)
        if not targets:
            return 0
        removed = 0
        with self._lock:
            victims: list[str] = []
            for norm, meta in self._iter_metas():
                if str(meta.get("status") or "") != STATUS_FAILED:
                    continue
                domain = str(
                    meta.get("domain") or _domain_of_url(str(meta.get("url", ""))) or ""
                )
                if any(
                    host_matches_skip_domains(domain, [target]) for target in targets
                ):
                    victims.append(norm)
            for norm in victims:
                pipe = self._client.pipeline()
                pipe.delete(self._meta(norm))
                pipe.delete(self._claim_key(norm))
                pipe.srem(self._seen, norm)
                try:
                    pipe.execute()
                except Exception as exc:
                    raise self._fail("purge failed domains", exc) from exc
                removed += 1
        return removed

    def queued_domains(
        self, limit: int = 100, offset: int = 0
    ) -> list[tuple[str, int, int]]:
        capped = max(0, int(limit or 0))
        skipped = max(0, int(offset or 0))
        with self._lock:
            rows = self._domain_rows(STATUS_QUEUED)
        by_domain: dict[str, list[int]] = {}
        for domain, meta in rows:
            try:
                prio = int(meta.get("priority") or 0)
            except (TypeError, ValueError):
                prio = 0
            by_domain.setdefault(domain, []).append(prio)
        out = sorted(
            ((d, len(v), max(v)) for d, v in by_domain.items()),
            key=lambda r: (-r[2], -r[1], r[0]),
        )
        return out[skipped : skipped + capped]

    def queued_domain_count(self) -> int:
        with self._lock:
            rows = self._domain_rows(STATUS_QUEUED)
        return len({domain for domain, _ in rows})

    def bump_queued_priority(self, domains: Any, delta: int = 1) -> int:
        targets = normalize_skip_domains(domains)
        try:
            step = int(delta or 0)
        except (TypeError, ValueError):
            return 0
        if not targets or step == 0:
            return 0
        moved = 0
        with self._lock:
            cands = [
                (norm, meta)
                for norm, meta in self._iter_metas()
                if str(meta.get("status") or "") == STATUS_QUEUED
                and any(
                    host_matches_skip_domains(
                        str(
                            meta.get("domain")
                            or _domain_of_url(str(meta.get("url", "")))
                            or ""
                        ),
                        [target],
                    )
                    for target in targets
                )
            ]
            for norm, meta in cands:
                try:
                    prio = int(meta.get("priority") or 0) + step
                except (TypeError, ValueError):
                    prio = step
                try:
                    self._client.hset(self._meta(norm), mapping={"priority": prio})
                    self._client.zadd(
                        self._ready,
                        {
                            norm: _score(
                                prio,
                                int(meta.get("depth") or 0),
                                int(meta.get("seq") or 0),
                            )
                        },
                    )
                except Exception as exc:
                    raise self._fail("bump queued priority", exc) from exc
                moved += 1
        return moved

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {status: 0 for status in _STATUSES}
        out["total"] = 0
        with self._lock:
            for _, meta in self._iter_metas():
                status = str(meta.get("status") or "")
                if status not in out:
                    out[status] = 0
                out[status] += 1
                out["total"] += 1
        return out

    def queue_depth(self) -> int:
        """URLs still to crawl: two O(1) cardinality lookups, no scan.

        The crawl loop calls this per URL; the inherited
        counts-based default would re-scan every meta key each time
        (O(N^2) over a backlog).
        """
        with self._lock:
            try:
                ready = self._client.zcard(self._ready)
                active = self._client.zcard(self._active)
            except Exception as exc:
                raise self._fail("count frontier URLs", exc) from exc
            return int(ready) + int(active)

    def evict_oldest(
        self,
        limit: int,
        *,
        on_evict: Callable[[str], None] | None = None,
    ) -> int:
        """Shed queued rows past ``limit`` (lowest-score-first eviction).

        Same outcome contract as the SQL backends (exactly the overflow
        leaves the queue, victims land ``skipped`` with
        ``evicted: queue cap exceeded`` so rediscovery stays shed), but
        the victim order differs by necessity: the SQL backends evict
        globally-oldest by ``discovered_at``, while here victims are the
        lowest-score members — lowest priority, then deepest, then
        newest — i.e. the rows the crawler (``ZPOPMAX``) would pop last.
        Reading every meta HASH to recover discovery order would turn
        the per-push eviction check into a full-queue scan; the score
        order needs one ``ZRANGE`` plus per-victim cleanup only.
        ``on_evict`` reports each victim URL (analytics parity); a
        raising listener is swallowed so the shed still lands.
        """
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise self._fail(
                "evict queued frontier URLs",
                ValueError(f"limit must be an integer, got {type(limit).__name__}"),
            )
        cap = max(0, limit)
        with self._lock:
            try:
                total = int(self._client.zcard(self._ready) or 0)
            except Exception as exc:
                raise self._fail("evict queued frontier URLs", exc) from exc
            overflow = total - cap
            if overflow <= 0:
                return 0
            try:
                members = self._client.zrange(self._ready, 0, overflow - 1)
                victims = [str(m) for m in members]
            except Exception as exc:
                raise self._fail("evict queued frontier URLs", exc) from exc
            evicted = 0
            for norm in victims:
                meta = self._read_meta(norm)
                if not meta:
                    self._drop_stale_member(norm)
                    continue
                self._finish(
                    norm,
                    meta,
                    STATUS_SKIPPED,
                    last_error="evicted: queue cap exceeded",
                )
                if on_evict is not None:
                    with contextlib.suppress(Exception):
                        on_evict(norm)
                evicted += 1
            return evicted

    # -- crawl requests -------------------------------------------------
    def submit_crawl_request(self, url: str) -> dict[str, Any]:
        if not isinstance(url, str) or not url.strip():
            raise ProviderError(
                "crawl request URL must not be blank",
                provider_name=_PROVIDER_NAME,
                code=ProviderError.INVALID_REQUEST,
            )
        norm = normalize_frontier_url(url)
        with self._lock:
            try:
                existing_id = self._client.get(self._crawlreq_byurl(norm))
            except Exception as exc:
                raise self._fail("submit crawl request", exc) from exc
            if existing_id is not None:
                record = self._crawlreq_record(int(existing_id))
                if record is not None and record["status"] == "pending":
                    return record
            try:
                request_id = int(self._client.incr(self._crawlreq_seq) or 1)
            except Exception as exc:
                raise self._fail("submit crawl request", exc) from exc
            stamp = _utcnow()
            pipe = self._client.pipeline()
            pipe.hset(
                self._crawlreq_key(request_id),
                mapping={
                    "id": request_id,
                    "url": norm,
                    "status": "pending",
                    "created_at": stamp,
                    "decided_at": "",
                },
            )
            pipe.zadd(self._crawlreq_ids, {str(request_id): request_id})
            pipe.set(self._crawlreq_byurl(norm), request_id)
            try:
                pipe.execute()
            except Exception as exc:
                raise self._fail("submit crawl request", exc) from exc
            record = self._crawlreq_record(request_id)
            if record is None:
                raise self._fail("submit crawl request", RuntimeError("missing row"))
            return record

    def _crawlreq_record(self, request_id: int) -> dict[str, Any] | None:
        try:
            row = self._client.hgetall(self._crawlreq_key(request_id))
        except Exception as exc:
            raise self._fail("read crawl request", exc) from exc
        if not row:
            return None
        try:
            rid: int | None = int(row.get("id"))
        except (TypeError, ValueError):
            rid = None
        return {
            "id": rid,
            "url": str(row.get("url") or ""),
            "status": str(row.get("status") or "pending"),
            "created_at": str(row.get("created_at") or "") or None,
            "decided_at": str(row.get("decided_at") or "") or None,
        }

    def list_crawl_requests(
        self, status: str | None = None, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        capped = max(0, limit)
        skipped = max(0, offset)
        with self._lock:
            try:
                ids = [
                    int(i) for i in self._client.zrevrange(self._crawlreq_ids, 0, -1)
                ]
            except Exception as exc:
                raise self._fail("list crawl requests", exc) from exc
            out: list[dict[str, Any]] = []
            for request_id in ids:
                record = self._crawlreq_record(request_id)
                if record is None:
                    continue
                if status is not None and record["status"] != status:
                    continue
                out.append(record)
        return out[skipped : skipped + capped]

    def set_crawl_request_status(self, request_id: int, status: str) -> bool:
        if status not in VALID_CRAWL_REQUEST_DECISIONS:
            return False
        try:
            request_id = int(request_id)
        except (TypeError, ValueError):
            return False
        with self._lock:
            record = self._crawlreq_record(request_id)
            if record is None or record["status"] != "pending":
                return False
            stamp = _utcnow()
            pipe = self._client.pipeline()
            pipe.hset(
                self._crawlreq_key(request_id),
                mapping={"status": status, "decided_at": stamp},
            )
            try:
                current = self._client.get(
                    self._crawlreq_byurl(str(record["url"] or ""))
                )
            except Exception as exc:
                raise self._fail("decide crawl request", exc) from exc
            if current is not None and int(current) == request_id:
                pipe.delete(self._crawlreq_byurl(str(record["url"] or "")))
            try:
                pipe.execute()
            except Exception as exc:
                raise self._fail("decide crawl request", exc) from exc
            return True

    # -- checkpoints / settings -----------------------------------------
    def save_checkpoint(self, checkpoint: CrawlCheckpoint) -> None:
        from openlens.storage.frontier import _VALID_PHASES

        phase = checkpoint.phase if checkpoint.phase in _VALID_PHASES else PHASE_RUNNING
        fields = {
            "crawl_id": checkpoint.crawl_id,
            "seeds": json.dumps(list(checkpoint.seeds)),
            "phase": phase,
            "pages_attempted": checkpoint.pages_attempted,
            "pages_indexed": checkpoint.pages_indexed,
            "frontier_size": checkpoint.frontier_size,
            "stats": json.dumps(dict(checkpoint.stats)),
            "updated_at": checkpoint.updated_at or _utcnow(),
        }
        with self._lock:
            try:
                self._client.hset(
                    self._checkpoint_key(checkpoint.crawl_id), mapping=fields
                )
            except Exception as exc:
                raise self._fail("save crawl checkpoint", exc) from exc
            self._pending.append(
                FlushRecord(kind="checkpoint", url=checkpoint.crawl_id, ts=_utcnow())
            )

    def load_checkpoint(
        self, crawl_id: str = DEFAULT_CRAWL_ID
    ) -> CrawlCheckpoint | None:
        try:
            row = self._client.hgetall(self._checkpoint_key(crawl_id))
        except Exception as exc:
            raise self._fail("load crawl checkpoint", exc) from exc
        if not row:
            return None
        return _checkpoint_from_parts(
            row.get("crawl_id", crawl_id),
            row.get("seeds", "[]"),
            row.get("phase", PHASE_RUNNING),
            row.get("pages_attempted", 0),
            row.get("pages_indexed", 0),
            row.get("frontier_size", 0),
            row.get("stats", "{}"),
            row.get("updated_at", ""),
        )

    def increment_counters(
        self,
        crawl_id: str = DEFAULT_CRAWL_ID,
        *,
        pages_attempted: int = 0,
        pages_indexed: int = 0,
    ) -> CrawlCheckpoint:
        attempted = max(0, int(pages_attempted or 0))
        indexed = max(0, int(pages_indexed or 0))
        key = self._checkpoint_key(crawl_id)
        with self._lock:
            pipe = self._client.pipeline()
            pipe.hsetnx(key, "crawl_id", crawl_id)
            pipe.hsetnx(key, "seeds", "[]")
            pipe.hsetnx(key, "phase", PHASE_RUNNING)
            pipe.hsetnx(key, "pages_attempted", 0)
            pipe.hsetnx(key, "pages_indexed", 0)
            pipe.hsetnx(key, "frontier_size", 0)
            pipe.hsetnx(key, "stats", "{}")
            pipe.hsetnx(key, "updated_at", _utcnow())
            if attempted:
                pipe.hincrby(key, "pages_attempted", attempted)
            if indexed:
                pipe.hincrby(key, "pages_indexed", indexed)
            try:
                pipe.execute()
            except Exception as exc:
                raise self._fail("increment crawl counters", exc) from exc
        checkpoint = self.load_checkpoint(crawl_id)
        if checkpoint is None:
            raise self._fail("increment crawl counters", RuntimeError("missing row"))
        return checkpoint

    def get_settings(self, settings_id: str = DEFAULT_SETTINGS_ID) -> dict[str, Any]:
        try:
            raw = self._client.get(self._settings_key(settings_id))
        except Exception as exc:
            raise self._fail("read frontier settings", exc) from exc
        if raw is None:
            return {}
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        return dict(payload) if isinstance(payload, dict) else {}

    def save_settings(
        self, payload: Mapping[str, Any], settings_id: str = DEFAULT_SETTINGS_ID
    ) -> None:
        with self._lock:
            try:
                self._client.set(
                    self._settings_key(settings_id), json.dumps(dict(payload))
                )
            except Exception as exc:
                raise self._fail("save frontier settings", exc) from exc
            self._pending.append(
                FlushRecord(kind="settings", url=settings_id, ts=_utcnow())
            )

    # -- coordination registry / politeness ------------------------------
    def register_worker(
        self,
        worker_id: str,
        crawl_id: str = DEFAULT_CRAWL_ID,
    ) -> None:
        self.heartbeat_worker(worker_id, crawl_id, claimed=0)

    def heartbeat_worker(
        self,
        worker_id: str,
        crawl_id: str = DEFAULT_CRAWL_ID,
        *,
        claimed: int = 0,
    ) -> None:
        if not worker_id or not worker_id.strip():
            return
        stamp = _utcnow_precise()
        try:
            self._client.hset(
                self._workers,
                mapping={
                    worker_id.strip(): json.dumps(
                        {
                            "worker_id": worker_id.strip(),
                            "crawl_id": crawl_id,
                            "claimed": max(0, int(claimed or 0)),
                            "last_heartbeat": stamp,
                            "updated_at": stamp,
                        }
                    )
                },
            )
        except Exception as exc:
            raise self._fail("heartbeat worker", exc) from exc

    def list_workers(self, crawl_id: str | None = None) -> list[dict[str, Any]]:
        try:
            rows = self._client.hgetall(self._workers)
        except Exception as exc:
            raise self._fail("list workers", exc) from exc
        out: list[dict[str, Any]] = []
        for raw in (rows or {}).values():
            try:
                record = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if not isinstance(record, dict):
                continue
            if crawl_id is not None and record.get("crawl_id") != crawl_id:
                continue
            out.append(
                {
                    "worker_id": str(record.get("worker_id", "")),
                    "crawl_id": str(record.get("crawl_id", "")),
                    "claimed": int(record.get("claimed") or 0),
                    "last_heartbeat": str(record.get("last_heartbeat") or ""),
                    "updated_at": str(record.get("updated_at") or ""),
                }
            )
        out.sort(key=lambda r: r["last_heartbeat"], reverse=True)
        return out

    def prune_workers(
        self,
        *,
        max_age_s: float = DEFAULT_LEASE_TTL_S * 2,
        now: float | None = None,
    ) -> int:
        cutoff = _cutoff_iso(max(0, max_age_s), now)
        with self._lock:
            try:
                rows = self._client.hgetall(self._workers) or {}
            except Exception as exc:
                raise self._fail("prune workers", exc) from exc
            stale = [
                worker_id
                for worker_id, raw in rows.items()
                if _worker_last_heartbeat(raw) < cutoff
            ]
            if not stale:
                return 0
            try:
                removed = self._client.hdel(self._workers, *stale)
            except Exception as exc:
                raise self._fail("prune workers", exc) from exc
            return int(removed or 0)

    def reserve_politeness(
        self,
        host: str,
        delay_s: float,
        *,
        now: float | None = None,
    ) -> float:
        key = (host or "").strip().lower()
        if not key:
            return 0.0
        delay = max(0.0, float(delay_s or 0.0))
        moment = now if now is not None else time.time()
        with self._lock:
            try:
                row = self._client.hgetall(self._polite_key(key)) or {}
            except Exception as exc:
                raise self._fail("reserve politeness slot", exc) from exc
            try:
                current = float(row.get("next_fetch_at") or 0.0)
            except (TypeError, ValueError):
                current = 0.0
            wait = max(0.0, current - moment)
            try:
                self._client.hset(
                    self._polite_key(key),
                    mapping={
                        "next_fetch_at": max(moment, current) + delay,
                        "delay_s": delay,
                        "updated_at": _utcnow(),
                    },
                )
            except Exception as exc:
                raise self._fail("reserve politeness slot", exc) from exc
            return wait

    def get_politeness_delay(self, host: str) -> float:
        key = (host or "").strip().lower()
        if not key:
            return 0.0
        try:
            delay = self._client.hget(self._polite_key(key), "delay_s")
        except Exception as exc:
            raise self._fail("read politeness delay", exc) from exc
        try:
            return float(delay or 0.0)
        except (TypeError, ValueError):
            return 0.0

    # -- PASS-2 flusher seam ---------------------------------------------
    def drain_pending_flush(self, batch_size: int = 1000) -> list[FlushRecord]:
        """Pop up to ``batch_size`` buffered :class:`FlushRecord` items."""
        try:
            count = max(0, int(batch_size))
        except (TypeError, ValueError):
            return []
        out: list[FlushRecord] = []
        with self._lock:
            while self._pending and len(out) < count:
                out.append(self._pending.popleft())
        return out

    def requeue_flush(self, records: Any) -> None:
        """Return unflushed records to the buffer head (flusher retry path).

        The mirror flusher calls this with the unapplied tail after a
        mid-batch mirror error so the next pass retries them in order
        (at-least-once); never raises.
        """
        if not records:
            return
        with self._lock, contextlib.suppress(Exception):
            for record in reversed(list(records)):
                self._pending.appendleft(record)

    def backfill_done(self) -> bool:
        """Whether a mirror→Redis cutover backfill already completed."""
        with self._lock, contextlib.suppress(Exception):
            return str(self._client.get(self._k("backfill", "done"))) == "1"
        return False

    def try_claim_backfill(self, ttl_s: float = 600.0) -> bool:
        """Claim the one-time cutover backfill (first claimant wins).

        ``SET NX`` with a TTL so a crashed claimant's lock expires and
        a later boot retries; the replay itself is idempotent.
        """
        try:
            ttl = max(1, int(ttl_s or 0))
        except (TypeError, ValueError):
            ttl = 600
        with self._lock, contextlib.suppress(Exception):
            return bool(
                self._client.set(self._k("backfill", "lock"), "1", nx=True, ex=ttl)
            )
        return False

    def mark_backfill_done(self, ttl_s: float = 30.0 * 86400.0) -> None:
        """Record backfill completion and release the claim lock."""
        try:
            ttl = max(1, int(ttl_s or 0))
        except (TypeError, ValueError):
            ttl = 30 * 86400
        with self._lock, contextlib.suppress(Exception):
            self._client.set(self._k("backfill", "done"), "1", ex=ttl)
            self._client.delete(self._k("backfill", "lock"))

    def snapshot_stats(self) -> dict[str, int]:
        """Current ``{prefix}:stats:*`` event counters (flusher input)."""
        pipe = self._client.pipeline()
        for event in _STAT_EVENTS:
            pipe.get(self._stat(event))
        try:
            values = pipe.execute()
        except Exception as exc:
            raise self._fail("read frontier stats", exc) from exc
        out: dict[str, int] = {}
        for event, value in zip(_STAT_EVENTS, values):
            try:
                out[event] = int(value or 0)
            except (TypeError, ValueError):
                out[event] = 0
        return out

    def close(self, flush: bool = True) -> None:
        """Release the client; ``flush=True`` drains the flusher buffer first."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        if flush:
            self.drain_pending_flush(1 << 30)
        with contextlib.suppress(Exception):
            self._client.close()


def _worker_last_heartbeat(raw: Any) -> str:
    try:
        record = json.loads(raw)
    except (ValueError, TypeError):
        return ""
    if not isinstance(record, dict):
        return ""
    return str(record.get("last_heartbeat") or "")

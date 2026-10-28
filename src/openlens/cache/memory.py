"""In-process bounded-LRU cache backend (default, zero dependencies)."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict

from openlens.cache.base import CacheProvider


class MemoryCacheProvider(CacheProvider):
    """TTL map with a hard entry bound; oldest entries evict first.

    Thread-safe for the single-process server (one lock around every
    mutation); expiry is lazy on read with opportunistic sweeps on
    write so dead entries cannot pin the bound.
    """

    def __init__(self, max_entries: int = 1000) -> None:
        if not isinstance(max_entries, int) or max_entries <= 0:
            raise ValueError(
                "MemoryCacheProvider 'max_entries' must be a positive int."
            )
        self._max_entries = max_entries
        self._entries: OrderedDict[str, tuple[str, float]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> str | None:
        now = time.time()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            value, expires_at = entry
            if expires_at <= now:
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return value

    def set(self, key: str, value: str, ttl_s: int) -> None:
        if ttl_s <= 0:
            return
        expires_at = time.time() + ttl_s
        with self._lock:
            self._entries[key] = (value, expires_at)
            self._entries.move_to_end(key)
            self._sweep_locked()
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._entries.pop(key, None)

    def clear(self) -> int:
        with self._lock:
            removed = len(self._entries)
            self._entries.clear()
            return removed

    def health_check(self) -> bool:
        return True

    def close(self) -> None:
        with self._lock:
            self._entries.clear()

    def _sweep_locked(self) -> None:
        now = time.time()
        expired = [
            key for key, (_, expires_at) in self._entries.items() if expires_at <= now
        ]
        for key in expired:
            del self._entries[key]

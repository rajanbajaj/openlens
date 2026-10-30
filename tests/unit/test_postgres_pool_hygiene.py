"""Pool hygiene: Postgres connections must not return to the pool
"idle in transaction".

psycopg leaves successful SELECTs inside an open transaction. The
lingering AccessShare locks block boot-time DDL (AccessExclusive),
which in turn queues every later query behind it — the backend goes
idle-CPU wedged and all HTTP times out. Both Postgres pools must roll
back on the success path before recycling.
"""

from __future__ import annotations

import queue
import threading

from openlens.storage import frontier as frontier_mod
from openlens.storage import postgres_backend as pg_mod


class _FakeConn:
    def __init__(self) -> None:
        self.rollbacks = 0
        self.commits = 0
        self.closes = 0

    def rollback(self) -> None:
        self.rollbacks += 1

    def commit(self) -> None:
        self.commits += 1

    def close(self) -> None:
        self.closes += 1

    def cursor(self):  # pragma: no cover - manual harness below
        raise AssertionError("cursor() unused in hygiene tests")


def _documents_store(pool: queue.Queue[object]) -> pg_mod.PostgresDocumentStore:
    store = object.__new__(pg_mod.PostgresDocumentStore)
    store._pool = pool  # type: ignore[attr-defined]
    store._state_lock = threading.Lock()  # type: ignore[attr-defined]
    store._closed = False  # type: ignore[attr-defined]
    store._checkout_timeout = 5.0  # type: ignore[attr-defined]
    return store


def _frontier_store(pool: queue.Queue[object]) -> frontier_mod.PostgresFrontierStore:
    store = object.__new__(frontier_mod.PostgresFrontierStore)
    store._pool = pool  # type: ignore[attr-defined]
    store._state_lock = threading.Lock()  # type: ignore[attr-defined]
    store._closed = False  # type: ignore[attr-defined]
    store._checkout_timeout = 5.0  # type: ignore[attr-defined]
    return store


def test_documents_pool_rolls_back_on_clean_release() -> None:
    conn = _FakeConn()
    pool: queue.Queue[object] = queue.Queue()
    pool.put(conn)
    store = _documents_store(pool)
    with store._connection():
        pass
    assert conn.rollbacks == 1
    assert conn.closes == 0
    assert pool.qsize() == 1


def test_documents_pool_closed_store_still_rolls_back() -> None:
    conn = _FakeConn()
    pool: queue.Queue[object] = queue.Queue()
    store = _documents_store(pool)
    store._closed = True  # type: ignore[attr-defined]
    store._release(conn)
    assert conn.rollbacks == 1
    assert conn.closes == 1
    assert pool.qsize() == 0


def test_frontier_pool_rolls_back_on_clean_release() -> None:
    conn = _FakeConn()
    pool: queue.Queue[object] = queue.Queue()
    pool.put(conn)
    store = _frontier_store(pool)
    with store._connection():
        pass
    assert conn.rollbacks == 1
    assert pool.qsize() == 1

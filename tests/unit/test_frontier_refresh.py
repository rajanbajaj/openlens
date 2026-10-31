"""Stale-URL refresh: done rows older than the cutoff re-enter the queue.

Offline: SQLite ``:memory:`` store; old rows are backdated through the
store connection to simulate yesterday's crawl.
"""

from __future__ import annotations

from openlens.storage.frontier import SqliteFrontierStore


def _store_with_done(url: str, fetched_at: str) -> SqliteFrontierStore:
    store = SqliteFrontierStore(":memory:")
    assert store.enqueue(url, depth=0)
    store.mark_done(url)
    store._conn.execute(  # noqa: SLF001 — white-box backdate for age tests
        "UPDATE frontier_queue SET fetched_at = ? WHERE url = ?",
        (fetched_at, url),
    )
    return store


def test_requeue_stale_moves_old_done_to_queued() -> None:
    store = _store_with_done("https://news.example/a", "2000-01-01T00:00:00Z")
    moved = store.requeue_stale(30, 100)
    assert moved == 1
    assert store.status_of("https://news.example/a") == "queued"


def test_requeue_stale_keeps_fresh_done() -> None:
    store = _store_with_done("https://news.example/a", "2999-01-01T00:00:00Z")
    assert store.requeue_stale(30, 100) == 0
    assert store.status_of("https://news.example/a") == "done"


def test_requeue_stale_ignores_non_done_rows() -> None:
    store = SqliteFrontierStore(":memory:")
    assert store.enqueue("https://news.example/q", depth=0)
    assert store.enqueue("https://news.example/f", depth=0)
    store.mark_failed("https://news.example/f", "http 500")
    assert store.requeue_stale(30, 100) == 0


def test_requeue_stale_respects_limit() -> None:
    store = SqliteFrontierStore(":memory:")
    for i in range(3):
        url = f"https://news.example/old-{i}"
        assert store.enqueue(url, depth=0)
        store.mark_done(url)
        store._conn.execute(  # noqa: SLF001
            "UPDATE frontier_queue SET fetched_at = '2000-01-01T00:00:00Z'"
            " WHERE url = ?",
            (url,),
        )
    assert store.requeue_stale(30, 2) == 2
    assert store.requeue_stale(30, 10) == 1

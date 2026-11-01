"""Kafka worker drains batch discovered links in one broker flush.

Regression coverage for the throughput batch: ``run_kafka_worker``
buffers ``on_link`` callbacks during a drain and publishes them with a
single ``publish_many`` call (one broker flush) instead of one blocking
publish per link, and it prints a startup banner so a worker in a long
initial drain is visible instead of silent.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import openlens.indexing.indexer as indexer_mod
from openlens.indexing.worker import API_URL_ENV, run_kafka_worker


class _ScriptedQueue:
    """Offline queue: scripted messages, then idle; records publishes."""

    def __init__(self, messages: list[Any]) -> None:
        self._messages = list(messages)
        self.published_batches: list[list[tuple[str, int, int | None]]] = []
        self.published_single: list[tuple[str, int, int | None]] = []
        self.commits = 0
        self.max_pages_per_poll = 10

    def check_available(self, timeout_s: float = 5.0) -> bool:
        return True

    def publish(self, url: str, depth: int = 0, priority: int | None = None) -> bool:
        self.published_single.append((url, depth, priority))
        return True

    def publish_many(self, items: list[tuple[str, int, int | None]]) -> int:
        self.published_batches.append(list(items))
        return len(items)

    def poll(self) -> Any | None:
        if not self._messages:
            return None
        return self._messages.pop(0)

    def commit(self) -> None:
        self.commits += 1

    def close(self) -> None:
        return None


def _config() -> dict[str, Any]:
    return {
        "storage": {"backend": "sqlite", "sqlite": {"db_path": ":memory:"}},
        "providers": {"custom_index": {"enabled": True, "db_path": ":memory:"}},
        "indexing": {},
        "kafka": {"enabled": True, "bootstrap_servers": ["b:1"]},
    }


def _run_index_with_links(*args: Any, **kwargs: Any) -> SimpleNamespace:
    on_link = kwargs["on_link"]
    on_link("https://example.com/a", 1, None)
    on_link("https://example.com/b", 2, 7)
    return SimpleNamespace(added=1, updated=0)


def test_worker_batches_links_one_flush_per_drain(
    monkeypatch: Any, capsys: Any
) -> None:
    monkeypatch.setenv(API_URL_ENV, "http://127.0.0.1:9")
    monkeypatch.setattr(indexer_mod, "run_index", _run_index_with_links)
    queue = _ScriptedQueue([SimpleNamespace(url="https://example.com/seed", depth=0)])
    counts = run_kafka_worker(
        [], _config(), crawl_id="test-batch", queue=queue, max_iterations=1
    )
    assert counts["added"] == 2  # initial drain + one polled message
    # Two drains, each flushed as one batch; no per-link publishes.
    assert len(queue.published_batches) == 2
    for batch in queue.published_batches:
        assert batch == [
            ("https://example.com/a", 1, None),
            ("https://example.com/b", 2, 7),
        ]
    assert queue.published_single == []
    assert queue.commits == 1
    # Startup banner prints before any drain, so a worker in a long
    # initial drain is visible in logs instead of silent.
    out = capsys.readouterr().out
    assert "queue=kafka" in out
    assert "crawl=test-batch" in out

"""Tests for the Kafka-backed URL queue (distributed crawling).

Offline only: a hermetic fake producer/consumer stands in for the
broker, ``:memory:``/tmp-file SQLite stores back the persistent state,
and page fetches are stubbed (no network, no loopback bind).

Covers ``validate_kafka_section``/``kafka_enabled``, message keying
(normalized URL), publish failure surfacing, the poll/commit cycle, the
``run_kafka_worker`` consume loop (drain, discovered-link fan-out,
pause, broker-down), the API resume bridge (Kafka-first, store
fallback), the ``PersistentFrontier`` push hook, and the package-init
regression for the ``python -m openlens.indexing.worker`` warning.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from openlens.core.interfaces import ConfigError, ProviderError
from openlens.indexing import crawler as crawler_mod
from openlens.indexing.crawler import PoliteCrawler, _FetchedPage
from openlens.indexing.kafka_queue import (
    KafkaMessage,
    KafkaQueue,
    kafka_enabled,
    open_kafka_queue,
    purge_topic,
    validate_kafka_section,
)
from openlens.indexing.worker import run_kafka_worker
from openlens.main import create_app
from openlens.storage.frontier import SqliteFrontierStore

from openlens.api import routes as routes_mod
from tests.unit.test_api import FakeEngine


# -- fakes ---------------------------------------------------------------


class _FakeFuture:
    def __init__(self, fail: bool = False) -> None:
        self._fail = fail

    def get(self, timeout: float | None = None) -> dict[str, int]:
        if self._fail:
            raise RuntimeError("broker down")
        return {"offset": 0}


class _FakeProducer:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[str, str | None, dict[str, Any]]] = []
        self._fail = fail

    def send(
        self, topic: str, key: str | None = None, value: Any = None
    ) -> _FakeFuture:
        self.sent.append((topic, key, value))
        return _FakeFuture(fail=self._fail)

    def flush(self, timeout: float | None = None) -> None:
        return None

    def close(self, timeout: float | None = None) -> None:
        return None


class _FakeRecord:
    def __init__(self, value: Any) -> None:
        self.value = value


class _FakeConsumer:
    def __init__(self, batches: list[list[Any]]) -> None:
        self._batches = [list(batch) for batch in batches]
        self.commits = 0
        self.closed = False

    def poll(self, timeout_ms: int = 0) -> dict[str, list[_FakeRecord]]:
        if not self._batches:
            return {}
        return {"p-0": [_FakeRecord(value) for value in self._batches.pop(0)]}

    def commit(self) -> None:
        self.commits += 1

    def bootstrap_connected(self) -> bool:
        return True

    def close(self, autocommit: bool = False) -> None:
        self.closed = True


class _FakeQueue:
    """Offline KafkaQueue stand-in (same small surface as the real one)."""

    def __init__(
        self,
        messages: list[tuple[str, int] | tuple[str, int, int | None]] | None = None,
        available: bool = True,
    ) -> None:
        self._messages = []
        for item in messages or []:
            if len(item) == 3:
                url, depth, prio = item
                self._messages.append(KafkaMessage(url, depth, prio))
            else:
                url, depth = item
                self._messages.append(KafkaMessage(url, depth))
        self._available = available
        self.published: list[tuple[str, int, int | None]] = []
        self.commits = 0

    def check_available(self, timeout_s: float = 5.0) -> bool:
        return self._available

    def publish(self, url: str, depth: int = 0, priority: int | None = None) -> bool:
        self.published.append((url, depth, priority))
        return True

    def poll(self) -> KafkaMessage | None:
        if not self._messages:
            return None
        return self._messages.pop(0)

    def commit(self) -> None:
        self.commits += 1

    def close(self) -> None:
        return None


_PAGES: dict[str, list[str]] = {
    "https://kafka.test/a": ["https://kafka.test/b"],
    "https://kafka.test/b": [],
}


def _crawler_factory(hits: dict[str, int]):
    def factory() -> PoliteCrawler:
        crawler = PoliteCrawler()
        crawler.configure(
            {
                "timeout_s": 5,
                "crawl_delay_s": 0,
                "max_pages": 10,
                "respect_robots": False,
            }
        )

        def fake_fetch(url: str) -> _FetchedPage | None:
            hits[url] = hits.get(url, 0) + 1
            if url not in _PAGES:
                return _FetchedPage(
                    status=404, url=url, content_type="text/plain", body=b""
                )
            links = "".join(f'<a href="{link}">x</a>' for link in _PAGES[url])
            body = (
                f"<html><head><title>{url}</title></head><body>{links}</body></html>"
            ).encode()
            return _FetchedPage(
                status=200,
                url=url,
                content_type="text/html",
                body=body,
                headers={},
            )

        crawler._fetch_bounded = fake_fetch  # type: ignore[method-assign]
        crawler.respect_robots = False
        return crawler

    return factory


def _kafka_app_config(db_path: str) -> dict[str, Any]:
    return {
        "storage": {"backend": "sqlite", "sqlite": {"db_path": db_path}},
        "indexing": {
            "timeout_s": 5,
            "crawl_delay_s": 0,
            "max_pages": 10,
            "respect_robots": False,
        },
        "kafka": {
            "enabled": True,
            "bootstrap_servers": ["broker:9092"],
            "topic": "crawl.urls",
            "group_id": "crawlers",
        },
        "providers": {},
    }


# -- section validation --------------------------------------------------


def test_validate_kafka_section_defaults() -> None:
    assert validate_kafka_section(None) == {}
    normalized = validate_kafka_section({})
    assert normalized["enabled"] is False
    assert normalized["topic"] == "crawl.urls"
    assert normalized["group_id"] == "crawlers"


def test_validate_kafka_section_servers_shapes() -> None:
    section = validate_kafka_section({"bootstrap_servers": "a:1, b:2"})
    assert section["bootstrap_servers"] == ["a:1", "b:2"]
    section = validate_kafka_section({"bootstrap_servers": ["a:1"]})
    assert section["bootstrap_servers"] == ["a:1"]
    with pytest.raises(ConfigError):
        validate_kafka_section({"bootstrap_servers": ""})
    with pytest.raises(ConfigError):
        validate_kafka_section({"enabled": "maybe"})
    with pytest.raises(ConfigError):
        validate_kafka_section({"enabled": 2})
    with pytest.raises(ConfigError):
        validate_kafka_section({"topic": ""})
    with pytest.raises(ConfigError):
        validate_kafka_section({"poll_timeout_s": -1})
    with pytest.raises(ConfigError):
        validate_kafka_section({"bogus": 1})


def test_kafka_enabled() -> None:
    assert kafka_enabled(None) is False
    assert kafka_enabled({}) is False
    assert kafka_enabled({"kafka": {"enabled": False}}) is False
    assert (
        kafka_enabled({"kafka": {"enabled": True, "bootstrap_servers": ["b:9092"]}})
        is True
    )


def test_kafka_enabled_coerces_env_strings() -> None:
    """Env substitution stringifies YAML booleans (like frontier.enabled)."""
    assert (
        kafka_enabled({"kafka": {"enabled": "true", "bootstrap_servers": ["b:9092"]}})
        is True
    )
    assert kafka_enabled({"kafka": {"enabled": "false"}}) is False
    assert kafka_enabled({"kafka": {"enabled": "yes"}}) is True
    assert kafka_enabled({"kafka": {"enabled": "maybe"}}) is False
    assert validate_kafka_section({"enabled": "true"})["enabled"] is True
    assert validate_kafka_section({"enabled": "FALSE"})["enabled"] is False


# -- produce / consume units ---------------------------------------------


def test_publish_keys_by_normalized_url() -> None:
    producer = _FakeProducer()
    queue = KafkaQueue.from_app_config(
        {"kafka": {"bootstrap_servers": ["b:1"]}}, producer=producer
    )
    assert queue.publish("  HTTPS://Example.COM/A/ ", 2) is True
    topic, key, value = producer.sent[0]
    assert topic == "crawl.urls"
    assert key == "https://example.com/a"
    assert value == {"url": "HTTPS://Example.COM/A/", "depth": 2}
    assert queue.publish("   ") is False
    queue.close()


def test_publish_failure_surfaces_unavailable() -> None:
    producer = _FakeProducer(fail=True)
    queue = KafkaQueue.from_app_config(
        {"kafka": {"bootstrap_servers": ["b:1"]}}, producer=producer
    )
    with pytest.raises(ProviderError):
        queue.publish("https://example.com/", 0)


def test_poll_commit_cycle() -> None:
    consumer = _FakeConsumer(
        [[{"url": "https://example.com/a", "depth": 3}], [], ["garbage"]]
    )
    queue = KafkaQueue.from_app_config(
        {"kafka": {"bootstrap_servers": ["b:1"]}},
        producer=_FakeProducer(),
        consumer_factory=lambda: consumer,
    )
    first = queue.poll()
    assert first == KafkaMessage(url="https://example.com/a", depth=3)
    queue.commit()
    assert consumer.commits == 1
    assert queue.poll() is None  # idle batch
    # A plain-string message degrades to a depth-0 URL.
    assert queue.poll() == KafkaMessage(url="garbage", depth=0)
    queue.commit()
    assert queue.poll() is None  # batches exhausted
    queue.close()
    assert consumer.closed is True


def test_publish_carries_priority_only_when_explicit() -> None:
    producer = _FakeProducer()
    queue = KafkaQueue.from_app_config(
        {"kafka": {"bootstrap_servers": ["b:1"]}}, producer=producer
    )
    assert queue.publish("https://example.com/hi", 0, priority=7) is True
    assert producer.sent[0][2] == {
        "url": "https://example.com/hi",
        "depth": 0,
        "priority": 7,
    }
    assert queue.publish("https://example.com/lo", 0) is True
    assert producer.sent[1][2] == {"url": "https://example.com/lo", "depth": 0}
    # Explicit zero still travels (it overwrites on conflict downstream).
    assert queue.publish("https://example.com/zero", 0, priority=0) is True
    assert producer.sent[2][2]["priority"] == 0
    queue.close()


def test_poll_parses_priority_and_tolerates_garbage() -> None:
    consumer = _FakeConsumer(
        [
            [{"url": "https://example.com/hi", "depth": 0, "priority": 7}],
            [{"url": "https://example.com/old", "depth": 1}],
            [{"url": "https://example.com/bad", "depth": 0, "priority": "x"}],
        ]
    )
    queue = KafkaQueue.from_app_config(
        {"kafka": {"bootstrap_servers": ["b:1"]}},
        producer=_FakeProducer(),
        consumer_factory=lambda: consumer,
    )
    assert queue.poll() == KafkaMessage(
        url="https://example.com/hi", depth=0, priority=7
    )
    # Pre-upgrade payloads (no key) keep the stored value downstream.
    assert queue.poll() == KafkaMessage(url="https://example.com/old", depth=1)
    # Unparseable priorities degrade to keep-stored, never crash the loop.
    assert queue.poll() == KafkaMessage(url="https://example.com/bad", depth=0)
    queue.close()


class _FakeTopicErrors:
    def __init__(self, errors: list[tuple[str, int, str]]) -> None:
        self.topic_errors = errors


class _FakeAdmin:
    """Offline KafkaAdminClient stand-in (records calls, canned errors)."""

    def __init__(
        self,
        partitions: int = 3,
        replicas: int = 1,
        delete_errors: list[tuple[str, int, str]] | None = None,
        create_errors: list[tuple[str, int, str]] | None = None,
    ) -> None:
        self._partitions = partitions
        self._replicas = replicas
        self._delete_errors = delete_errors or []
        self._create_errors = create_errors or []
        self.deleted: list[str] = []
        self.created: list[tuple[str, int, int]] = []
        self.closed = False

    def describe_topics(self, topics: list[str]) -> list[dict[str, Any]]:
        return [
            {
                "topic": topic,
                "partitions": [
                    {"partition": i, "replicas": [0] * self._replicas}
                    for i in range(self._partitions)
                ],
            }
            for topic in topics
        ]

    def delete_topics(
        self, topics: list[str], timeout_ms: int | None = None
    ) -> _FakeTopicErrors:
        self.deleted.extend(topics)
        return _FakeTopicErrors(self._delete_errors)

    def create_topics(
        self, new_topics: list[Any], timeout_ms: int | None = None
    ) -> _FakeTopicErrors:
        for topic in new_topics:
            self.created.append(
                (topic.name, topic.num_partitions, topic.replication_factor)
            )
        return _FakeTopicErrors(self._create_errors)

    def close(self) -> None:
        self.closed = True


def test_purge_topic_recreates_with_same_layout() -> None:
    admin = _FakeAdmin(partitions=4, replicas=2)
    result = purge_topic(
        {"kafka": {"bootstrap_servers": ["b:1"]}},
        admin_client_factory=lambda: admin,
    )
    assert result == {"purged": True, "partitions": 4}
    assert admin.deleted == ["crawl.urls"]
    assert admin.created == [("crawl.urls", 4, 2)]
    assert admin.closed is True


def test_purge_topic_tolerates_unknown_topic_on_delete() -> None:
    admin = _FakeAdmin(delete_errors=[("crawl.urls", 3, "unknown")])
    result = purge_topic(
        {"kafka": {"bootstrap_servers": ["b:1"]}},
        admin_client_factory=lambda: admin,
    )
    assert result["purged"] is True
    assert admin.created and admin.created[0][0] == "crawl.urls"


def test_purge_topic_surfaces_broker_errors() -> None:
    admin = _FakeAdmin(delete_errors=[("crawl.urls", 7, "request timed out")])
    with pytest.raises(ProviderError):
        purge_topic(
            {"kafka": {"bootstrap_servers": ["b:1"]}},
            admin_client_factory=lambda: admin,
        )


def test_purge_topic_fails_closed_without_driver() -> None:
    def _boom() -> Any:
        raise ImportError("no kafka package")

    with pytest.raises(ProviderError):
        purge_topic(
            {"kafka": {"bootstrap_servers": ["b:1"]}}, admin_client_factory=_boom
        )


def test_missing_driver_fails_closed() -> None:
    with pytest.raises(ProviderError):
        KafkaQueue(bootstrap_servers=["b:1"], producer=None)


def test_open_kafka_queue_factory() -> None:
    queue = open_kafka_queue(
        {"kafka": {"bootstrap_servers": ["b:1"]}}, producer=_FakeProducer()
    )
    assert queue.publish("https://example.com/", 0) is True
    queue.close()


# -- worker loop ----------------------------------------------------------


def test_run_kafka_worker_drains_and_fans_out(tmp_path: Path) -> None:
    db_path = str(tmp_path / "frontier.db")
    fake = _FakeQueue([("https://kafka.test/a", 0)])
    hits: dict[str, int] = {}
    counts = run_kafka_worker(
        [],
        _kafka_app_config(db_path),
        worker_id="w1",
        crawler_factory=_crawler_factory(hits),
        queue=fake,
        max_iterations=10,
        idle_sleep_s=0.01,
    )
    assert counts["received"] == 1
    assert fake.commits == 1
    # Seed fetched; the discovered link fanned back out to the topic...
    assert ("https://kafka.test/b", 1, None) in fake.published
    # ...and durably recorded in the store (no refetch on redelivery).
    store = SqliteFrontierStore(db_path)
    try:
        pending_urls = {entry.url for entry in store.pending(limit=100)}
    finally:
        store.close()
    assert "https://kafka.test/a" not in pending_urls
    assert "https://kafka.test/b" not in pending_urls
    assert hits.get("https://kafka.test/a", 0) == 1


def test_run_kafka_worker_passes_message_priority_to_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The drain enqueues the consumed message with its Kafka priority."""
    db_path = str(tmp_path / "frontier.db")
    seen: list[tuple[str, int | None]] = []
    original_enqueue = SqliteFrontierStore.enqueue

    def _spy(
        self: SqliteFrontierStore,
        url: str,
        depth: int = 0,
        parent_url: str | None = None,
        priority: int | None = None,
    ) -> bool:
        seen.append((url, priority))
        return original_enqueue(
            self, url, depth, parent_url=parent_url, priority=priority
        )

    monkeypatch.setattr(SqliteFrontierStore, "enqueue", _spy)
    fake = _FakeQueue([("https://kafka.test/a", 0, 7)])
    hits: dict[str, int] = {}
    counts = run_kafka_worker(
        [],
        _kafka_app_config(db_path),
        worker_id="w1",
        crawler_factory=_crawler_factory(hits),
        queue=fake,
        max_iterations=10,
        idle_sleep_s=0.01,
    )
    assert counts["received"] == 1
    assert ("https://kafka.test/a", 7) in seen


def test_run_kafka_worker_drains_store_backlog_on_startup(tmp_path: Path) -> None:
    """Stranded store rows (fallback seeds) don't wait for the next message."""
    db_path = str(tmp_path / "frontier.db")
    store = SqliteFrontierStore(db_path)
    try:
        assert store.enqueue("https://kafka.test/a", 0) is True
    finally:
        store.close()
    fake = _FakeQueue([])
    hits: dict[str, int] = {}
    counts = run_kafka_worker(
        [],
        _kafka_app_config(db_path),
        worker_id="w1",
        crawler_factory=_crawler_factory(hits),
        queue=fake,
        max_iterations=0,
        idle_sleep_s=0.01,
    )
    assert counts["received"] == 0
    # The seed plus its discovered link both drain in the startup pass.
    assert counts["added"] == 2
    assert hits.get("https://kafka.test/a", 0) == 1
    assert hits.get("https://kafka.test/b", 0) == 1


def test_run_kafka_worker_idle_polls_drain_store_backlog(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Store seeds stranded behind an empty topic are still crawled.

    Seeds that land in the store after worker startup (fallback path
    during a broker outage) must not wait for the next Kafka message:
    every Nth idle poll drains the store. ``/c`` below is enqueued
    mid-run (it is linked from nowhere, so only the idle drain can
    reach it).
    """
    monkeypatch.setattr("openlens.indexing.worker._IDLE_DRAIN_EVERY_POLLS", 3)
    db_path = str(tmp_path / "frontier.db")
    store = SqliteFrontierStore(db_path)
    try:
        assert store.enqueue("https://kafka.test/a", 0) is True
    finally:
        store.close()
    polls = 0

    def seeding_poll() -> KafkaMessage | None:
        nonlocal polls
        polls += 1
        if polls == 5:
            late = SqliteFrontierStore(db_path)
            try:
                assert late.enqueue("https://kafka.test/c", 0) is True
            finally:
                late.close()
        return None

    fake = _FakeQueue([])
    fake.poll = seeding_poll  # type: ignore[method-assign]
    hits: dict[str, int] = {}
    counts = run_kafka_worker(
        [],
        _kafka_app_config(db_path),
        worker_id="w1",
        crawler_factory=_crawler_factory(hits),
        queue=fake,
        max_iterations=10,
        idle_sleep_s=0.01,
    )
    assert counts["received"] == 0
    assert hits.get("https://kafka.test/a", 0) == 1
    # Reached purely through the idle drain (404s retry per
    # max_attempts_per_url, so only presence is asserted).
    assert hits.get("https://kafka.test/c", 0) >= 1


def test_run_kafka_worker_respects_pause(tmp_path: Path) -> None:
    db_path = str(tmp_path / "frontier.db")
    store = SqliteFrontierStore(db_path)
    try:
        store.save_settings({"paused": True})
    finally:
        store.close()
    fake = _FakeQueue([("https://kafka.test/a", 0)])
    hits: dict[str, int] = {}
    counts = run_kafka_worker(
        [],
        _kafka_app_config(db_path),
        worker_id="w1",
        crawler_factory=_crawler_factory(hits),
        queue=fake,
        max_iterations=3,
        idle_sleep_s=0.01,
    )
    assert counts["received"] == 0
    assert fake.commits == 0
    assert hits == {}


def test_run_kafka_worker_broker_down_fails_loud(tmp_path: Path) -> None:
    fake = _FakeQueue([], available=False)
    with pytest.raises(ProviderError):
        run_kafka_worker(
            [],
            _kafka_app_config(str(tmp_path / "frontier.db")),
            crawler_factory=_crawler_factory({}),
            queue=fake,
            max_iterations=1,
        )


# -- API bridge ------------------------------------------------------------


def _api_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **overrides: Any
) -> TestClient:
    client = TestClient(create_app(engine=FakeEngine()))
    config = dict(client.app.state.config)
    config["storage"] = {
        "backend": "sqlite",
        "sqlite": {"db_path": str(tmp_path / "api-frontier.db")},
    }
    config.update(overrides)
    client.app.state.config = config
    return client


def test_resume_publishes_to_kafka(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _FakeQueue()
    monkeypatch.setattr(routes_mod, "kafka_enabled", lambda config: True)
    monkeypatch.setattr(routes_mod, "open_kafka_queue", lambda config: fake)
    client = _api_client(monkeypatch, tmp_path)
    resp = client.post(
        "/api/v1/frontier/resume", json={"seeds": ["https://kafka.test/a"]}
    )
    assert resp.status_code == 200, resp.text
    assert fake.published == [("https://kafka.test/a", 0, None)]
    # No explicit priority: Kafka-only, no store copy.
    store = SqliteFrontierStore(str(tmp_path / "api-frontier.db"))
    try:
        assert store.is_known("https://kafka.test/a") is False
    finally:
        store.close()


def test_resume_falls_back_to_store_when_broker_down(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(routes_mod, "kafka_enabled", lambda config: True)

    def _boom(config: Any) -> Any:
        raise ProviderError(
            "broker down",
            provider_name="kafka_queue",
            code=ProviderError.UNAVAILABLE,
        )

    monkeypatch.setattr(routes_mod, "open_kafka_queue", _boom)
    client = _api_client(monkeypatch, tmp_path)
    resp = client.post(
        "/api/v1/frontier/resume", json={"seeds": ["https://kafka.test/a"]}
    )
    assert resp.status_code == 200, resp.text
    store = SqliteFrontierStore(str(tmp_path / "api-frontier.db"))
    try:
        assert {e.url for e in store.pending(limit=100)} == {"https://kafka.test/a"}
        assert store.get_settings().get("paused") is False
    finally:
        store.close()


def test_resume_priority_reaches_kafka_and_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _FakeQueue()
    monkeypatch.setattr(routes_mod, "kafka_enabled", lambda config: True)
    monkeypatch.setattr(routes_mod, "open_kafka_queue", lambda config: fake)
    client = _api_client(monkeypatch, tmp_path)
    resp = client.post(
        "/api/v1/frontier/resume",
        json={"seeds": ["https://kafka.test/a"], "priority": 6},
    )
    assert resp.status_code == 200, resp.text
    assert fake.published == [("https://kafka.test/a", 0, 6)]
    assert resp.json()["queue"][0]["priority"] == 6
    # Fast-lane: the explicit priority also lands in the store, so the
    # next per-message drain claims it first instead of waiting behind
    # the topic backlog in offset order.
    store = SqliteFrontierStore(str(tmp_path / "api-frontier.db"))
    try:
        assert [(e.url, e.priority) for e in store.pending(limit=100)] == [
            ("https://kafka.test/a", 6)
        ]
    finally:
        store.close()


def test_resume_priority_rejects_non_integers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = _api_client(monkeypatch, tmp_path)
    for bad in ("high", 1.5):
        resp = client.post(
            "/api/v1/frontier/resume",
            json={"seeds": ["https://kafka.test/a"], "priority": bad},
        )
        assert resp.status_code == 422, bad


def test_resume_priority_falls_back_to_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(routes_mod, "kafka_enabled", lambda config: True)

    def _boom(config: Any) -> Any:
        raise ProviderError(
            "broker down",
            provider_name="kafka_queue",
            code=ProviderError.UNAVAILABLE,
        )

    monkeypatch.setattr(routes_mod, "open_kafka_queue", _boom)
    client = _api_client(monkeypatch, tmp_path)
    resp = client.post(
        "/api/v1/frontier/resume",
        json={"seeds": ["https://kafka.test/a"], "priority": 4},
    )
    assert resp.status_code == 200, resp.text
    store = SqliteFrontierStore(str(tmp_path / "api-frontier.db"))
    try:
        entries = store.pending(limit=100)
        assert [(e.url, e.priority) for e in entries] == [("https://kafka.test/a", 4)]
    finally:
        store.close()


def test_clear_pauses_and_clears_store_queue(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = _api_client(monkeypatch, tmp_path)
    db = str(tmp_path / "api-frontier.db")
    store = SqliteFrontierStore(db)
    try:
        assert store.enqueue("https://kafka.test/a", 0) is True
        assert store.enqueue("https://kafka.test/b", 1, priority=2) is True
    finally:
        store.close()
    resp = client.post("/api/v1/frontier/clear")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["paused"] is True
    assert body["cleared_queued"] == 2
    assert body["kafka_purged"] is False  # kafka disabled in this client
    assert body["queue"] == []
    assert body["checkpoint"]["phase"] == "paused"
    store = SqliteFrontierStore(db)
    try:
        assert store.pending(limit=100) == []
        assert store.get_settings().get("paused") is True
    finally:
        store.close()


def test_clear_purges_kafka_when_enabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(routes_mod, "kafka_enabled", lambda config: True)
    purged: list[Any] = []

    def _fake_purge(config: Any) -> dict[str, Any]:
        purged.append(config)
        return {"purged": True, "partitions": 3}

    monkeypatch.setattr(routes_mod, "purge_topic", _fake_purge)
    client = _api_client(monkeypatch, tmp_path)
    resp = client.post("/api/v1/frontier/clear")
    assert resp.status_code == 200, resp.text
    assert resp.json()["kafka_purged"] is True
    assert len(purged) == 1


def test_clear_reports_purge_failure_without_failing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A dead broker still yields 200: pause+store clear stand alone."""
    monkeypatch.setattr(routes_mod, "kafka_enabled", lambda config: True)

    def _boom(config: Any) -> Any:
        raise ProviderError(
            "broker down",
            provider_name="kafka_queue",
            code=ProviderError.UNAVAILABLE,
        )

    monkeypatch.setattr(routes_mod, "purge_topic", _boom)
    client = _api_client(monkeypatch, tmp_path)
    resp = client.post("/api/v1/frontier/clear")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["paused"] is True
    assert body["kafka_purged"] is False


def test_pause_resume_flip_store_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = _api_client(monkeypatch, tmp_path)
    db = str(tmp_path / "api-frontier.db")

    def _settings() -> dict[str, Any]:
        store = SqliteFrontierStore(db)
        try:
            return store.get_settings()
        finally:
            store.close()

    assert client.post("/api/v1/frontier/pause").status_code == 200
    assert _settings().get("paused") is True
    assert client.post("/api/v1/frontier/resume", json={}).status_code == 200
    assert _settings().get("paused") is False


# -- crawl seams ------------------------------------------------------------


def test_persistent_frontier_push_hook(tmp_path: Path) -> None:
    seen: list[tuple[str, int]] = []
    store = SqliteFrontierStore(str(tmp_path / "hook.db"))
    try:
        frontier = crawler_mod.PersistentFrontier(
            store, 100, on_push=lambda url, depth: seen.append((url, depth))
        )
        frontier.push("https://example.com/a", 2)
        # Re-sighting a known URL refires nothing: the store row is the
        # durable record and any drain picks it up, so the topic would
        # otherwise carry one copy per page linking it.
        frontier.push("https://example.com/a", 2)
        frontier.push("https://example.com/b", 1)
    finally:
        store.close()
    assert seen == [("https://example.com/a", 2), ("https://example.com/b", 1)]


def test_package_init_does_not_preimport_worker() -> None:
    """Regression: `python -m openlens.indexing.worker` must not warn."""
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, openlens.indexing; sys.exit("
            "'openlens.indexing.worker' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr

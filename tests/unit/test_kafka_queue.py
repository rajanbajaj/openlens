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
        messages: list[tuple[str, int]] | None = None,
        available: bool = True,
    ) -> None:
        self._messages = [KafkaMessage(url, depth) for url, depth in (messages or [])]
        self._available = available
        self.published: list[tuple[str, int]] = []
        self.commits = 0

    def check_available(self, timeout_s: float = 5.0) -> bool:
        return self._available

    def publish(self, url: str, depth: int = 0) -> bool:
        self.published.append((url, depth))
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
    assert ("https://kafka.test/b", 1) in fake.published
    # ...and durably recorded in the store (no refetch on redelivery).
    store = SqliteFrontierStore(db_path)
    try:
        pending_urls = {entry.url for entry in store.pending(limit=100)}
    finally:
        store.close()
    assert "https://kafka.test/a" not in pending_urls
    assert "https://kafka.test/b" not in pending_urls
    assert hits.get("https://kafka.test/a", 0) == 1


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
    assert fake.published == [("https://kafka.test/a", 0)]


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
    finally:
        store.close()
    assert seen == [("https://example.com/a", 2)]


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

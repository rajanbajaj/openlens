"""Discovery + refresh worker passes (offline: real SQLite frontier store,
fake feed fetches, backdated rows for age tests)."""

from __future__ import annotations

from typing import Any

from openlens.indexing.crawler import PoliteCrawler
from openlens.indexing.worker import CrawlerWorker
from openlens.storage.frontier import SqliteFrontierStore
from openlens.indexing import discovery as discovery_module

FEED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Example News</title>
<item><title>Fresh one</title><link>https://news.example/fresh-one</link></item>
<item><title>Fresh two</title><link>https://news.example/fresh-two</link></item>
</channel></rss>
"""

SITEMAP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://news.example/sitemap-story</loc></url>
</urlset>
"""


def _worker(store: SqliteFrontierStore) -> CrawlerWorker:
    crawler = PoliteCrawler()
    crawler.configure({"default_max_depth": 3})
    return CrawlerWorker(
        crawler=crawler,
        repository=None,
        frontier_store=store,
    )


def _settings(
    store: SqliteFrontierStore,
    **overrides: Any,
) -> None:
    payload: dict[str, Any] = {
        "default_max_depth": 3,
        "discovery_feeds": [],
        "refresh_after_days": 0,
        "discovery_interval_s": 3600,
    }
    payload.update(overrides)
    store.save_settings(payload)


def _backdate(store: SqliteFrontierStore, url: str) -> None:
    store._conn.execute(  # noqa: SLF001 — white-box backdate for age tests
        "UPDATE frontier_queue SET fetched_at = '2000-01-01T00:00:00Z' WHERE url = ?",
        (url,),
    )


def test_discovery_pass_enqueues_only_new_urls() -> None:
    store = SqliteFrontierStore(":memory:")
    assert store.enqueue("https://news.example/fresh-one", depth=0)
    _settings(store, discovery_feeds=["https://news.example/feed.xml"])
    worker = _worker(store)
    worker.refresh_limits()

    def fake_fetch(url: str, **kwargs: Any) -> str | None:
        assert url == "https://news.example/feed.xml"
        return FEED_XML

    result = worker.run_discovery_pass(http_fetch=fake_fetch)
    assert result["feeds_checked"] == 1
    assert result["new_urls"] == 1
    assert store.status_of("https://news.example/fresh-two") == "queued"


def test_discovery_pass_handles_sitemaps() -> None:
    store = SqliteFrontierStore(":memory:")
    _settings(store, discovery_feeds=["https://news.example/sitemap.xml"])
    worker = _worker(store)
    worker.refresh_limits()
    result = worker.run_discovery_pass(http_fetch=lambda url, **kwargs: SITEMAP_XML)
    assert result["new_urls"] == 1
    assert store.status_of("https://news.example/sitemap-story") == "queued"


def test_discovery_pass_failed_fetch_counts_zero() -> None:
    store = SqliteFrontierStore(":memory:")
    _settings(store, discovery_feeds=["https://news.example/feed.xml"])
    worker = _worker(store)
    worker.refresh_limits()

    def boom(url: str, **kwargs: Any) -> str | None:
        raise RuntimeError("network down")

    result = worker.run_discovery_pass(http_fetch=boom)
    assert result == {"feeds_checked": 1, "new_urls": 0, "refreshed": 0}


def test_discovery_pass_refreshes_stale_done() -> None:
    store = SqliteFrontierStore(":memory:")
    assert store.enqueue("https://news.example/old", depth=0)
    store.mark_done("https://news.example/old")
    _backdate(store, "https://news.example/old")
    _settings(store, refresh_after_days=30)
    worker = _worker(store)
    worker.refresh_limits()
    result = worker.run_discovery_pass()
    assert result["refreshed"] == 1
    assert store.status_of("https://news.example/old") == "queued"


def test_discovery_pass_disabled_by_default() -> None:
    store = SqliteFrontierStore(":memory:")
    assert store.enqueue("https://news.example/old", depth=0)
    store.mark_done("https://news.example/old")
    _backdate(store, "https://news.example/old")
    _settings(store)
    worker = _worker(store)
    worker.refresh_limits()
    result = worker.run_discovery_pass()
    assert result == {"feeds_checked": 0, "new_urls": 0, "refreshed": 0}
    assert store.status_of("https://news.example/old") == "done"


def test_maybe_run_discovery_gates_on_interval() -> None:
    store = SqliteFrontierStore(":memory:")
    _settings(
        store,
        discovery_feeds=["https://news.example/feed.xml"],
        discovery_interval_s=3600,
    )
    worker = _worker(store)
    worker.refresh_limits()
    calls = worker.maybe_run_discovery(http_fetch=lambda url, **kwargs: FEED_XML)
    assert calls["feeds_checked"] == 1
    gated = worker.maybe_run_discovery(http_fetch=lambda url, **kwargs: FEED_XML)
    assert gated == {"feeds_checked": 0, "new_urls": 0, "refreshed": 0}


def test_maybe_run_discovery_first_pass_ignores_boot_time(
    monkeypatch: Any,
) -> None:
    """A freshly booted machine (monotonic near zero) still runs discovery."""
    import time as time_module

    store = SqliteFrontierStore(":memory:")
    _settings(
        store,
        discovery_feeds=["https://news.example/feed.xml"],
        discovery_interval_s=3600,
    )
    worker = _worker(store)
    worker.refresh_limits()
    monkeypatch.setattr(time_module, "monotonic", lambda: 100.0)
    calls = worker.maybe_run_discovery(http_fetch=lambda url, **kwargs: FEED_XML)
    assert calls["feeds_checked"] == 1


def test_refresh_limits_validates_discovery_settings() -> None:
    store = SqliteFrontierStore(":memory:")
    _settings(
        store,
        discovery_feeds=[
            "https://news.example/feed.xml",
            "not-a-url",
            42,
            "https://news.example/" + "x" * 5000,
        ],
        refresh_after_days=-5,
        discovery_interval_s="hourly",
    )
    worker = _worker(store)
    worker.refresh_limits()
    assert worker.discovery_feeds == ["https://news.example/feed.xml"]
    assert worker.refresh_after_days == 0
    assert worker.discovery_interval_s == 21600
    assert discovery_module is not None

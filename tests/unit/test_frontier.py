"""Unit tests for the persistent URL frontier (storage + crawler + API).

Offline only: SQLite ``:memory:`` stores, a loopback stub HTTP server
(never real network; skip when loopback bind is denied), and the
FastAPI TestClient over a fake engine.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from openlens.core.interfaces import ConfigError
from openlens.indexing import run_index
from openlens.indexing.crawler import PoliteCrawler
from openlens.main import create_app
from openlens.storage.frontier import (
    DEFAULT_CRAWL_ID,
    CrawlCheckpoint,
    SqliteFrontierStore,
    open_frontier_store,
)
from openlens.storage.repository import DocumentRepository


def _crawler(**overrides: Any) -> PoliteCrawler:
    config: dict[str, Any] = {
        "timeout_s": 5,
        "crawl_delay_s": 0,
        "max_pages": 10,
        "respect_robots": True,
    }
    config.update(overrides)
    crawler = PoliteCrawler()
    crawler.configure(config)
    return crawler


# --- store -----------------------------------------------------------------


def test_enqueue_dedups_normalized_urls() -> None:
    store = SqliteFrontierStore(":memory:")
    try:
        assert store.enqueue("https://example.com/Page", 0) is True
        assert store.enqueue("https://example.com/page#frag", 0) is False
        assert store.enqueue("https://example.com/page/", 0) is False
        counts = store.counts()
        assert counts.get("queued") == 1
        assert counts.get("total") == 1
    finally:
        store.close()


def test_dequeue_depth_order_and_mark_done() -> None:
    store = SqliteFrontierStore(":memory:")
    try:
        store.enqueue("https://example.com/deep", 2)
        store.enqueue("https://example.com/shallow", 0)
        first = store.dequeue()
        assert first is not None and first.url.endswith("/shallow")
        assert first.status == "active"
        store.mark_done(first.url, etag='"abc"')
        counts = store.counts()
        assert counts.get("done") == 1
        assert store.validators()[first.url]["etag"] == '"abc"'
    finally:
        store.close()


def test_requeue_active_restores_pause() -> None:
    store = SqliteFrontierStore(":memory:")
    try:
        store.enqueue("https://example.com/x", 0)
        assert store.dequeue() is not None
        assert store.counts().get("queued", 0) == 0
        assert store.requeue_active() == 1
        assert store.counts().get("queued") == 1
    finally:
        store.close()


def test_checkpoint_and_settings_round_trip() -> None:
    store = SqliteFrontierStore(":memory:")
    try:
        assert store.load_checkpoint("missing") is None
        store.save_checkpoint(
            CrawlCheckpoint(
                crawl_id=DEFAULT_CRAWL_ID,
                seeds=["https://example.com/"],
                phase="paused",
                pages_attempted=3,
                pages_indexed=2,
                frontier_size=5,
                stats={"fetched": 2},
            )
        )
        loaded = store.load_checkpoint(DEFAULT_CRAWL_ID)
        assert loaded is not None
        assert loaded.phase == "paused"
        assert loaded.seeds == ["https://example.com/"]
        assert loaded.pages_attempted == 3
        assert loaded.stats == {"fetched": 2}
        assert store.get_settings() == {}
        store.save_settings({"max_pages": 50})
        assert store.get_settings() == {"max_pages": 50}
    finally:
        store.close()


def test_open_frontier_store_defaults_to_sqlite() -> None:
    store = open_frontier_store(
        {"storage": {"backend": "sqlite", "sqlite": {"db_path": ":memory:"}}}
    )
    try:
        assert isinstance(store, SqliteFrontierStore)
        assert store.enqueue("https://example.com/q", 0) is True
    finally:
        store.close()


# --- crawler persistent path ------------------------------------------------


_PAGES: dict[str, list[str]] = {
    "https://crawl.test/a": ["https://crawl.test/b", "https://crawl.test/c"],
    "https://crawl.test/b": [],
    "https://crawl.test/c": [],
}


def _install_fake_fetch(crawler: PoliteCrawler, hits: dict[str, int]) -> None:
    """Serve a fixed 3-page site without any network (hermetic)."""
    from openlens.indexing.crawler import _FetchedPage

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
            status=200, url=url, content_type="text/html", body=body, headers={}
        )

    crawler._fetch_bounded = fake_fetch  # type: ignore[method-assign]
    crawler.respect_robots = False


def test_crawl_through_store_marks_done_and_checkpoints() -> None:
    store = SqliteFrontierStore(":memory:")
    try:
        crawler = _crawler(max_pages=10)
        hits: dict[str, int] = {}
        _install_fake_fetch(crawler, hits)
        docs = crawler.crawl(["https://crawl.test/a"], frontier_store=store)
        assert {d.url for d in docs} == set(_PAGES)
        counts = store.counts()
        assert counts.get("done") == 3
        assert counts.get("queued", 0) == 0
        checkpoint = store.load_checkpoint()
        assert checkpoint is not None
        assert checkpoint.phase == "done"
        assert checkpoint.pages_attempted == 3
        assert crawler.last_checkpoint["phase"] == "done"
    finally:
        store.close()


def test_pause_then_resume_avoids_refetch() -> None:
    store = SqliteFrontierStore(":memory:")
    try:
        crawler = _crawler(max_pages=10)
        hits: dict[str, int] = {}
        _install_fake_fetch(crawler, hits)
        fetches = {"n": 0}
        orig_fetch = crawler._fetch_bounded

        def counting_fetch(url: str) -> Any:
            fetches["n"] += 1
            return orig_fetch(url)

        crawler._fetch_bounded = counting_fetch  # type: ignore[method-assign]
        first = crawler.crawl(
            ["https://crawl.test/a"],
            frontier_store=store,
            should_stop=lambda: fetches["n"] >= 1,
        )
        assert len(first) == 1
        assert store.load_checkpoint() is not None
        assert store.load_checkpoint().phase == "paused"

        resumed = crawler.crawl(["https://crawl.test/a"], frontier_store=store)
        resumed_urls = {d.url for d in resumed}
        assert resumed_urls == {"https://crawl.test/b", "https://crawl.test/c"}
        # The paused-on page was already done: fetched exactly once.
        assert hits.get("https://crawl.test/a") == 1
        assert store.load_checkpoint().phase == "done"
    finally:
        store.close()


def test_run_index_with_frontier_store() -> None:
    repo = DocumentRepository(":memory:")
    store = SqliteFrontierStore(":memory:")
    try:
        crawler = _crawler(max_pages=10)
        hits: dict[str, int] = {}
        _install_fake_fetch(crawler, hits)
        stats = run_index(
            ["https://crawl.test/a"],
            repo,
            {"timeout_s": 5, "crawl_delay_s": 0, "max_pages": 10},
            crawler=crawler,
            frontier_store=store,
        )
        assert stats.added == 3
        assert repo.count() == 3
        # New-URL detection: a second incremental run over the same crawl
        # output reports everything unchanged.
        from openlens.indexing.indexer import DocumentIndexer

        fresh = SqliteFrontierStore(":memory:")
        try:
            probe = _crawler(max_pages=10)
            probe_hits: dict[str, int] = {}
            _install_fake_fetch(probe, probe_hits)
            docs = probe.crawl(["https://crawl.test/a"], frontier_store=fresh)
        finally:
            fresh.close()
        preview = DocumentIndexer(repo).describe_refresh(docs)
        assert preview.summary() == {
            "added": 0,
            "updated": 0,
            "unchanged": 3,
            "duplicates": 0,
        }
    finally:
        store.close()
        repo.close()


def test_crawler_rejects_bad_frontier_config() -> None:
    crawler = PoliteCrawler()
    with pytest.raises(ConfigError):
        crawler.configure({"checkpoint_interval_pages": 0})
    with pytest.raises(ConfigError):
        crawler.configure({"max_attempts_per_url": 0})
    with pytest.raises(ConfigError):
        crawler.configure({"frontier_enabled": "yes"})
    crawler.configure({"checkpoint_interval_pages": 2, "max_attempts_per_url": 1})
    assert crawler.checkpoint_interval_pages == 2
    assert crawler.max_attempts_per_url == 1


# --- API --------------------------------------------------------------------


class _FakeEngine:
    def search(self, request: Any) -> Any:  # pragma: no cover
        raise AssertionError("frontier tests never search")


def _client() -> TestClient:
    return TestClient(create_app(engine=_FakeEngine()))  # type: ignore[arg-type]


def test_frontier_status_pause_resume_settings_flow() -> None:
    client = _client()
    resp = client.get("/api/v1/frontier/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["queue_depth"] == 0
    assert body["checkpoint"]["phase"] in ("idle", "running", "paused", "done")

    resp = client.post("/api/v1/frontier/pause")
    assert resp.status_code == 200
    assert resp.json()["paused"] is True
    assert resp.json()["checkpoint"]["phase"] == "paused"

    resp = client.post(
        "/api/v1/frontier/resume", json={"seeds": ["https://example.com/"]}
    )
    assert resp.status_code == 200
    resumed = resp.json()
    assert resumed["paused"] is False
    assert resumed["queue_depth"] == 1

    resp = client.get("/api/v1/frontier/settings")
    assert resp.status_code == 200
    assert "max_pages" in resp.json()["settings"]

    resp = client.put(
        "/api/v1/frontier/settings",
        json={
            "max_pages": 25,
            "max_queued_urls": 500,
            "crawl_delay_s": 0.1,
            "default_max_depth": 2,
            "respect_robots": True,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["settings"]["max_pages"] == 25

    resp = client.put("/api/v1/frontier/settings", json={"nope": 1, "max_pages": 5})
    assert resp.status_code == 422

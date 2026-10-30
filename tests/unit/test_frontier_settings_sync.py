"""Dashboard settings reach the crawlers (fail-open local dev under test).

Regression tests for the depth-cap gap: ``PUT /frontier/settings``
used to stay in backend memory, so workers enforced stale boot values
(``default_max_depth`` unset in the file config = unlimited) and
crawled past the dashboard cap. Settings now persist in the frontier
store and workers refresh the cap from the shared row.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from openlens.api import frontier_state
from openlens.api.routes import ALLOW_OPEN_ADMIN_ENV
from openlens.indexing.crawler import PoliteCrawler
from openlens.indexing.worker import CrawlerWorker as CrawlWorker
from openlens.main import create_app

from tests.unit.test_api import FakeEngine


@pytest.fixture()
def client(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,  # type: ignore[no-untyped-def]
) -> TestClient:
    """App whose frontier store is an isolated tmp sqlite file."""
    monkeypatch.setenv(ALLOW_OPEN_ADMIN_ENV, "true")
    monkeypatch.delenv("OPENLENS_FRONTIER_ADMIN_TOKEN", raising=False)
    frontier_state.reset_frontier_state()
    app = create_app(engine=FakeEngine())
    app.state.config = {
        "storage": {"sqlite": {"db_path": str(tmp_path / "settings.db")}}
    }
    return TestClient(app)


def test_lifespan_seeds_empty_store_row_without_overwriting(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,  # type: ignore[no-untyped-def]
) -> None:
    """Boot backfills missing settings keys; saved values always win."""
    from openlens.storage.frontier import open_frontier_store

    db_path = str(tmp_path / "boot.db")
    config = {"storage": {"sqlite": {"db_path": db_path}}}
    frontier_state.reset_frontier_state()
    app = create_app(engine=FakeEngine())
    app.state.config = config
    with TestClient(app):
        pass
    assert open_frontier_store(config).get_settings()["default_max_depth"] == 3
    store = open_frontier_store(config)
    try:
        saved = dict(store.get_settings())
        saved["default_max_depth"] = 5
        store.save_settings(saved)
    finally:
        store.close()
    frontier_state.reset_frontier_state()
    with TestClient(app):
        pass
    assert open_frontier_store(config).get_settings()["default_max_depth"] == 5
    # A row that only carries the pause flag still gets missing keys.
    store = open_frontier_store(config)
    try:
        store.save_settings({"paused": False})
    finally:
        store.close()
    frontier_state.reset_frontier_state()
    with TestClient(app):
        pass
    row = open_frontier_store(config).get_settings()
    assert row["default_max_depth"] == 3
    assert row["paused"] is False


def test_put_settings_persist_to_store_and_survive_memory_reset(
    client: TestClient,
) -> None:
    put = client.put(
        "/api/v1/frontier/settings",
        json={
            "max_pages": 10,
            "max_queued_urls": 50,
            "crawl_delay_s": 0.5,
            "default_max_depth": 2,
            "respect_robots": False,
        },
    )
    assert put.status_code == 200, put.text
    # Backend restart equivalent: drop the in-memory snapshot entirely.
    frontier_state.reset_frontier_state()
    got = client.get("/api/v1/frontier/settings")
    assert got.status_code == 200, got.text
    assert got.json()["settings"]["default_max_depth"] == 2


class _SettingsStore:
    def __init__(self, settings: object) -> None:
        self._settings = settings

    def get_settings(self) -> object:
        if isinstance(self._settings, Exception):
            raise self._settings
        return self._settings


def _worker(settings: object) -> CrawlWorker:
    crawler = PoliteCrawler()
    crawler.configure({"default_max_depth": 5})
    return CrawlWorker(
        crawler=crawler,
        repository=None,
        frontier_store=_SettingsStore(settings),
    )


def test_refresh_limits_applies_stored_depth_cap() -> None:
    worker = _worker({"default_max_depth": 1})
    worker.refresh_limits()
    assert worker.crawler.default_max_depth == 1


def test_refresh_limits_ignores_missing_or_invalid_cap() -> None:
    worker = _worker({})
    worker.refresh_limits()
    assert worker.crawler.default_max_depth == 5
    for bad in (True, "3", -1, 2.5, None):
        worker = _worker({"default_max_depth": bad})
        worker.refresh_limits()
        assert worker.crawler.default_max_depth == 5


def test_refresh_limits_fail_open_on_store_error() -> None:
    worker = _worker(RuntimeError("store down"))
    worker.refresh_limits()
    assert worker.crawler.default_max_depth == 5


def test_refresh_limits_no_store_or_crawler_is_noop() -> None:
    worker = CrawlWorker(crawler=None, repository=None, frontier_store=None)
    worker.refresh_limits()

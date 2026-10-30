"""Unit tests for the standalone crawler worker (indexing/worker.py + CLI).

Offline only: ``:memory:`` SQLite frontier stores, ``:memory:``
repositories, and a hermetic fake fetch (no network, no loopback bind).
"""

from __future__ import annotations

import contextlib
import urllib.robotparser
from pathlib import Path
from typing import Any

import pytest

import openlens.cli as cli
from openlens.indexing.crawler import PoliteCrawler, _FetchedPage, extract_visible_text
from openlens.indexing.worker import CrawlerWorker, new_worker_id
from openlens.storage.frontier import SqliteFrontierStore
from openlens.storage.repository import DocumentRepository


_PAGES: dict[str, list[str]] = {
    "https://worker.test/a": ["https://worker.test/b", "https://worker.test/c"],
    "https://worker.test/b": [],
    "https://worker.test/c": [],
}


def _install_fake_fetch(crawler: PoliteCrawler, hits: dict[str, int]) -> None:
    """Serve a fixed 3-page site without any network (hermetic)."""

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


def _crawler(**overrides: Any) -> PoliteCrawler:
    config: dict[str, Any] = {
        "timeout_s": 5,
        "crawl_delay_s": 0,
        "max_pages": 10,
        "respect_robots": False,
    }
    config.update(overrides)
    crawler = PoliteCrawler()
    crawler.configure(config)
    return crawler


def _worker(
    crawler: PoliteCrawler,
    repo: DocumentRepository,
    store: SqliteFrontierStore,
    **overrides: Any,
) -> CrawlerWorker:
    kwargs: dict[str, Any] = {
        "crawler": crawler,
        "repository": repo,
        "frontier_store": store,
        "worker_id": "test-worker",
        "crawl_id": "test-crawl",
        "indexing_config": {"timeout_s": 5, "crawl_delay_s": 0, "max_pages": 10},
        "heartbeat_interval_s": 3600.0,
    }
    kwargs.update(overrides)
    return CrawlerWorker(**kwargs)


def test_worker_claim_fetch_mark_done() -> None:
    repo = DocumentRepository(":memory:")
    store = SqliteFrontierStore(":memory:")
    try:
        crawler = _crawler()
        hits: dict[str, int] = {}
        _install_fake_fetch(crawler, hits)
        worker = _worker(crawler, repo, store)
        stats = worker.run(["https://worker.test/a"])
        assert stats.added == 3
        assert repo.count() == 3
        counts = store.counts()
        assert counts.get("done") == 3
        assert counts.get("queued", 0) == 0
        checkpoint = worker.last_checkpoint
        assert checkpoint is not None
        assert checkpoint["phase"] == "done"
        assert checkpoint["pages_attempted"] == 3
    finally:
        store.close()
        repo.close()


def test_worker_checkpoint_continuity_no_refetch() -> None:
    repo = DocumentRepository(":memory:")
    store = SqliteFrontierStore(":memory:")
    try:
        crawler = _crawler()
        hits: dict[str, int] = {}
        _install_fake_fetch(crawler, hits)
        first = _worker(crawler, repo, store)
        first.run(["https://worker.test/a"])
        assert dict(hits) == {u: 1 for u in _PAGES}
        # Same crawl_id resumes: done rows are never dequeued, so the
        # second worker writes nothing new and fetches nothing again.
        second = _worker(_crawler(), repo, store)
        stats = second.run(["https://worker.test/a"])
        assert stats.added == 0
        assert stats.skipped_unchanged == 0  # no docs crawled at all
        assert dict(hits) == {u: 1 for u in _PAGES}
        assert second.last_checkpoint is not None
        assert second.last_checkpoint["phase"] == "done"
    finally:
        store.close()
        repo.close()


def test_worker_request_stop_pauses_checkpoint() -> None:
    repo = DocumentRepository(":memory:")
    store = SqliteFrontierStore(":memory:")
    try:
        crawler = _crawler()
        hits: dict[str, int] = {}
        _install_fake_fetch(crawler, hits)
        worker = _worker(crawler, repo, store)
        worker.request_stop()
        assert worker.should_stop() is True
        worker.run(["https://worker.test/a"])
        assert worker.last_checkpoint is not None
        assert worker.last_checkpoint["phase"] == "paused"
        assert hits == {}
    finally:
        store.close()
        repo.close()


def test_worker_global_pause_flag_via_store_settings() -> None:
    repo = DocumentRepository(":memory:")
    store = SqliteFrontierStore(":memory:")
    try:
        crawler = _crawler()
        hits: dict[str, int] = {}
        _install_fake_fetch(crawler, hits)
        worker = _worker(crawler, repo, store)
        store.save_settings({"paused": True})
        assert worker.should_stop() is True
        worker.run(["https://worker.test/a"])
        assert worker.last_checkpoint is not None
        assert worker.last_checkpoint["phase"] == "paused"
        assert hits == {}
        # Clearing the flag lets the next run proceed.
        store.save_settings({"paused": False})
        assert worker.should_stop() is False
    finally:
        store.close()
        repo.close()


def test_worker_enqueue_new_urls_dedups() -> None:
    repo = DocumentRepository(":memory:")
    store = SqliteFrontierStore(":memory:")
    try:
        worker = _worker(_crawler(), repo, store)
        assert worker.enqueue_new_urls(["https://worker.test/n1"]) == 1
        assert worker.enqueue_new_urls(["https://worker.test/n1"]) == 0
        assert worker.enqueue_new_urls(["  ", ""]) == 0
        assert worker.enqueue_new_urls(["https://worker.test/n1#frag"]) == 0
        counts = store.counts()
        assert counts.get("queued") == 1
    finally:
        store.close()
        repo.close()


def test_worker_heartbeat_registers_liveness_in_store() -> None:
    repo = DocumentRepository(":memory:")
    store = SqliteFrontierStore(":memory:")
    try:
        crawler = _crawler()
        hits: dict[str, int] = {}
        _install_fake_fetch(crawler, hits)
        worker = _worker(crawler, repo, store)
        worker.run(["https://worker.test/a"])
        workers = store.list_workers("test-crawl")
        assert [w["worker_id"] for w in workers] == ["test-worker"]
        assert workers[0]["claimed"] == 3
    finally:
        store.close()
        repo.close()


def test_worker_releases_owned_rows_on_shutdown() -> None:
    repo = DocumentRepository(":memory:")
    store = SqliteFrontierStore(":memory:")
    try:
        crawler = _crawler()
        hits: dict[str, int] = {}
        _install_fake_fetch(crawler, hits)
        worker = _worker(crawler, repo, store)
        worker.run(["https://worker.test/a"])
        # Graceful exit requeues nothing owned here (crawl marks done),
        # but the path is exercised and leaves no active rows behind.
        assert store.counts().get("active", 0) == 0
        assert store.owned("test-worker") == []
    finally:
        store.close()
        repo.close()


def test_worker_respects_robots_txt() -> None:
    repo = DocumentRepository(":memory:")
    store = SqliteFrontierStore(":memory:")
    try:
        crawler = _crawler(respect_robots=True)
        hits: dict[str, int] = {}
        _install_fake_fetch(crawler, hits)
        parser = urllib.robotparser.RobotFileParser()
        parser.parse(["User-agent: *", "Disallow: /b"])
        crawler._robots_for = lambda url: parser  # type: ignore[method-assign]
        crawler.respect_robots = True
        worker = _worker(crawler, repo, store)
        stats = worker.run(["https://worker.test/a"])
        assert "https://worker.test/b" not in hits
        assert hits.get("https://worker.test/c") == 1
        assert crawler.last_stats.skipped_robots >= 1
        assert stats.added == 2
    finally:
        store.close()
        repo.close()


def test_new_worker_id_prefers_explicit_and_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert new_worker_id("explicit") == "explicit"
    monkeypatch.setenv("OPENLENS_WORKER_ID", "from-env")
    assert new_worker_id() == "from-env"
    monkeypatch.delenv("OPENLENS_WORKER_ID")
    generated = new_worker_id()
    assert generated and generated != new_worker_id()


def test_cli_crawl_worker_parses() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "crawl-worker",
            "https://worker.test/a",
            "--crawl-id",
            "c1",
            "--worker-id",
            "w1",
            "--chunk-size",
            "7",
        ]
    )
    assert args.seeds == ["https://worker.test/a"]
    assert args.crawl_id == "c1"
    assert args.worker_id == "w1"
    assert args.chunk_size == 7


def test_cli_crawl_worker_rejects_bad_chunk_size(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["crawl-worker", "--chunk-size", "0"]) == cli.FAILURE_EXIT
    assert "chunk-size" in capsys.readouterr().err


def test_cli_crawl_worker_runs_offline(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from openlens.indexing.indexer import IndexStats

    def fake_run(seeds: list[str], config: dict[str, Any], **kwargs: Any) -> Any:
        assert seeds == ["https://worker.test/a"]
        # The worker boots from the effective config (file + DB-persisted
        # admin docs), not the raw file config.
        assert config == {"indexing": {}}
        assert kwargs.get("crawl_id") == "c1"
        assert kwargs.get("worker_id") == "w1"
        stats = IndexStats()
        stats.added = 2
        return stats

    monkeypatch.setattr("openlens.indexing.worker.run_standalone_worker", fake_run)
    monkeypatch.setattr(cli, "load_effective_config", lambda *a, **k: {"indexing": {}})
    code = cli.main(
        [
            "crawl-worker",
            "https://worker.test/a",
            "--crawl-id",
            "c1",
            "--worker-id",
            "w1",
        ]
    )
    assert code == 0
    assert "crawl-worker done: added=2" in capsys.readouterr().out


def test_build_worker_sqlite_file_repo_creates_parent(tmp_path) -> None:
    """File-backed sqlite repo works when the parent dir is missing."""
    from openlens.indexing.worker import _build_worker

    db_path = str(tmp_path / "missing-parent" / "crawl.db")
    config: dict[str, Any] = {
        "storage": {"backend": "sqlite"},
        "providers": {"custom_index": {"enabled": True, "db_path": db_path}},
        "indexing": {},
    }
    worker, repository, frontier_store, _ = _build_worker(config)
    try:
        from openlens.storage.repository import DocumentRepository

        assert isinstance(repository, DocumentRepository)
        assert Path(db_path).is_file()
    finally:
        with contextlib.suppress(Exception):
            frontier_store.close()
        with contextlib.suppress(Exception):
            repository.close()


def test_build_worker_postgres_repo(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Postgres backend crawls into the searched PG index (not sqlite)."""
    from openlens.indexing.worker import _build_worker

    seen: dict[str, Any] = {}

    class _FakePGRepo:
        @classmethod
        def from_config(cls, section: Any) -> "_FakePGRepo":
            seen["section"] = dict(section)
            return cls()

        def close(self) -> None:
            return None

    class _FakeStore:
        def close(self) -> None:
            return None

    monkeypatch.setattr(
        "openlens.storage.postgres_backend.PostgresDocumentStore", _FakePGRepo
    )
    monkeypatch.setattr(
        "openlens.storage.frontier.open_frontier_store",
        lambda config: _FakeStore(),
    )
    pg_section = {"host": "postgres", "dbname": "openlens"}
    config: dict[str, Any] = {
        "storage": {"backend": "postgres", "postgres": pg_section},
        "providers": {"custom_index": {"enabled": True}},
        "indexing": {},
    }
    worker, repository, frontier_store, _ = _build_worker(config)
    try:
        assert isinstance(repository, _FakePGRepo)
        assert seen["section"] == pg_section
    finally:
        repository.close()
        frontier_store.close()


def test_extract_visible_text_strips_markup() -> None:
    html = (
        "<html><head><title>T</title><style>.a{color:red}</style>"
        "<script>var x = 1;</script></head>"
        "<body><h1>Head &amp; shoulders</h1><p>  spaced   out </p></body></html>"
    )
    assert extract_visible_text(html) == "T Head & shoulders spaced out"


def test_extract_visible_text_never_raises() -> None:
    assert extract_visible_text("") == ""
    assert extract_visible_text(None) == ""  # type: ignore[arg-type]
    assert extract_visible_text("<p>unclosed") == "unclosed"


def test_to_document_stores_visible_text_not_markup() -> None:
    crawler = PoliteCrawler()
    body = (
        "<html><head><title>Page title</title>"
        "<script>var x = 1;</script></head>"
        "<body><p>Visible prose.</p></body></html>"
    ).encode()
    doc = crawler._to_document(
        _FetchedPage(
            status=200,
            url="https://x.test/a",
            content_type="text/html",
            body=body,
            headers={},
        )
    )
    assert doc.title == "Page title"
    assert "<" not in doc.content
    assert "var x" not in doc.content
    assert "Visible prose." in doc.content


def test_to_document_keeps_plain_text_verbatim() -> None:
    crawler = PoliteCrawler()
    raw = b"a < b and c > d stay as-is"
    doc = crawler._to_document(
        _FetchedPage(
            status=200,
            url="https://x.test/b",
            content_type="text/plain",
            body=raw,
            headers={},
        )
    )
    assert doc.content == raw.decode()


def test_to_document_falls_back_to_raw_without_visible_text() -> None:
    crawler = PoliteCrawler()
    raw = b"<html><head><script>var x = 1;</script></head><body></body></html>"
    doc = crawler._to_document(
        _FetchedPage(
            status=200,
            url="https://x.test/c",
            content_type="text/html",
            body=raw,
            headers={},
        )
    )
    assert doc.content == raw.decode()

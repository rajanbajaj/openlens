"""Backend tests for the ``skip_domains`` frontier setting.

Covers the fixed skip contract: schema accepts/rejects, bulk-skip on
both backends (subdomain match + non-matching control), claim-time skip
never fetches, refresh applies list changes, malformed entries ignored.

SQLite runs against a real store; Postgres has no live server under
test, so its bulk-skip is verified through a fake pool connection that
captures the generated SQL (same harness style as
``test_postgres_pool_hygiene.py``).
"""

from __future__ import annotations

import queue
import threading
from typing import Any

import pytest

from openlens.api.schemas import FrontierSettingsSchema
from openlens.indexing.crawler import PoliteCrawler, _FetchedPage
from openlens.indexing.worker import CrawlerWorker, _live_crawl_limits
from openlens.storage import frontier as frontier_mod
from openlens.storage.frontier import SqliteFrontierStore
from openlens.storage.repository import DocumentRepository


# -- schema ---------------------------------------------------------------


def test_schema_accepts_skip_domains() -> None:
    body = FrontierSettingsSchema(skip_domains=["evil.test", "Example.COM"])
    assert body.skip_domains == ["evil.test", "Example.COM"]
    assert FrontierSettingsSchema().skip_domains == []


def test_schema_rejects_bad_skip_domains() -> None:
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        FrontierSettingsSchema(skip_domains=[""])  # empty entry
    with pytest.raises(pydantic.ValidationError):
        FrontierSettingsSchema(skip_domains=["x" * 254])  # oversize entry
    with pytest.raises(pydantic.ValidationError):
        FrontierSettingsSchema(skip_domains=["ok.test", 123])  # non-string
    with pytest.raises(pydantic.ValidationError):
        FrontierSettingsSchema(skip_domains="evil.test")  # type: ignore[arg-type]
    with pytest.raises(pydantic.ValidationError):
        FrontierSettingsSchema(skip_domains=[], unknown_key=1)  # type: ignore[call-arg]


# -- worker validation -----------------------------------------------------


def test_live_limits_normalizes_skip_domains() -> None:
    live = _live_crawl_limits({"skip_domains": ["  EVIL.test ", "*.sub.test"]})
    assert live["skip_domains"] == ["evil.test", "sub.test"]
    live = _live_crawl_limits({"skip_domains": ["https://Path.test/some/page?x=1"]})
    assert live["skip_domains"] == ["path.test"]


def test_live_limits_skip_domains_missing_or_invalid_is_none() -> None:
    assert _live_crawl_limits({})["skip_domains"] is None
    assert _live_crawl_limits({"skip_domains": None})["skip_domains"] is None
    assert _live_crawl_limits({"skip_domains": "evil.test"})["skip_domains"] is None
    assert _live_crawl_limits({"skip_domains": 123})["skip_domains"] is None


def test_live_limits_malformed_entries_ignored_not_fatal() -> None:
    live = _live_crawl_limits(
        {"skip_domains": ["", "   ", 123, None, "x" * 300, "not a domain!!", "ok.test"]}
    )
    assert live["skip_domains"] == ["ok.test"]
    live = _live_crawl_limits({"skip_domains": ["", 123]})
    assert live["skip_domains"] == []


# -- SQLite bulk-skip -------------------------------------------------------


def _sqlite_store() -> SqliteFrontierStore:
    return SqliteFrontierStore(":memory:")


def test_sqlite_bulk_skip_matches_subdomains_not_controls() -> None:
    store = _sqlite_store()
    try:
        queued = [
            "https://evil.test/a",
            "https://sub.evil.test/b",
            "https://deep.sub.evil.test/c",
            "https://evil.test:8080/port",
            "https://evil.test?x=1",
            "https://good.test/keep",
            "https://not-evil.test/keep",
            "https://evil.test.evil.com/keep",
            "https://evil.testx/keep",
        ]
        for url in queued:
            assert store.enqueue(url, 0) is True
        moved = store.skip_queued_domains(["EVIL.test"])
        assert moved == 5
        for url in queued[:5]:
            assert store.status_of(url) == "skipped", url
        for url in queued[5:]:
            assert store.status_of(url) == "queued", url
    finally:
        store.close()


def test_sqlite_bulk_skip_stamps_reason() -> None:
    store = SqliteFrontierStore(":memory:")
    try:
        assert store.enqueue("https://evil.test/a", 0) is True
        assert store.skip_queued_domains(["evil.test"]) == 1
    finally:
        with store._lock:
            rows = store._conn.execute(
                "SELECT last_error FROM frontier_queue WHERE url = ?",
                ("https://evil.test/a",),
            ).fetchall()
        store.close()
    assert rows and (rows[0]["last_error"] or "").startswith("skipped:")
    assert "evil.test" in rows[0]["last_error"]


def test_sqlite_bulk_skip_empty_or_unknown_is_noop() -> None:
    store = _sqlite_store()
    try:
        assert store.enqueue("https://good.test/a", 0) is True
        assert store.skip_queued_domains([]) == 0
        assert store.skip_queued_domains(["nosuch.test"]) == 0
        assert store.skip_queued_domains(None) == 0
        assert store.skip_queued_domains("evil.test") == 0  # bare str: not a list
        assert store.status_of("https://good.test/a") == "queued"
    finally:
        store.close()


def test_sqlite_bulk_skip_leaves_active_and_done_rows() -> None:
    store = _sqlite_store()
    try:
        assert store.enqueue("https://evil.test/a", 0) is True
        assert store.enqueue("https://evil.test/b", 0) is True
        claimed = store.claim("w1")
        assert claimed is not None and claimed.url == "https://evil.test/a"
        assert store.skip_queued_domains(["evil.test"]) == 1
        assert store.status_of("https://evil.test/a") == "active"
        assert store.status_of("https://evil.test/b") == "skipped"
    finally:
        store.close()


# -- Postgres bulk-skip (fake-pool SQL capture) ------------------------------


class _FakeCursor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self.rowcount = 0

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.executed.append((sql, params))

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *args: Any) -> None:
        return None


class _FakePGConn:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> _FakeCursor:
        return self._cursor

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        return None


def _postgres_store(cursor: _FakeCursor) -> frontier_mod.PostgresFrontierStore:
    pool: queue.Queue[object] = queue.Queue()
    pool.put(_FakePGConn(cursor))
    store = object.__new__(frontier_mod.PostgresFrontierStore)
    store._pool = pool  # type: ignore[attr-defined]
    store._state_lock = threading.Lock()  # type: ignore[attr-defined]
    store._closed = False  # type: ignore[attr-defined]
    store._checkout_timeout = 5.0  # type: ignore[attr-defined]
    store._schema = "public"  # type: ignore[attr-defined]
    return store


def test_postgres_bulk_skip_sql_matches_scheme_and_subdomains() -> None:
    cursor = _FakeCursor()
    cursor.rowcount = 4
    store = _postgres_store(cursor)
    assert store.skip_queued_domains(["EVIL.test"]) == 4
    assert len(cursor.executed) == 1
    sql, params = cursor.executed[0]
    assert "SET status = 'skipped'" in sql
    assert "status = 'queued'" in sql
    assert "LIKE" in sql and "ESCAPE" in sql
    assert params[0] == "skipped: domain evil.test"
    patterns = list(params[1:])
    assert "%://evil.test" in patterns  # exact host, scheme-bound
    assert "%://evil.test/%" in patterns
    assert "%://%.evil.test" in patterns  # subdomain, scheme-bound
    assert "%://%.evil.test/%" in patterns


def test_postgres_bulk_skip_sums_per_domain_and_skips_empty() -> None:
    cursor = _FakeCursor()
    cursor.rowcount = 2
    store = _postgres_store(cursor)
    assert store.skip_queued_domains(["a.test", "b.test"]) == 4
    assert len(cursor.executed) == 2
    cursor2 = _FakeCursor()
    store2 = _postgres_store(cursor2)
    assert store2.skip_queued_domains([]) == 0
    assert store2.skip_queued_domains(None) == 0
    assert cursor2.executed == []


def test_like_patterns_escape_metacharacters() -> None:
    patterns = frontier_mod._skip_domain_like_patterns("a%b_c\\d.test")
    assert all("\\%" in p and "\\_" in p and "\\\\" in p for p in patterns)
    assert patterns[0].startswith("%://")


# -- worker refresh + claim-time guard ----------------------------------------


def _crawler() -> PoliteCrawler:
    crawler = PoliteCrawler()
    crawler.configure(
        {
            "timeout_s": 5,
            "crawl_delay_s": 0,
            "max_pages": 10,
            "respect_robots": False,
        }
    )
    return crawler


_PAGES: dict[str, list[str]] = {
    "https://good.test/a": ["https://good.test/b"],
    "https://good.test/b": [],
}


def _install_fake_fetch(crawler: PoliteCrawler, hits: dict[str, int]) -> None:
    def fake_fetch(url: str) -> _FetchedPage | None:
        hits[url] = hits.get(url, 0) + 1
        if url not in _PAGES:
            return _FetchedPage(
                status=404, url=url, content_type="text/plain", body=b""
            )
        links = "".join(f'<a href="{link}">x</a>' for link in _PAGES[url])
        prose = "Informative crawlable prose about the topic. " * 20
        body = (
            f"<html><head><title>Guide for {url}</title></head>"
            f"<body><p>{prose}</p>{links}</body></html>"
        ).encode()
        return _FetchedPage(
            status=200, url=url, content_type="text/html", body=body, headers={}
        )

    crawler._fetch_bounded = fake_fetch  # type: ignore[method-assign]
    crawler.respect_robots = False


def _worker(
    crawler: PoliteCrawler,
    repo: DocumentRepository,
    store: SqliteFrontierStore,
) -> CrawlerWorker:
    return CrawlerWorker(
        crawler=crawler,
        repository=repo,
        frontier_store=store,
        worker_id="test-worker",
        crawl_id="test-crawl",
        indexing_config={"timeout_s": 5, "crawl_delay_s": 0, "max_pages": 10},
        heartbeat_interval_s=3600.0,
    )


def test_claim_time_skip_never_fetches() -> None:
    repo = DocumentRepository(":memory:")
    store = _sqlite_store()
    try:
        # Already-queued row from a listed domain (bulk-skip path) plus a
        # seed from a listed domain (claim-time guard path).
        assert store.enqueue("https://evil.test/queued", 0) is True
        store.save_settings({"skip_domains": ["evil.test"]})
        crawler = _crawler()
        hits: dict[str, int] = {}
        _install_fake_fetch(crawler, hits)
        worker = _worker(crawler, repo, store)
        stats = worker.run(["https://good.test/a", "https://evil.test/seed"])
        assert "https://evil.test/queued" not in hits
        assert "https://evil.test/seed" not in hits
        assert hits.get("https://good.test/a") == 1
        assert store.status_of("https://evil.test/queued") == "skipped"
        assert store.status_of("https://evil.test/seed") == "skipped"
        assert stats.added == 2
        assert worker.last_skip_bulk_count == 1
    finally:
        store.close()
        repo.close()


def test_new_enqueues_from_listed_domains_are_stored_not_dropped() -> None:
    store = _sqlite_store()
    try:
        store.save_settings({"skip_domains": ["evil.test"]})
        crawler = _crawler()
        repo = DocumentRepository(":memory:")
        try:
            worker = _worker(crawler, repo, store)
            worker.refresh_limits()
            # Still stored (auditable counts), settled as skipped instead.
            assert store.enqueue("https://evil.test/new", 0) is True
            worker.refresh_limits()
            counts = store.counts()
            assert counts.get("queued", 0) == 0
            assert counts.get("skipped", 0) == 1
            assert counts.get("total", 0) == 1
        finally:
            repo.close()
    finally:
        store.close()


def test_refresh_applies_list_changes_add_then_remove() -> None:
    repo = DocumentRepository(":memory:")
    store = _sqlite_store()
    try:
        crawler = _crawler()
        worker = _worker(crawler, repo, store)
        store.save_settings({"skip_domains": ["evil.test"]})
        worker.refresh_limits()
        assert worker.skip_domains == ["evil.test"]
        assert store.enqueue("https://evil.test/one", 0) is True
        worker.refresh_limits()
        assert store.status_of("https://evil.test/one") == "skipped"
        # Removing the domain stops enforcement for later rows.
        store.save_settings({"skip_domains": []})
        worker.refresh_limits()
        assert worker.skip_domains == []
        assert store.enqueue("https://evil.test/two", 0) is True
        worker.refresh_limits()
        assert store.status_of("https://evil.test/two") == "queued"
        assert worker.last_skip_bulk_count == 0
    finally:
        store.close()
        repo.close()


def test_refresh_malformed_skip_entries_ignored() -> None:
    repo = DocumentRepository(":memory:")
    store = _sqlite_store()
    try:
        worker = _worker(_crawler(), repo, store)
        store.save_settings(
            {"skip_domains": ["  EVIL.test ", "", 123, None, "not a domain!!"]}
        )
        worker.refresh_limits()  # must not raise
        assert worker.skip_domains == ["evil.test"]
        assert store.enqueue("https://sub.evil.test/x", 0) is True
        worker.refresh_limits()
        assert store.status_of("https://sub.evil.test/x") == "skipped"
    finally:
        store.close()
        repo.close()


def test_guarded_dequeue_is_bounded() -> None:
    store = _sqlite_store()
    try:
        worker = _worker(_crawler(), DocumentRepository(":memory:"), store)
        try:
            for i in range(150):
                assert store.enqueue(f"https://evil.test/p{i}", 0) is True
            assert store.enqueue("https://good.test/keep", 0) is True
            worker.skip_domains = ["evil.test"]
            guarded = worker.guarded_store()
            # One guarded take skips at most MAX_SKIPS_PER_CLAIM + 1 rows,
            # then yields (no starvation on an all-listed queue).
            assert guarded.dequeue() is None
            counts = store.counts()
            assert counts.get("skipped", 0) == 101
            assert counts.get("queued", 0) == 50
            # The next take keeps skipping and reaches the good row.
            entry = guarded.dequeue()
            assert entry is not None and entry.url == "https://good.test/keep"
        finally:
            worker.repository.close()
    finally:
        store.close()

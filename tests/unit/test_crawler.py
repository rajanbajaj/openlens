"""Offline crawler tests: local stub HTTP server only, no network."""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from openlens.core.interfaces import ConfigError
from openlens.indexing.crawler import PoliteCrawler


def _loopback_server(handler: type[BaseHTTPRequestHandler]) -> ThreadingHTTPServer:
    """Bind a stub server, skipping when the sandbox denies loopback bind."""
    try:
        return ThreadingHTTPServer(("127.0.0.1", 0), handler)
    except OSError as exc:
        pytest.skip(f"loopback bind unavailable in this environment: {exc}")


BIG_BODY = b"x" * 200_000

#: Quality-gate filler: stub pages carry real prose so the content-validity
#: floor (200 visible chars) keeps them. Intent of each test (robots, delay,
#: caps, domain scope) is orthogonal to content length.
PROSE = (
    b"<p>Informative crawlable prose about the topic. " * 5
    + b"Informative crawlable prose about the topic.</p>"
)


class _StubHandler(BaseHTTPRequestHandler):
    hits: dict[str, int] = {}
    robots_body: bytes = b"User-agent: *\nDisallow: /private\n"

    def log_message(self, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        type(self).hits[path] = type(self).hits.get(path, 0) + 1
        if path == "/robots.txt":
            self._send(200, "text/plain", type(self).robots_body)
        elif path == "/page1":
            self._send(
                200,
                "text/html; charset=utf-8",
                b"<html><head><title>Page One</title></head><body>"
                + PROSE
                + b'<a href="/page2">two</a> '
                b'<a href="/private">priv</a></body></html>',
            )
        elif path == "/page2":
            self._send(
                200,
                "text/html; charset=utf-8",
                b"<html><head><title>Page Two</title></head><body>"
                + PROSE
                + b"hello</body></html>",
            )
        elif path == "/private":
            self._send(200, "text/html", b"<html><body>secret</body></html>")
        elif path == "/big":
            self._send(200, "text/html", BIG_BODY)
        elif path == "/binary":
            self._send(200, "application/octet-stream", b"\x00\x01\x02")
        else:
            self._send(404, "text/plain", b"missing")

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def stub_base() -> str:
    _StubHandler.hits = {}
    _StubHandler.robots_body = b"User-agent: *\nDisallow: /private\n"
    server = _loopback_server(_StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _crawler(**overrides: object) -> PoliteCrawler:
    crawler = PoliteCrawler()
    crawler.configure(dict(overrides))
    return crawler


def test_robots_txt_respected(stub_base: str) -> None:
    crawler = _crawler()
    docs = crawler.crawl([f"{stub_base}/page1"])
    urls = [doc.url or "" for doc in docs]
    assert not any(url.endswith("/private") for url in urls)
    assert _StubHandler.hits.get("/private", 0) == 0
    assert any(url.endswith("/page1") for url in urls)
    assert any(url.endswith("/page2") for url in urls)


def test_robots_fetched_once_per_host(stub_base: str) -> None:
    crawler = _crawler()
    crawler.crawl([f"{stub_base}/page1", f"{stub_base}/page2"])
    assert _StubHandler.hits.get("/robots.txt", 0) == 1


def test_crawl_delay_honored(stub_base: str) -> None:
    crawler = _crawler(crawl_delay_s=0.5)
    start = time.monotonic()
    docs = crawler.crawl([f"{stub_base}/page1", f"{stub_base}/page2"])
    elapsed = time.monotonic() - start
    assert len(docs) >= 2
    assert elapsed >= 0.5


def test_max_pages_cap_enforced(stub_base: str) -> None:
    crawler = _crawler(max_pages=1)
    docs = crawler.crawl([f"{stub_base}/page1", f"{stub_base}/page2"])
    assert len(docs) == 1


def test_crawl_reports_page_outcomes(stub_base: str) -> None:
    """Each terminal page outcome emits (url, outcome, reason) once."""
    seen: list[tuple[str, str, str | None]] = []
    crawler = _crawler(deny_domains=["blocked.example"], max_pages=10)
    crawler.crawl(
        [f"{stub_base}/page1", f"{stub_base}/nope", "https://blocked.example/x"],
        on_outcome=lambda url, outcome, reason: seen.append((url, outcome, reason)),
    )
    by_url = {url: (outcome, reason) for url, outcome, reason in seen}
    assert by_url[f"{stub_base}/page1"][0] == "fetched"
    assert by_url[f"{stub_base}/page2"][0] == "fetched"
    assert by_url[f"{stub_base}/nope"] == ("failed", "http 404")
    assert by_url["https://blocked.example/x"] == ("skipped", "denied by deny_domains")
    # Popped terminal pages only: the robots-gated /private link is
    # never queued, so it never reports.
    assert not any(url.endswith("/private") for url, _, _ in seen)


def _failed_page(url: str, status: int) -> object:
    from openlens.indexing.crawler import _FetchedPage

    return _FetchedPage(
        status=status, url=url, content_type="text/plain", body=b"nope", headers={}
    )


def test_http_4xx_terminal_no_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """4xx rejections fail once: retrying a decided rejection burns budget.

    The in-memory ``seen`` set fetches each URL at most once per
    ``crawl()`` call, so retries surface across calls — drive several
    and assert the 404 is never refetched.
    """
    from openlens.storage.frontier import SqliteFrontierStore

    hits: list[str] = []

    def _fetch(url: str) -> object:
        hits.append(url)
        return _failed_page(url, 404)

    store = SqliteFrontierStore(":memory:")
    try:
        events: list[tuple[str, str, str | None]] = []
        crawler = _crawler(max_pages=10, respect_robots=False)
        monkeypatch.setattr(crawler, "_fetch_bounded", _fetch)
        for _ in range(5):
            crawler.crawl(
                ["https://example.com/nope"],
                frontier_store=store,
                on_outcome=lambda url, outcome, reason: events.append(
                    (url, outcome, reason)
                ),
            )
        assert hits == ["https://example.com/nope"]
        assert ("https://example.com/nope", "failed", "http 404") in events
        failed = {entry.url: entry for entry in store.failed(limit=10)}
        assert failed["https://example.com/nope"].attempts == 1
    finally:
        store.close()


def test_http_transient_retried_then_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """429/5xx keep the old retry budget (max_attempts_per_url fetches)."""
    from openlens.storage.frontier import SqliteFrontierStore

    hits: list[str] = []
    statuses = {
        "https://example.com/throttled": 429,
        "https://example.com/broken": 500,
    }

    def _fetch(url: str) -> object:
        hits.append(url)
        return _failed_page(url, statuses[url])

    store = SqliteFrontierStore(":memory:")
    try:
        events: list[tuple[str, str, str | None]] = []
        crawler = _crawler(max_pages=20, max_attempts_per_url=3, respect_robots=False)
        monkeypatch.setattr(crawler, "_fetch_bounded", _fetch)
        for _ in range(5):
            crawler.crawl(
                list(statuses),
                frontier_store=store,
                on_outcome=lambda url, outcome, reason: events.append(
                    (url, outcome, reason)
                ),
            )
        assert sorted(hits) == sorted(list(statuses) * 3)
        by_url = {url: (outcome, reason) for url, outcome, reason in events}
        assert by_url["https://example.com/throttled"] == ("failed", "http 429")
        assert by_url["https://example.com/broken"] == ("failed", "http 500")
    finally:
        store.close()


def test_429_hands_off_to_backoff_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 429 with a backoff sink parks the row and emits once, then moves on."""
    from openlens.storage.frontier import SqliteFrontierStore

    hits: list[str] = []
    handed: list[str] = []

    def _fetch(url: str) -> object:
        hits.append(url)
        return _failed_page(url, 429)

    store = SqliteFrontierStore(":memory:")
    try:
        crawler = _crawler(max_pages=20, max_attempts_per_url=3, respect_robots=False)
        monkeypatch.setattr(crawler, "_fetch_bounded", _fetch)
        for _ in range(3):
            crawler.crawl(
                ["https://example.com/throttled"],
                frontier_store=store,
                on_backoff=handed.append,
            )
        # One fetch, one handoff; the parked row never re-enters the main drain.
        assert hits == ["https://example.com/throttled"]
        assert handed == ["https://example.com/throttled"]
        counts = store.counts()
        assert counts.get("backoff", 0) == 1
        assert counts.get("failed", 0) == 0
        backed = store.dequeue_backoff()
        assert backed is not None and backed.attempts == 1
        assert backed.last_error == "http 429"
    finally:
        store.close()


def test_timeout_hands_off_to_backoff_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fetch timeout with a backoff sink parks instead of requeueing."""
    from openlens.indexing.crawler import CrawlError
    from openlens.storage.frontier import SqliteFrontierStore

    hits: list[str] = []
    handed: list[str] = []

    def _fetch(url: str) -> object:
        hits.append(url)
        raise CrawlError("boom")

    store = SqliteFrontierStore(":memory:")
    try:
        crawler = _crawler(max_pages=20, max_attempts_per_url=3, respect_robots=False)
        monkeypatch.setattr(crawler, "_fetch_bounded", _fetch)
        for _ in range(3):
            crawler.crawl(
                ["https://example.com/slow"],
                frontier_store=store,
                on_backoff=handed.append,
            )
        assert hits == ["https://example.com/slow"]
        assert handed == ["https://example.com/slow"]
        assert store.counts().get("backoff", 0) == 1
        backed = store.dequeue_backoff()
        assert backed is not None and backed.last_error == "fetch timeout"
    finally:
        store.close()


def test_backoff_exhausted_budget_terminates_as_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A handoff never fires once the retry budget is spent."""
    from openlens.indexing.crawler import CrawlError
    from openlens.storage.frontier import SqliteFrontierStore

    handed: list[str] = []

    def _fetch(url: str) -> object:
        raise CrawlError("boom")

    store = SqliteFrontierStore(":memory:")
    try:
        crawler = _crawler(max_pages=20, max_attempts_per_url=1, respect_robots=False)
        monkeypatch.setattr(crawler, "_fetch_bounded", _fetch)
        crawler.crawl(
            ["https://example.com/slow"],
            frontier_store=store,
            on_backoff=handed.append,
        )
        assert handed == []
        assert store.counts().get("failed", 0) == 1
        assert store.counts().get("backoff", 0) == 0
    finally:
        store.close()


def test_unfetchable_urls_fail_without_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """mailto:/hostless URLs never reach the network; one terminal failure."""
    from openlens.indexing.crawler import _is_fetchable_url
    from openlens.storage.frontier import SqliteFrontierStore

    assert not _is_fetchable_url("mailto:info@example.com")
    assert not _is_fetchable_url("http:/no-such-host-xyz/path")
    assert not _is_fetchable_url(
        "https://web.archive.org/web/20211221001342/mailto:info@example.com"
    )
    assert _is_fetchable_url("https://example.com/page1")
    assert _is_fetchable_url("https://web.archive.org/web/2021/https://example.com/")

    def _must_not_fetch(url: str) -> object:
        raise AssertionError(f"unfetchable URL reached fetch: {url}")

    store = SqliteFrontierStore(":memory:")
    try:
        seen: list[tuple[str, str, str | None]] = []
        crawler = _crawler(max_pages=10, respect_robots=False)
        monkeypatch.setattr(crawler, "_fetch_bounded", _must_not_fetch)
        crawler.crawl(
            ["mailto:info@example.com", "http:/no-such-host-xyz/path"],
            frontier_store=store,
            on_outcome=lambda url, outcome, reason: seen.append((url, outcome, reason)),
        )
        by_url = {url: (outcome, reason) for url, outcome, reason in seen}
        assert by_url["mailto:info@example.com"] == ("failed", "invalid url")
        assert by_url["http:/no-such-host-xyz/path"] == ("failed", "invalid url")
    finally:
        store.close()


def _ok_page(url: str) -> object:
    from openlens.indexing.crawler import _FetchedPage

    body = (
        b"<html><head><title>Real page</title></head><body>"
        + b"<p>Informative crawlable prose about the topic. </p>" * 12
        + b"</body></html>"
    )
    return _FetchedPage(
        status=200, url=url, content_type="text/html", body=body, headers={}
    )


def test_domain_page_budget_caps_trap_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A budgeted domain stops fetching after max_pages; the rest skip."""
    from openlens.storage.frontier import SqliteFrontierStore

    hits: list[str] = []

    def _fetch(url: str) -> object:
        hits.append(url)
        return _ok_page(url)

    store = SqliteFrontierStore(":memory:")
    try:
        events: list[tuple[str, str, str | None]] = []
        crawler = _crawler(
            max_pages=10,
            respect_robots=False,
            domain_policies={"example.com": {"max_pages": 2}},
        )
        monkeypatch.setattr(crawler, "_fetch_bounded", _fetch)
        seeds = [f"https://example.com/p{i}" for i in range(5)]
        crawler.crawl(
            seeds,
            frontier_store=store,
            on_outcome=lambda url, outcome, reason: events.append(
                (url, outcome, reason)
            ),
        )
        assert hits == seeds[:2]
        by_url = {url: (outcome, reason) for url, outcome, reason in events}
        assert by_url[seeds[0]] == ("fetched", None)
        assert by_url[seeds[2]] == ("skipped", "domain page budget exceeded")
    finally:
        store.close()


def test_domain_policy_max_pages_validated() -> None:
    with pytest.raises(ConfigError):
        _crawler(domain_policies={"example.com": {"max_pages": 0}})
    with pytest.raises(ConfigError):
        _crawler(domain_policies={"example.com": {"max_pages": "many"}})
    with pytest.raises(ConfigError):
        _crawler(domain_policies={"example.com": {"nope": 1}})


def test_crawl_without_sink_stays_silent(stub_base: str) -> None:
    """No on_outcome means zero reporting overhead and no behavior change."""
    crawler = _crawler(max_pages=10)
    docs = crawler.crawl([f"{stub_base}/page1"])
    assert {doc.url for doc in docs} >= {f"{stub_base}/page1"}


def test_run_index_forwards_outcome_sink() -> None:
    """run_index threads the outcome callback through to crawl."""
    from openlens.indexing.indexer import run_index
    from openlens.storage.frontier import SqliteFrontierStore
    from openlens.storage.repository import DocumentRepository

    seen: dict[str, object] = {}

    class _RecCrawler:
        def crawl(self, seeds: list[str], **kwargs: object) -> list:
            seen.update(kwargs)
            return []

    store = SqliteFrontierStore(":memory:")
    try:
        marker = object()
        run_index(
            [],
            DocumentRepository(),
            {},
            crawler=_RecCrawler(),
            frontier_store=store,
            on_outcome=marker,
        )
        assert seen.get("on_outcome") is marker
    finally:
        store.close()


def test_deny_domains_enforced(stub_base: str) -> None:
    crawler = _crawler(deny_domains=["127.0.0.1"])
    assert crawler.crawl([f"{stub_base}/page1"]) == []
    assert crawler.last_stats.skipped_domain >= 1


def test_allow_domains_enforced(stub_base: str) -> None:
    blocked = _crawler(allow_domains=["example.com"])
    assert blocked.crawl([f"{stub_base}/page1"]) == []
    allowed = _crawler(allow_domains=["127.0.0.1"])
    assert len(allowed.crawl([f"{stub_base}/page1"])) >= 1


def test_max_bytes_aborts_oversized_body(stub_base: str) -> None:
    crawler = _crawler(max_bytes=1024)
    docs = crawler.crawl([f"{stub_base}/big"])
    assert docs == []
    assert _StubHandler.hits.get("/big", 0) == 1
    assert crawler.last_stats.aborted_bytes == 1


def test_content_type_allowlist_enforced(stub_base: str) -> None:
    crawler = _crawler()
    docs = crawler.crawl([f"{stub_base}/binary"])
    assert docs == []
    assert crawler.last_stats.skipped_content_type == 1


def test_unknown_config_keys_rejected() -> None:
    crawler = PoliteCrawler()
    with pytest.raises(ConfigError):
        crawler.configure({"bogus_key": 1})
    with pytest.raises(ConfigError):
        crawler.configure({"timeout_s": 5, "max_pages": 10, "extra": True})


def test_invalid_config_values_rejected() -> None:
    crawler = PoliteCrawler()
    with pytest.raises(ConfigError):
        crawler.configure({"timeout_s": -1})
    with pytest.raises(ConfigError):
        crawler.configure({"max_pages": 0})


def test_normalize_url_sorts_and_strips_volatile_query() -> None:
    from openlens.indexing.crawler import normalize_url

    assert (
        normalize_url("https://Example.com/Path/?b=2&a=1")
        == "https://example.com/path?a=1&b=2"
    )
    # Tracking/view params collapse to the canonical page.
    assert (
        normalize_url("https://example.com/a?utm_source=x&fbclid=y&q=1")
        == "https://example.com/a?q=1"
    )
    assert normalize_url("https://example.com/t?bb=264781") == "https://example.com/t"
    assert (
        normalize_url("https://example.com/trending?since=daily")
        == "https://example.com/trending"
    )
    # Fragments still dropped, case/trailing-slash still folded.
    assert normalize_url("https://EXAMPLE.com/A/#frag") == "https://example.com/a"


def _junk_page(url: str = "https://example.com/seed"):  # noqa: ANN202
    from openlens.indexing.crawler import _FetchedPage

    body = (
        "<html><head><title>Seed</title></head><body>"
        "<p>Informative crawlable prose about the topic. </p>"
        * 12
        + '<nav><a href="https://example.com/nav-link">nav</a></nav>'
        + '<a href="https://example.com/nf" rel="nofollow">nf</a>'
        + '<a href="https://example.com/login">login</a>'
        + '<a href="https://example.com/vote?id=1">vote</a>'
        + '<a href="https://example.com/trending/rust">trend</a>'
        + '<a href="https://example.com/t/python">tag</a>'
        + '<a href="https://example.com/real-article">good</a>'
        + "</body></html>"
    ).encode("utf-8")
    return _FetchedPage(
        status=200, url=url, content_type="text/html", body=body, headers={}
    )


def test_extract_links_drops_nofollow_chrome_and_junk() -> None:
    crawler = _crawler(max_pages=1, respect_robots=False)
    links = crawler._extract_links(_junk_page())
    assert links == ["https://example.com/real-article"]


def test_push_time_robots_gate_keeps_disallowed_out_of_queue(stub_base: str) -> None:
    from openlens.storage.frontier import SqliteFrontierStore

    store = SqliteFrontierStore(":memory:")
    try:
        seed = f"{stub_base}/page1"
        crawler = _crawler(max_pages=1)
        docs = crawler.crawl([seed], frontier_store=store)
        assert len(docs) == 1
        pending = {entry.url for entry in store.pending(limit=10)}
        # /page2 (allowed) queues; /private (robots-disallowed) never does.
        assert any(url.endswith("/page2") for url in pending)
        assert not any(url.endswith("/private") for url in pending)
        assert crawler.last_stats.skipped_robots >= 1
    finally:
        store.close()


def test_discovery_applies_push_time_gates_and_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openlens.indexing.crawler as crawler_mod
    from openlens.storage.frontier import SqliteFrontierStore

    monkeypatch.setattr(crawler_mod, "MAX_OUTLINKS_PER_PAGE", 3)
    store = SqliteFrontierStore(":memory:")
    try:
        seed = "https://example.com/seed"
        assert store.enqueue(seed, 0) is True
        crawler = _crawler(
            max_pages=1, respect_robots=False, deny_domains=["blocked.test"]
        )
        body = (
            "<html><head><title>Seed</title></head><body>"
            + "<p>Informative crawlable prose about the topic. </p>" * 12
            + '<a href="https://example.com/good-1">1</a>'
            + '<a href="https://example.com/good-2">2</a>'
            + '<a href="https://example.com/login">junk</a>'
            + '<a href="https://blocked.test/x">denied</a>'
            + '<a href="https://example.com/good-3">3</a>'
            + '<a href="https://example.com/good-4">over-cap</a>'
            + "</body></html>"
        ).encode("utf-8")
        from openlens.indexing.crawler import _FetchedPage

        monkeypatch.setattr(
            crawler,
            "_fetch_bounded",
            lambda url: _FetchedPage(
                status=200, url=url, content_type="text/html", body=body, headers={}
            ),
        )
        docs = crawler.crawl([seed], frontier_store=store)
        assert len(docs) == 1
        pending = {entry.url for entry in store.pending(limit=20)}
        assert "https://example.com/good-1" in pending
        assert "https://example.com/good-2" in pending
        assert "https://example.com/good-3" in pending
        assert "https://example.com/login" not in pending
        assert "https://blocked.test/x" not in pending
        assert "https://example.com/good-4" not in pending
        assert crawler.last_stats.skipped_domain >= 1
        assert crawler.last_stats.skipped_cap >= 1
    finally:
        store.close()


def test_deny_url_patterns_config_and_enforcement() -> None:
    crawler = _crawler(
        max_pages=1, respect_robots=False, deny_url_patterns=["internal-stuff"]
    )
    links = crawler._extract_links(_junk_page("https://example.com/seed"))
    assert "https://example.com/real-article" in links
    page = _junk_page("https://example.com/seed")
    page.body = (
        b"<html><head><title>T</title></head><body>"
        b'<a href="https://example.com/internal-stuff/x">x</a>'
        b'<a href="https://example.com/real-article">good</a></body></html>'
    )
    assert crawler._extract_links(page) == ["https://example.com/real-article"]
    with pytest.raises(ConfigError):
        _crawler(deny_url_patterns="not-a-list")


def test_decay_child_priority_steps() -> None:
    from openlens.indexing.crawler import decay_child_priority

    assert decay_child_priority(5) == 4
    assert decay_child_priority(1) == 0
    assert decay_child_priority(0) == 0
    # Deprioritized rows stay deprioritized; unknown parents get no boost.
    assert decay_child_priority(-3) == -3
    assert decay_child_priority(None) == 0


def test_persistent_frontier_push_forwards_priority() -> None:
    from openlens.indexing.crawler import PersistentFrontier
    from openlens.storage.frontier import SqliteFrontierStore

    store = SqliteFrontierStore(":memory:")
    try:
        seen: list[tuple[str, int, int | None]] = []
        frontier = PersistentFrontier(
            store, 100, on_push=lambda u, d, p: seen.append((u, d, p))
        )
        frontier.push("https://example.com/a", 1, 4)
        assert store.pending(limit=10)[0].priority == 4
        assert seen == [("https://example.com/a", 1, 4)]
    finally:
        store.close()


def test_persistent_frontier_push_survives_evict_failure() -> None:
    from openlens.indexing.crawler import PersistentFrontier
    from openlens.storage.frontier import SqliteFrontierStore

    class _EvictBoom(SqliteFrontierStore):
        def evict_oldest(self, limit: int) -> int:
            raise RuntimeError("shm exhausted")

    store = _EvictBoom(":memory:")
    try:
        frontier = PersistentFrontier(store, 1)
        frontier.push("https://example.com/a")
        # Over cap: the evict raises inside push, but the row is
        # already stored and the drain must continue, not crash-loop.
        frontier.push("https://example.com/b")
        assert store.counts()["queued"] == 2
        assert frontier.evicted == 0
    finally:
        store.close()


def test_discovered_links_inherit_decayed_priority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openlens.indexing.crawler import _FetchedPage
    from openlens.storage.frontier import SqliteFrontierStore

    store = SqliteFrontierStore(":memory:")
    try:
        seed = "https://example.com/seed"
        assert store.enqueue(seed, 0, priority=5) is True
        crawler = _crawler(max_pages=1, respect_robots=False)
        prose = "<p>Informative crawlable prose about the topic. </p>" * 12
        body = (
            "<html><head><title>Seed</title></head><body>"
            + prose
            + '<a href="https://example.com/child">child</a></body></html>'
        ).encode("utf-8")
        page = _FetchedPage(
            status=200,
            url=seed,
            content_type="text/html",
            body=body,
            headers={},
        )
        monkeypatch.setattr(crawler, "_fetch_bounded", lambda url: page)
        published: list[tuple[str, int, int | None]] = []
        docs = crawler.crawl(
            [seed],
            frontier_store=store,
            on_link=lambda u, d, p: published.append((u, d, p)),
        )
        assert len(docs) == 1
        by_url = {entry.url: entry.priority for entry in store.pending(limit=10)}
        assert by_url["https://example.com/child"] == 4
        assert ("https://example.com/child", 1, 4) in published
    finally:
        store.close()


def test_malformed_ipv6_seed_fails_terminally_not_fatally() -> None:
    """An unclosed IPv6 literal seed is an invalid URL, not a dead worker."""
    crawler = _crawler(respect_robots=False)
    assert crawler.crawl(["http://[::1"]) == []


def test_malformed_ipv6_outlink_is_skipped() -> None:
    """Discovered hrefs that urlsplit rejects never enter the queue."""
    from openlens.indexing.crawler import _FetchedPage

    crawler = _crawler(respect_robots=False)
    prose = "<p>Informative crawlable prose about the topic. </p>" * 12
    body = (
        "<html><head><title>T</title></head><body>"
        + prose
        + '<a href="http://[::1">bad</a>'
        + '<a href="https://example.com/ok">ok</a></body></html>'
    ).encode("utf-8")
    page = _FetchedPage(
        status=200,
        url="https://example.com/",
        content_type="text/html",
        body=body,
        headers={},
    )
    assert crawler._extract_links(page) == ["https://example.com/ok"]


def test_malformed_ipv6_booking_and_alternate_do_not_kill_document() -> None:
    """Bad booking/hreflang hrefs drop the attribute, not the document."""
    from openlens.indexing.crawler import _FetchedPage

    crawler = _crawler(respect_robots=False)
    prose = "<p>Informative crawlable prose about the topic. </p>" * 12
    body = (
        "<html><head><title>T</title>"
        '<link rel="alternate" hreflang="fr" href="http://[::1/fr">'
        "</head><body>" + prose + '<a rel="booking" href="http://[::1/book">b</a>'
        "</body></html>"
    ).encode("utf-8")
    page = _FetchedPage(
        status=200,
        url="https://example.com/",
        content_type="text/html",
        body=body,
        headers={},
    )
    doc = crawler._to_document(page)
    assert "booking_url" not in doc.metadata
    assert doc.metadata.get("hreflang") is None


def test_persistent_frontier_len_tracks_queue_depth() -> None:
    """len() must follow queued+active through the terminal cycle."""
    from openlens.indexing.crawler import PersistentFrontier
    from openlens.storage.frontier import SqliteFrontierStore

    store = SqliteFrontierStore(":memory:")
    try:
        frontier = PersistentFrontier(store, 100)
        store.enqueue("https://example.com/a", 0)
        store.enqueue("https://example.com/b", 0)
        assert len(frontier) == 2
        entry = store.dequeue()
        assert entry is not None
        assert len(frontier) == 2  # active rows still count
        store.mark_done(entry.url)
        assert len(frontier) == 1
    finally:
        store.close()


def test_persistent_frontier_len_prefers_depth_over_counts() -> None:
    """len() must take the O(1) depth path, never the full-scan counts."""
    from openlens.indexing.crawler import PersistentFrontier

    class _DepthOnly:
        def counts(self) -> dict:
            raise AssertionError("full scan must not run in len()")

        def queue_depth(self) -> int:
            return 7

    assert len(PersistentFrontier(_DepthOnly(), 100)) == 7


def test_persistent_frontier_len_falls_back_to_counts() -> None:
    """Stores without a depth fast path keep the old counts math."""

    class _CountsOnly:
        def counts(self) -> dict:
            return {"queued": 2, "active": 1}

    from openlens.indexing.crawler import PersistentFrontier

    assert len(PersistentFrontier(_CountsOnly(), 100)) == 3

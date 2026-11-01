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

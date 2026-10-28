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
                b'<a href="/page2">two</a> '
                b'<a href="/private">priv</a></body></html>',
            )
        elif path == "/page2":
            self._send(
                200,
                "text/html; charset=utf-8",
                b"<html><head><title>Page Two</title></head><body>hello</body></html>",
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

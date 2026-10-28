"""Index breadth (plan step 4): per-domain crawl policy + refresh reporting.

Offline only. Loopback tests skip exactly like the existing crawler
tests (``pytest.skip`` when the sandbox denies loopback bind); every
other test uses fakes or policy-blocked hosts and never touches the
network.
"""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from openlens.core.interfaces import ConfigError, Document
from openlens.indexing.crawler import PoliteCrawler, _FetchedPage
from openlens.indexing.indexer import DocumentIndexer, IndexStats, run_index
from openlens.storage.repository import DocumentRepository


def _crawler(**overrides: object) -> PoliteCrawler:
    crawler = PoliteCrawler()
    crawler.configure(dict(overrides))
    return crawler


def _doc(
    doc_id: str = "d1",
    title: str = "hello",
    content: str = "hello content",
    url: str | None = "http://example.com/a",
) -> Document:
    return Document(id=doc_id, title=title, content=content, url=url)


# -- policy validation is fail-closed -------------------------------------


@pytest.mark.parametrize(
    "bad_config",
    [
        {"domain_policies": ["example.com"]},
        {"domain_policies": {42: {"allow": True}}},
        {"domain_policies": {"": {"allow": True}}},
        {"domain_policies": {"example.com": [True]}},
        {"domain_policies": {"example.com": {"bogus": 1}}},
        {"domain_policies": {"example.com": {"allow": "yes"}}},
        {"domain_policies": {"example.com": {"rate_limit_s": -1}}},
        {"domain_policies": {"example.com": {"rate_limit_s": "fast"}}},
        {"domain_policies": {"example.com": {"rate_limit_s": True}}},
        {"domain_policies": {"example.com": {"max_depth": -1}}},
        {"domain_policies": {"example.com": {"max_depth": "2"}}},
        {"domain_policies": {"example.com": {"max_depth": True}}},
        {"default_max_depth": -1},
        {"default_max_depth": "2"},
        {"default_max_depth": True},
    ],
)
def test_malformed_policy_config_fails_closed(bad_config: dict[str, object]) -> None:
    with pytest.raises(ConfigError):
        _crawler(**bad_config)


def test_policy_defaults_preserve_current_behavior() -> None:
    crawler = _crawler()
    assert crawler.domain_policies == {}
    assert crawler.default_max_depth is None
    assert crawler._max_depth_for("example.com") is None
    assert crawler._rate_limit_for("example.com") == 0.0
    assert crawler._policy_allowed("example.com") is True


def test_valid_policy_config_accepted_and_normalized() -> None:
    crawler = _crawler(
        domain_policies={
            " Example.COM ": {"allow": True, "rate_limit_s": 1.5, "max_depth": 2},
            "blocked.example.net": {"allow": False},
        },
        default_max_depth=3,
    )
    assert set(crawler.domain_policies) == {"example.com", "blocked.example.net"}
    assert crawler._rate_limit_for("example.com") == 1.5
    assert crawler._max_depth_for("example.com") == 2
    assert crawler.default_max_depth == 3
    assert crawler._max_depth_for("other.org") == 3


def test_policy_matches_subdomains_most_specific_wins() -> None:
    crawler = _crawler(
        domain_policies={
            "example.com": {"rate_limit_s": 1.0},
            "sub.example.com": {"rate_limit_s": 2.0},
        }
    )
    assert crawler._rate_limit_for("sub.example.com") == 2.0
    assert crawler._rate_limit_for("deep.sub.example.com") == 2.0
    assert crawler._rate_limit_for("other.example.com") == 1.0
    assert crawler._rate_limit_for("notexample.com") == 0.0


# -- policy enforcement without any network --------------------------------


def test_policy_allow_false_blocks_before_any_fetch() -> None:
    crawler = _crawler(
        respect_robots=False,
        domain_policies={"example.com": {"allow": False}},
    )
    assert crawler.crawl(["http://example.com/"]) == []
    assert crawler.last_stats.skipped_policy == 1


def test_policy_allow_false_beats_global_allowlist() -> None:
    crawler = _crawler(
        respect_robots=False,
        allow_domains=["example.com"],
        domain_policies={"example.com": {"allow": False}},
    )
    assert crawler.crawl(["http://example.com/"]) == []
    assert crawler.last_stats.skipped_policy == 1


def test_policy_allow_true_exempts_global_allowlist_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crawler = _crawler(
        respect_robots=False,
        allow_domains=["other.org"],
        domain_policies={"example.com": {"allow": True}},
    )
    page = _FetchedPage(
        status=200,
        url="http://example.com/",
        content_type="text/plain",
        body=b"hello",
    )
    monkeypatch.setattr(crawler, "_do_fetch", lambda url: page)
    docs = crawler.crawl(["http://example.com/"])
    assert len(docs) == 1
    assert crawler.last_stats.skipped_domain == 0


def test_global_deny_still_wins_over_policy_allow() -> None:
    crawler = _crawler(
        respect_robots=False,
        deny_domains=["example.com"],
        domain_policies={"example.com": {"allow": True}},
    )
    assert crawler.crawl(["http://example.com/"]) == []
    assert crawler.last_stats.skipped_domain == 1


def _html_page(url: str, *links: str) -> _FetchedPage:
    anchors = "".join(f'<a href="{link}">x</a>' for link in links)
    body = f"<html><head><title>{url}</title></head><body>{anchors}</body></html>"
    return _FetchedPage(
        status=200, url=url, content_type="text/html", body=body.encode()
    )


def _chain_fetch(monkeypatch: pytest.MonkeyPatch, crawler: PoliteCrawler) -> None:
    pages = {
        "http://d.test/": _html_page("http://d.test/", "http://d.test/child"),
        "http://d.test/child": _html_page(
            "http://d.test/child", "http://d.test/grandchild"
        ),
        "http://d.test/grandchild": _html_page("http://d.test/grandchild"),
    }

    def fake_fetch(url: str) -> _FetchedPage | None:
        return pages.get(url)

    monkeypatch.setattr(crawler, "_do_fetch", fake_fetch)


def test_default_max_depth_caps_link_following(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crawler = _crawler(respect_robots=False, default_max_depth=1)
    _chain_fetch(monkeypatch, crawler)
    docs = crawler.crawl(["http://d.test/"])
    urls = sorted(doc.url or "" for doc in docs)
    assert urls == ["http://d.test/", "http://d.test/child"]
    assert crawler.last_stats.skipped_depth >= 1


def test_per_domain_max_depth_overrides_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crawler = _crawler(
        respect_robots=False,
        default_max_depth=5,
        domain_policies={"d.test": {"max_depth": 0}},
    )
    _chain_fetch(monkeypatch, crawler)
    docs = crawler.crawl(["http://d.test/"])
    assert [doc.url for doc in docs] == ["http://d.test/"]
    assert crawler.last_stats.skipped_depth >= 1


def test_per_domain_rate_limit_delays_repeat_hits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crawler = _crawler(
        respect_robots=False,
        domain_policies={"d.test": {"rate_limit_s": 0.3}},
    )
    pages = {
        "http://d.test/a": _html_page("http://d.test/a"),
        "http://d.test/b": _html_page("http://d.test/b"),
    }
    monkeypatch.setattr(crawler, "_do_fetch", lambda url: pages[url])
    start = time.monotonic()
    docs = crawler.crawl(["http://d.test/a", "http://d.test/b"])
    elapsed = time.monotonic() - start
    assert len(docs) == 2
    assert elapsed >= 0.3


# -- loopback test: skips exactly like existing crawler tests --------------


def _loopback_server(handler: type[BaseHTTPRequestHandler]) -> ThreadingHTTPServer:
    """Bind a stub server, skipping when the sandbox denies loopback bind."""
    try:
        return ThreadingHTTPServer(("127.0.0.1", 0), handler)
    except OSError as exc:
        pytest.skip(f"loopback bind unavailable in this environment: {exc}")


class _BreadthHandler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        body = (
            b"<html><head><title>root</title></head><body>"
            b'<a href="/child">child</a></body></html>'
            if self.path == "/"
            else b"<html><head><title>child</title></head><body>hi</body></html>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_policy_applies_over_loopback_like_existing_tests() -> None:
    server = _loopback_server(_BreadthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        crawler = _crawler(
            respect_robots=False,
            domain_policies={"127.0.0.1": {"max_depth": 1}},
        )
        docs = crawler.crawl([base + "/"])
        assert len(docs) == 2
        assert crawler.last_stats.fetched == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# -- refresh reporting from existing content hashes ------------------------


def test_describe_refresh_counts_without_writing() -> None:
    repo = DocumentRepository(":memory:")
    indexer = DocumentIndexer(repo)
    indexer.index_incremental([_doc()])
    before = repo.count()

    batch = [
        _doc(),  # unchanged
        _doc(title="hello v2", content="hello content v2"),  # updated
        _doc(doc_id="d2", url="http://example.com/b"),  # duplicate content
        _doc(doc_id="d3", title="new", content="brand new", url="http://x.test/n"),
    ]
    stats = indexer.describe_refresh(batch)
    assert (stats.skipped_unchanged, stats.updated) == (1, 1)
    assert stats.skipped_duplicates == 1
    assert stats.added == 1
    assert repo.count() == before  # read-only: nothing written
    assert stats.summary() == {
        "added": 1,
        "updated": 1,
        "unchanged": 1,
        "duplicates": 1,
    }


def test_describe_refresh_matches_incremental_outcome() -> None:
    repo = DocumentRepository(":memory:")
    indexer = DocumentIndexer(repo)
    indexer.index_incremental([_doc(), _doc(doc_id="d9", content="other stuff")])
    batch = [
        _doc(),
        _doc(doc_id="d9", content="other stuff changed"),
        _doc(doc_id="fresh", content="totally fresh", url="http://x.test/f"),
    ]
    preview = indexer.describe_refresh(batch)
    actual = indexer.index_incremental(batch)
    assert preview.summary() == actual.summary()


def test_run_index_reports_refresh_counts_offline() -> None:
    repo = DocumentRepository(":memory:")
    stats = run_index(
        ["http://example.com/"],
        repo,
        {"respect_robots": False, "domain_policies": {"example.com": {"allow": False}}},
    )
    assert isinstance(stats, IndexStats)
    assert stats.summary() == {
        "added": 0,
        "updated": 0,
        "unchanged": 0,
        "duplicates": 0,
    }


def test_default_yaml_indexing_section_still_configures() -> None:
    config_path = Path(__file__).resolve().parents[2] / "configs" / "default.yaml"
    with config_path.open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    crawler = PoliteCrawler()
    crawler.configure(dict(config["indexing"]))
    assert crawler.domain_policies == {}
    assert crawler.default_max_depth is None


def test_cli_delegation_surfaces_refresh_counts(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import openlens.cli as cli
    import openlens.indexing as indexing_api

    stats = IndexStats(added=1, updated=2, skipped_unchanged=3, skipped_duplicates=4)
    monkeypatch.setattr(
        indexing_api,
        "run_index",
        lambda urls, repository, config: stats,
    )
    monkeypatch.setattr(
        cli,
        "load_app_config",
        lambda path=None: {"indexing": {}},  # type: ignore[arg-type]
    )
    monkeypatch.setattr(cli, "DocumentRepository", lambda db_path: object())
    assert cli.main(["index", "add", "http://example.com/"]) == 0
    out = capsys.readouterr().out
    assert "added=1" in out
    assert "updated=2" in out
    assert "skipped_unchanged=3" in out
    assert "skipped_duplicates=4" in out

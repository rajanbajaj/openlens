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
        body=b"hello world of crawlable prose. " * 30,
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
    prose = "Informative crawlable prose about the topic. " * 20
    body = (
        f"<html><head><title>Guide for {url}</title></head>"
        f"<body><p>{prose}</p>{anchors}</body></html>"
    )
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
        prose = b"Informative crawlable prose about the topic. " * 20
        body = (
            b"<html><head><title>root</title></head><body><p>"
            + prose
            + b'</p><a href="/child">child</a></body></html>'
            if self.path == "/"
            else b"<html><head><title>child</title></head><body><p>"
            + prose
            + b"</p></body></html>"
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
    assert crawler.domain_policies == {
        "arxiv.org": {"rate_limit_s": 3.0},
        "huggingface.co": {"rate_limit_s": 5.0},
        "news24.com": {"rate_limit_s": 5.0},
        "dawn.com": {"rate_limit_s": 5.0},
        "pkg.go.dev": {"rate_limit_s": 5.0},
    }
    assert crawler.default_max_depth == 2


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


# -- explicit seeds re-root the depth budget --------------------------------


def _seed_store(urls_with_depths: list[tuple[str, int]]) -> object:
    """In-memory frontier store preloaded with ``(url, depth)`` rows."""
    from openlens.storage.frontier import SqliteFrontierStore

    store = SqliteFrontierStore(":memory:")
    for url, depth in urls_with_depths:
        assert store.enqueue(url, depth) is True
    return store


def test_explicit_seed_depth_roots_budget_for_children(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A seed submitted with depth 2 follows its page links (bug report).

    Regression: a dev.to profile seeded at depth 2 with
    ``default_max_depth=2`` fetched only itself — the article links
    (children at depth 3) died to ``skipped_depth`` under absolute
    gating. Explicit seeds re-root the budget, so the seed and its
    ``cap`` hops of children are honored, never capped.
    """
    seed = "http://d.test/profile"
    articles = [f"http://d.test/article-{index}" for index in range(2)]
    anchors = "".join(f'<a href="{link}">read</a>' for link in articles)
    prose = "Informative crawlable prose about the topic. " * 20
    seed_page = _FetchedPage(
        status=200,
        url=seed,
        content_type="text/html",
        body=(
            f"<html><head><title>profile</title></head>"
            f"<body><p>{prose}</p>{anchors}</body></html>"
        ).encode(),
    )

    def fake_fetch(url: str) -> _FetchedPage | None:
        if url == seed:
            return seed_page
        return _html_page(url)

    crawler = _crawler(respect_robots=False, default_max_depth=2)
    monkeypatch.setattr(crawler, "_do_fetch", fake_fetch)
    store = _seed_store([(seed, 2)])
    try:
        docs = crawler.crawl([seed], frontier_store=store)
    finally:
        store.close()
    assert sorted(doc.url or "" for doc in docs) == sorted([seed, *articles])
    assert crawler.last_stats.skipped_depth == 0


def test_explicit_seed_above_cap_still_fetches_with_bounded_hops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An admin seed depth above the default is honored, not capped.

    Seed at depth 5 with ``default_max_depth=2`` must still fetch, and
    its children get two hops from the seed (depths 6 and 7); the third
    hop (depth 8) stays capped, so the depth gate still binds.
    """
    chain = {
        "http://d.test/seed": ["http://d.test/a"],
        "http://d.test/a": ["http://d.test/b"],
        "http://d.test/b": ["http://d.test/c"],
        "http://d.test/c": [],
    }

    def fake_fetch(url: str) -> _FetchedPage | None:
        return _html_page(url, *chain.get(url, []))

    crawler = _crawler(respect_robots=False, default_max_depth=2)
    monkeypatch.setattr(crawler, "_do_fetch", fake_fetch)
    store = _seed_store([("http://d.test/seed", 5)])
    try:
        docs = crawler.crawl(["http://d.test/seed"], frontier_store=store)
    finally:
        store.close()
    assert sorted(doc.url or "" for doc in docs) == [
        "http://d.test/a",
        "http://d.test/b",
        "http://d.test/seed",
    ]
    assert crawler.last_stats.skipped_depth >= 1


def test_rows_only_drain_keeps_absolute_depths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backlog/restart rows without seed context keep absolute gating.

    A drain that never saw the explicit seed (empty ``seed_urls``,
    stale preloaded rows) behaves exactly as before: the row at the
    cap fetches, its children stay capped. This pins the no-inflation
    guarantee for queues that predate seed-relative budgeting.
    """
    seed = "http://d.test/profile"
    articles = [f"http://d.test/article-{index}" for index in range(2)]
    anchors = "".join(f'<a href="{link}">read</a>' for link in articles)
    prose = "Informative crawlable prose about the topic. " * 20

    def fake_fetch(url: str) -> _FetchedPage | None:
        if url == seed:
            return _FetchedPage(
                status=200,
                url=seed,
                content_type="text/html",
                body=(
                    f"<html><head><title>profile</title></head>"
                    f"<body><p>{prose}</p>{anchors}</body></html>"
                ).encode(),
            )
        return _html_page(url)

    crawler = _crawler(respect_robots=False, default_max_depth=2)
    monkeypatch.setattr(crawler, "_do_fetch", fake_fetch)
    store = _seed_store([(seed, 2)])
    try:
        docs = crawler.crawl([], frontier_store=store)
    finally:
        store.close()
    assert [doc.url for doc in docs] == [seed]
    assert crawler.last_stats.skipped_depth >= 1


def test_over_cap_rows_only_drain_self_roots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rows-only drain still honors a deep explicit seed (bug report).

    Regression (fossbytes report): an admin seed stored at depth 10
    with ``default_max_depth=2`` failed with "depth cap exceeded"
    when no seed context reached the drain (backlog/idle
    ``_drain_once`` with empty ``seed_urls``). Over-cap preloaded
    rows can only be explicit seeds — discovered links are
    push-gated — so the drain self-roots them: the seed fetches and
    its children get the full cap in hops.
    """
    chain = {
        "http://d.test/seed": ["http://d.test/a"],
        "http://d.test/a": ["http://d.test/b"],
        "http://d.test/b": ["http://d.test/c"],
        "http://d.test/c": [],
    }

    def fake_fetch(url: str) -> _FetchedPage | None:
        return _html_page(url, *chain.get(url, []))

    crawler = _crawler(respect_robots=False, default_max_depth=2)
    monkeypatch.setattr(crawler, "_do_fetch", fake_fetch)
    store = _seed_store([("http://d.test/seed", 10)])
    try:
        docs = crawler.crawl([], frontier_store=store)
    finally:
        store.close()
    assert sorted(doc.url or "" for doc in docs) == [
        "http://d.test/a",
        "http://d.test/b",
        "http://d.test/seed",
    ]
    assert crawler.last_stats.skipped_depth >= 1


def test_hub_page_fans_out_to_every_article_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A link-heavy hub queues every article, not just the first.

    Regression pin for a hub seed (e.g. a homepage full of ``<a>``
    tags) that yielded a single queued URL: junk/chrome/tag-index
    links are still dropped, but every distinct article link is
    enqueued and fetched.
    """
    articles = [f"http://d.test/story-{index}" for index in range(8)]
    anchors = "".join(f'<a href="{link}">read {link}</a>' for link in articles)
    # Chrome, tag indexes, and nofollow links must stay out.
    anchors += (
        '<nav><a href="http://d.test/nav-only">nav</a></nav>'
        '<a href="http://d.test/t/sometag">tag</a>'
        '<a href="http://d.test/nofollow-me" rel="nofollow">nf</a>'
    )
    prose = "Informative crawlable prose about the topic. " * 20
    hub = _FetchedPage(
        status=200,
        url="http://d.test/",
        content_type="text/html",
        body=(
            "<html><head><title>hub</title></head>"
            f"<body><p>{prose}</p>{anchors}</body></html>"
        ).encode(),
    )

    def fake_fetch(url: str) -> _FetchedPage | None:
        if url.rstrip("/") == "http://d.test":
            return hub
        return _html_page(url)

    crawler = _crawler(respect_robots=False, default_max_depth=2)
    monkeypatch.setattr(crawler, "_do_fetch", fake_fetch)
    store = _seed_store([("http://d.test/", 0)])
    try:
        docs = crawler.crawl(["http://d.test/"], frontier_store=store)
    finally:
        store.close()
    assert sorted(doc.url or "" for doc in docs) == sorted(
        ["http://d.test/", *articles]
    )
    assert crawler.last_stats.skipped_depth == 0


def test_reseed_refresh_updates_without_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-crawling a reseeded URL updates its row instead of duplicating.

    Seed, crawl, edit the page, reseed, crawl again: the repository
    holds exactly one row with the latest content (refresh keeps the
    latest crawl, never duplicates).
    """
    from openlens.indexing.indexer import run_index
    from openlens.storage.frontier import SqliteFrontierStore
    from openlens.storage.repository import DocumentRepository

    seed = "http://d.test/story"
    bodies = {"v1": "first version of the story. "}

    def fake_fetch(url: str) -> _FetchedPage | None:
        prose = (bodies["live"] + "Informative crawlable prose. ") * 20
        return _FetchedPage(
            status=200,
            url=url,
            content_type="text/html",
            body=(
                f"<html><head><title>story</title></head>"
                f"<body><p>{prose}</p></body></html>"
            ).encode(),
        )

    bodies["live"] = bodies["v1"]
    crawler = _crawler(respect_robots=False, default_max_depth=2)
    monkeypatch.setattr(crawler, "_do_fetch", fake_fetch)
    repository = DocumentRepository(":memory:")
    store = SqliteFrontierStore(":memory:")
    try:
        first = run_index([seed], repository, {}, crawler=crawler, frontier_store=store)
        assert first.added == 1
        assert repository.count() == 1
        # Edit the page, then refresh via reseed (what a duplicate
        # admin seed now triggers) and crawl again.
        bodies["live"] = "second version of the story, rewritten. "
        assert store.reseed(seed) is True
        second = run_index(
            [seed], repository, {}, crawler=crawler, frontier_store=store
        )
        assert second.added == 0
        assert second.updated == 1
        assert repository.count() == 1
        (hits, total) = repository.search("rewritten", limit=5)
        assert total == 1
        assert "second version" in hits[0][0].content
    finally:
        store.close()
        repository.close()

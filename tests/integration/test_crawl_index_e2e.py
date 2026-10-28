"""End-to-end crawl -> index -> update -> reindex over a stub HTTP site.

Offline only: a local `http.server` stub (in a thread, no network) serves
a mutable 3-page site. The test proves the plan step-4 falsifiable bar:

- one crawl indexes exactly N documents;
- an unchanged re-crawl changes zero rows (no `add_documents` /
  `update_document` calls, identical content hashes);
- editing one page causes an incremental run to rewrite exactly that row;
- a deleted page stays indexed until explicit delete, after which
  `search()` no longer returns it.

The crawl is driven by the real `indexing:` section of
`configs/default.yaml` through `PoliteCrawler.configure()`, so this
suite also wires and proves the config section.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import yaml

from openlens.core.interfaces import Document
from openlens.indexing.crawler import PoliteCrawler
from openlens.indexing.indexer import DocumentIndexer
from openlens.storage.repository import DocumentRepository

CONFIG_PATH = Path(__file__).resolve().parents[2] / "configs" / "default.yaml"


def _loopback_server(handler: type[BaseHTTPRequestHandler]) -> ThreadingHTTPServer:
    """Bind a stub server, skipping when the sandbox denies loopback bind."""
    try:
        return ThreadingHTTPServer(("127.0.0.1", 0), handler)
    except OSError as exc:
        pytest.skip(f"loopback bind unavailable in this environment: {exc}")


N_PAGES = 3


def _page(title: str, body_word: str, links: list[str]) -> bytes:
    anchors = " ".join(f'<a href="{link}">x</a>' for link in links)
    return (
        f"<html><head><title>{title}</title></head>"
        f"<body><p>{body_word}</p>{anchors}</body></html>"
    ).encode("utf-8")


class _SiteHandler(BaseHTTPRequestHandler):
    """Mutable stub site: `pages` maps path -> raw HTML body."""

    pages: dict[str, bytes] = {}
    hits: dict[str, int] = {}

    def log_message(self, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        type(self).hits[path] = type(self).hits.get(path, 0) + 1
        body = type(self).pages.get(path)
        if body is None:
            self._send(404, "text/plain", b"missing")
            return
        self._send(200, "text/html; charset=utf-8", body)

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def site() -> str:
    """Serve a 3-page interlinked stub site; yield its base URL."""
    _SiteHandler.pages = {
        "/a": _page("Alpha", "alphaardvark", ["/b", "/c"]),
        "/b": _page("Beta", "betababoon", ["/a", "/c"]),
        "/c": _page("Gamma", "gammagibbon", ["/a", "/b"]),
    }
    _SiteHandler.hits = {}
    server = _loopback_server(_SiteHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _indexing_section() -> dict[str, Any]:
    with CONFIG_PATH.open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    section = config["indexing"]
    assert isinstance(section, dict), "configs/default.yaml needs an indexing map"
    return dict(section)


def _crawler_from_config() -> PoliteCrawler:
    crawler = PoliteCrawler()
    crawler.configure(_indexing_section())
    return crawler


def _seed_urls(base: str) -> list[str]:
    return [f"{base}/a", f"{base}/b", f"{base}/c"]


def _search_ids(repo: DocumentRepository, term: str) -> list[str]:
    hits, _ = repo.search(term)
    return [doc.id for doc, _ in hits]


class _WriteSpy:
    """Counts repository writes per method and records updated ids."""

    def __init__(self, repo: DocumentRepository) -> None:
        self.add_calls = 0
        self.update_calls = 0
        self.updated_ids: list[str] = []
        orig_add = repo.add_documents
        orig_update = repo.update_document

        def counting_add(docs: list[Document]) -> int:
            self.add_calls += 1
            return orig_add(docs)

        def counting_update(doc: Document) -> bool:
            self.update_calls += 1
            self.updated_ids.append(doc.id)
            return orig_update(doc)

        repo.add_documents = counting_add  # type: ignore[method-assign]
        repo.update_document = counting_update  # type: ignore[method-assign]

    def reset(self) -> None:
        self.add_calls = 0
        self.update_calls = 0
        self.updated_ids = []


def test_indexing_config_section_consumed_by_crawler() -> None:
    """The `indexing:` section loads and configures the crawler cleanly."""
    section = _indexing_section()
    crawler = PoliteCrawler()
    crawler.configure(section)  # must not raise ConfigError
    assert crawler.timeout_s == section["timeout_s"]
    assert crawler.max_pages == section["max_pages"]
    assert crawler.respect_robots is True


def test_crawl_index_e2e(site: str) -> None:
    repo = DocumentRepository(db_path=":memory:")
    indexer = DocumentIndexer(repo)
    spy = _WriteSpy(repo)
    try:
        seeds = _seed_urls(site)

        # 1. Crawl the stub site -> index exactly N documents.
        docs = _crawler_from_config().crawl(seeds)
        assert len(docs) == N_PAGES
        stats = indexer.index_incremental(docs)
        assert (stats.added, stats.updated) == (N_PAGES, 0)
        assert repo.count() == N_PAGES
        assert _search_ids(repo, "alphaardvark")
        assert _search_ids(repo, "betababoon")
        assert _search_ids(repo, "gammagibbon")
        hashes_before = {doc.id: repo.get_hash(doc.id) for doc in docs}
        assert all(hashes_before.values())

        # 2. Re-crawl unchanged -> same count, zero rewrites.
        spy.reset()
        docs_again = _crawler_from_config().crawl(seeds)
        assert len(docs_again) == N_PAGES
        restats = indexer.index_incremental(docs_again)
        assert (restats.added, restats.updated) == (0, 0)
        assert restats.skipped_unchanged == N_PAGES
        assert (spy.add_calls, spy.update_calls) == (0, 0)
        assert repo.count() == N_PAGES
        for doc in docs:
            assert repo.get_hash(doc.id) == hashes_before[doc.id]

        # 3. Edit one page -> incremental run rewrites exactly that row.
        edited_path = "/b"
        _SiteHandler.pages[edited_path] = _page(
            "Beta", "betababoon deltadingo", ["/a", "/c"]
        )
        spy.reset()
        docs_edited = _crawler_from_config().crawl(seeds)
        edit_stats = indexer.index_incremental(docs_edited)
        assert (edit_stats.added, edit_stats.updated) == (0, 1)
        assert (spy.add_calls, spy.update_calls) == (0, 1)
        assert spy.updated_ids == [f"{site}{edited_path}"]
        assert repo.count() == N_PAGES
        assert _search_ids(repo, "deltadingo") == [f"{site}{edited_path}"]
        assert repo.get_hash(f"{site}/a") == hashes_before[f"{site}/a"]
        assert repo.get_hash(f"{site}/c") == hashes_before[f"{site}/c"]

        # 4. Deleted page stays indexed until explicit delete.
        del _SiteHandler.pages["/c"]
        docs_pruned = _crawler_from_config().crawl(seeds)
        prune_stats = indexer.index_incremental(docs_pruned)
        assert (prune_stats.added, prune_stats.updated) == (0, 0)
        assert repo.count() == N_PAGES
        assert _search_ids(repo, "gammagibbon"), (
            "removed page must stay searchable until explicit delete"
        )
        assert indexer.delete_document(f"{site}/c") is True
        assert repo.count() == N_PAGES - 1
        assert _search_ids(repo, "gammagibbon") == []
        assert _search_ids(repo, "alphaardvark"), (
            "explicit delete must not touch surviving rows"
        )
    finally:
        repo.close()

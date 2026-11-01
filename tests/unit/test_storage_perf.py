"""Storage perf batch: deterministic titles + list_domains cache.

Covers the ``storage-perf`` unit against the SQLite reference backend
(``storage/repository.py::DocumentRepository``): ``list_titles`` order
determinism, the short-TTL ``list_domains`` instance cache (a repeat
within the window avoids the bounded scan; expiry rescans), and the
best-effort degrade paths.

Offline only (``:memory:`` DBs).
"""

from __future__ import annotations

from openlens.core.interfaces import Document
from openlens.storage import repository
from openlens.storage.repository import DocumentRepository


def _doc(
    doc_id: str,
    title: str,
    url: str | None = None,
    content: str = "body text",
) -> Document:
    return Document(
        id=doc_id,
        title=title,
        content=content,
        url=url,
        metadata={},
    )


class _CountingConn:
    """Delegate wrapper counting ``execute`` calls (sqlite conn has no dict)."""

    def __init__(self, conn):
        self._conn = conn
        self.execute_calls = 0

    def execute(self, *args, **kwargs):
        self.execute_calls += 1
        return self._conn.execute(*args, **kwargs)

    def __enter__(self):
        return self._conn.__enter__()

    def __exit__(self, *args):
        return self._conn.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _repo_with_counter() -> tuple[DocumentRepository, _CountingConn]:
    repo = DocumentRepository(":memory:")
    counting = _CountingConn(repo._conn)
    repo._conn = counting  # type: ignore[assignment]
    return repo, counting


# ---------------------------------------------------------------------------
# list_titles determinism
# ---------------------------------------------------------------------------


def test_list_titles_is_ordered_by_title():
    repo = DocumentRepository(":memory:")
    repo.add_documents(
        [
            _doc("d3", "cherry pie"),
            _doc("d1", "apple tart"),
            _doc("d2", "mango lassi"),
        ]
    )
    assert repo.list_titles(limit=10) == [
        "apple tart",
        "cherry pie",
        "mango lassi",
    ]


def test_list_titles_respects_limit_in_title_order():
    repo = DocumentRepository(":memory:")
    repo.add_documents(
        [_doc(f"d{i}", title) for i, title in enumerate(["pear", "fig", "date"])]
    )
    assert repo.list_titles(limit=2) == ["date", "fig"]


# ---------------------------------------------------------------------------
# list_domains cache
# ---------------------------------------------------------------------------


def test_list_domains_cache_hit_avoids_rescan():
    repo, counting = _repo_with_counter()
    repo.add_documents(
        [
            _doc("d1", "one", url="https://a.example/x"),
            _doc("d2", "two", url="https://b.example/y"),
        ]
    )
    counting.execute_calls = 0
    first = repo.list_domains()
    assert first == [("a.example", 1), ("b.example", 1)]
    assert counting.execute_calls == 1
    second = repo.list_domains()
    assert second == first
    assert counting.execute_calls == 1


def test_list_domains_cache_expiry_rescans(monkeypatch):
    repo, counting = _repo_with_counter()
    repo.add_documents([_doc("d1", "one", url="https://a.example/x")])
    assert repo.list_domains() == [("a.example", 1)]
    monkeypatch.setattr(repository, "_DOMAINS_CACHE_TTL_S", 0)
    counting.execute_calls = 0
    assert repo.list_domains() == [("a.example", 1)]
    assert counting.execute_calls == 1


def test_list_domains_cache_documents_staleness():
    repo, _ = _repo_with_counter()
    repo.add_documents([_doc("d1", "one", url="https://a.example/x")])
    assert repo.list_domains() == [("a.example", 1)]
    repo.add_document(_doc("d2", "two", url="https://b.example/y"))
    # Write inside the TTL window stays invisible until expiry.
    assert repo.list_domains() == [("a.example", 1)]


# ---------------------------------------------------------------------------
# degrade paths
# ---------------------------------------------------------------------------


def test_list_titles_degrades_to_empty():
    repo = DocumentRepository(":memory:")
    assert repo.list_titles(limit=0) == []
    assert repo.list_titles(limit=-3) == []
    assert repo.list_titles(limit=True) == []  # type: ignore[arg-type]
    repo.close()
    assert repo.list_titles(limit=10) == []


def test_list_domains_degrades_to_empty():
    repo = DocumentRepository(":memory:")
    assert repo.list_domains(limit=0) == []
    assert repo.list_domains(limit=-1) == []
    assert repo.list_domains(limit="nope") == []  # type: ignore[arg-type]

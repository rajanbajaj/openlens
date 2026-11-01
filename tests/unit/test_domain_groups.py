"""Domain grouping: one entry per website, members folded in.

Covers retrieval/domain_groups.py over injectable SearchResult hits —
grouping order, member caps, hostname normalization, composite
summaries, and degrade paths. No I/O, no network.
"""

from __future__ import annotations

from openlens.core.interfaces import Document, SearchResult
from openlens.retrieval.domain_groups import group_key, group_results


def _hit(
    id: str,
    url: str | None,
    title: str = "Title",
    snippet: str = "Snippet",
) -> SearchResult:
    return SearchResult(
        id=id,
        title=title,
        snippet=snippet,
        original_score=1.0,
        final_score=1.0,
        url=url,
        provider="custom_index",
    )


def test_distinct_hosts_pass_through_untouched() -> None:
    first = _hit("1", "https://example.com/a")
    second = _hit("2", "https://example.org/b")
    grouped = group_results([first, second])
    assert grouped == [first, second]
    assert grouped[0].metadata == {}
    assert grouped[1].metadata == {}


def test_same_host_folds_into_representative() -> None:
    first = _hit("1", "https://example.com/a", title="Alpha", snippet="First")
    second = _hit("2", "https://example.com/b", title="Beta")
    third = _hit("3", "https://other.org/c", title="Gamma")
    grouped = group_results([first, second, third])
    assert [hit.id for hit in grouped] == ["1", "3"]
    assert grouped[0].metadata["group_count"] == 2
    assert grouped[0].metadata["group_members"] == [
        {
            "id": "2",
            "title": "Beta",
            "url": "https://example.com/b",
            "snippet": "Snippet",
            "provider": "custom_index",
        }
    ]
    assert grouped[0].metadata["group_summary"] == "Also from example.com: Beta"
    assert grouped[0].snippet == "First"
    assert "group_count" not in grouped[1].metadata


def test_member_cap_drops_overflow() -> None:
    hits = [_hit(str(i), f"https://example.com/p{i}", title=f"T{i}") for i in range(6)]
    grouped = group_results(hits, max_members=2)
    assert [hit.id for hit in grouped] == ["0"]
    assert grouped[0].metadata["group_count"] == 3
    assert [m["id"] for m in grouped[0].metadata["group_members"]] == ["1", "2"]


def test_www_and_case_fold_to_same_host() -> None:
    first = _hit("1", "https://Example.COM/a")
    second = _hit("2", "https://www.example.com/b")
    grouped = group_results([first, second])
    assert [hit.id for hit in grouped] == ["1"]
    assert grouped[0].metadata["group_count"] == 2


def test_subdomains_stay_separate() -> None:
    first = _hit("1", "https://news.example.com/a")
    second = _hit("2", "https://support.example.com/b")
    assert [hit.id for hit in group_results([first, second])] == ["1", "2"]


def test_unusable_urls_pass_through_ungrouped() -> None:
    first = _hit("1", None)
    second = _hit("2", "not a url [[[")
    third = _hit("3", "")
    grouped = group_results([first, second, third])
    assert [hit.id for hit in grouped] == ["1", "2", "3"]
    assert all(hit.metadata == {} for hit in grouped)


def _engine_docs() -> list[Document]:
    docs: list[Document] = []
    for i in range(4):
        docs.append(
            Document(
                id=f"a{i}",
                title=f"alpha guide {i}",
                content="alpha guide content with plenty of words " * 10,
                url=f"https://example.com/a{i}",
                metadata={},
            )
        )
    for i in range(2):
        docs.append(
            Document(
                id=f"b{i}",
                title=f"beta guide {i}",
                content="beta guide content with plenty of words " * 10,
                url=f"https://example.org/b{i}",
                metadata={},
            )
        )
    return docs


def test_engine_groups_same_host_and_totals_count_groups() -> None:
    from openlens.core.engine import SearchEngine
    from openlens.core.interfaces import SearchRequest
    from openlens.providers.custom_index import CustomIndexProvider

    provider = CustomIndexProvider(db_path=":memory:")
    provider.configure({"db_path": ":memory:", "seed": False})
    provider._repository.add_documents(_engine_docs())
    engine = SearchEngine(providers=[provider], plugins=[])
    response = engine.search(SearchRequest(query="guide", mode="default", limit=10))
    hosts = [group_key(hit) for hit in response.results]
    assert len(hosts) == len(set(hosts)) == 2
    assert response.total == 2
    grouped = [
        hit for hit in response.results if hit.metadata.get("group_count", 0) > 1
    ]
    assert len(grouped) == 2
    counts = sorted(hit.metadata["group_count"] for hit in grouped)
    assert counts == [2, 4]


def test_schema_maps_group_fields_and_defaults_ungrouped() -> None:
    from openlens.api.schemas import SearchResultSchema

    hit = _hit("1", "https://example.com/a", title="Alpha", snippet="First")
    hit.metadata["group_count"] = 2
    hit.metadata["group_summary"] = "Also from example.com: Beta"
    hit.metadata["group_members"] = [
        {
            "id": "2",
            "title": "Beta",
            "url": "https://example.com/b",
            "snippet": "Second",
            "provider": "custom_index",
        }
    ]
    mapped = SearchResultSchema.from_core(hit)
    assert mapped.group_count == 2
    assert mapped.group_summary == "Also from example.com: Beta"
    assert [m.id for m in mapped.group_members] == ["2"]
    assert mapped.group_members[0].url == "https://example.com/b"

    plain = SearchResultSchema.from_core(_hit("9", "https://example.org/z"))
    assert plain.group_count == 0
    assert plain.group_summary is None
    assert plain.group_members == []


def test_group_key_normalization() -> None:
    assert group_key(_hit("1", "https://WWW.Example.COM:8080/a")) == "example.com"
    assert group_key(_hit("1", None)) == ""
    assert group_key(_hit("1", "  ")) == ""


def test_degrades_on_malformed_input() -> None:
    assert group_results([]) == []
    assert group_results("nope") == []  # type: ignore[arg-type]
    assert group_results([_hit("1", "https://example.com/a"), object()]) != []
    assert group_results([_hit("1", "https://example.com/a")], max_members="x") != []

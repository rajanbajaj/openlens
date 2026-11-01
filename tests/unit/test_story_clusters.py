"""Story clustering: same-story coverage groups across outlets."""

from __future__ import annotations


def _h(
    doc_id: str,
    title: str,
    published: str | None = None,
    snippet: str = "",
) -> object:
    from openlens.core.interfaces import SearchResult

    metadata: dict[str, object] = {}
    if published is not None:
        metadata["published"] = published
    return SearchResult(
        id=doc_id,
        title=title,
        snippet=snippet or title,
        original_score=1.0,
        final_score=1.0,
        url=f"https://example.com/{doc_id}",
        provider="test",
        metadata=metadata,
    )


def _fixture_hits() -> list[object]:
    return [
        _h(
            "a1",
            "Central bank raises rates to 5% amid inflation fears",
            "2026-10-30",
        ),
        _h(
            "a2",
            "Central Bank Raises Interest Rates to 5% as Inflation Fears Grow",
            "2026-10-31",
        ),
        _h(
            "a3",
            "Rates hiked to five percent by central bank",
            "2026-10-29",
        ),
        _h(
            "b1",
            "Local team wins championship final in overtime",
            "2026-10-30",
        ),
        _h(
            "b2",
            "Championship final: local team triumphs in overtime thriller",
            "2026-10-30",
        ),
        _h(
            "c1",
            "New species of frog found in rainforest",
            "2026-10-20",
        ),
    ]


def test_same_story_across_outlets_clusters_together() -> None:
    from openlens.retrieval.story_clusters import cluster_stories

    clusters = cluster_stories(_fixture_hits())
    by_id = {}
    for cluster in clusters:
        for member in cluster.member_ids:
            by_id[member] = cluster.story_id
    assert by_id["a1"] == by_id["a2"] == by_id["a3"]
    assert by_id["b1"] == by_id["b2"]
    assert by_id["a1"] != by_id["b1"]


def test_loner_stays_out_of_clusters() -> None:
    from openlens.retrieval.story_clusters import cluster_stories

    clusters = cluster_stories(_fixture_hits())
    membered = {m for c in clusters for m in c.member_ids}
    assert "c1" not in membered


def test_cluster_carries_representative_claim_and_window() -> None:
    from openlens.retrieval.story_clusters import cluster_stories

    clusters = cluster_stories(_fixture_hits())
    assert len(clusters) == 2
    rates = next(c for c in clusters if "a1" in c.member_ids)
    assert len(rates.member_ids) == 3
    assert isinstance(rates.claim, str) and rates.claim.strip()
    assert rates.story_id  # stable non-empty id
    # Same headlines a month apart are different stories.
    far = [
        _h("x1", "Central bank raises rates to 5% amid inflation fears", "2026-08-01"),
        _h(
            "x2", "Central Bank Raises Interest Rates to 5% as Fears Grow", "2026-10-31"
        ),
    ]
    assert cluster_stories(far) == []


def test_generic_announcements_do_not_merge() -> None:
    from openlens.retrieval.story_clusters import cluster_stories

    hits = [
        _h("p1", "Apple announces new phone with better camera", "2026-10-30"),
        _h("p2", "Samsung announces new phone with better battery", "2026-10-30"),
    ]
    assert cluster_stories(hits) == []

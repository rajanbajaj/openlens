"""Unit tests for retrieval.perspectives (story views, caveats, fallback)."""

from openlens.api.schemas import PerspectiveViewSchema, SearchResponseSchema
from openlens.core.engine import SearchEngine
from openlens.core.interfaces import SearchRequest, SearchResponse, SearchResult
from openlens.providers.base import BaseSearchProvider
from openlens.retrieval.agent import run_agent_chat
from openlens.retrieval.perspectives import (
    build_perspectives,
    extractive_fallback_answer,
)


def _hit(
    doc_id: str,
    title: str,
    snippet: str,
    *,
    url: str = "",
    story_id: str | None = None,
    warnings: list[str] | None = None,
    trusted: bool | None = None,
) -> SearchResult:
    metadata: dict[str, object] = {}
    if story_id is not None:
        metadata["story_id"] = story_id
        metadata["key_claims"] = [f"Claim about {title}"]
    if trusted is not None:
        metadata["trusted"] = trusted
    return SearchResult(
        id=doc_id,
        title=title,
        snippet=snippet,
        original_score=1.0,
        final_score=1.0,
        url=url or f"https://{doc_id}.example.com/{doc_id}",
        provider="test",
        reasons=[],
        warnings=list(warnings or []),
        metadata=metadata,
    )


def test_groups_prefer_pipeline_story_id() -> None:
    hits = [
        _hit("a", "Council approves budget", "The council approved.", story_id="s1"),
        _hit("b", "Council budget approved", "Critics deny it helps.", story_id="s1"),
        _hit("c", "Unrelated weather post", "Sunny skies today.", story_id=None),
    ]
    views = build_perspectives(hits)
    assert len(views) == 1
    view = views[0]
    assert view.story_id == "s1"
    assert view.representative_id == "a"
    assert view.member_ids == ["a", "b"]
    assert view.claim == "Claim about Council approves budget"
    assert view.to_dict()["member_ids"] == ["a", "b"]


def test_orphans_cluster_without_story_id() -> None:
    hits = [
        _hit("a", "Harbor bridge reopens after repairs", "The bridge reopened Monday."),
        _hit("b", "Harbor bridge reopens to traffic", "Officials confirm reopening."),
    ]
    views = build_perspectives(hits)
    assert len(views) == 1
    assert views[0].story_id.startswith("cluster-")
    assert views[0].member_ids == ["a", "b"]


def test_singletons_yield_no_views() -> None:
    hits = [_hit("a", "Lone post about weather", "Sunny skies today.")]
    assert build_perspectives(hits) == []
    assert build_perspectives([]) == []
    assert build_perspectives(None) == []  # type: ignore[arg-type]


def test_trust_caveats_and_missing_note() -> None:
    hits = [
        _hit(
            "a",
            "Council approves budget",
            "The study confirms the budget passed.",
            story_id="s1",
            warnings=["unverified source"],
            trusted=False,
            url="https://example.com/a",
        ),
        _hit(
            "b",
            "Council budget approved",
            "Data shows the budget passed.",
            story_id="s1",
            url="https://example.com/b",
        ),
    ]
    (view,) = build_perspectives(hits)
    assert any("unverified source" in caveat for caveat in view.trust_caveats)
    assert view.missing_note == "no refuting source in this cluster"
    assert view.multi_source is False


def test_multi_source_across_hosts() -> None:
    hits = [
        _hit(
            "a",
            "Council approves budget",
            "The council approved.",
            story_id="s1",
            url="https://one.example/x",
        ),
        _hit(
            "b",
            "Council budget approved",
            "The council approved.",
            story_id="s1",
            url="https://two.example/y",
        ),
    ]
    (view,) = build_perspectives(hits)
    assert view.multi_source is True


def test_views_capped_and_rank_ordered() -> None:
    hits = [
        _hit("a", "Harbor bridge reopens Monday", "Open.", story_id="s1"),
        _hit("b", "Harbor bridge reopens Tuesday", "Open.", story_id="s1"),
        _hit("c", "Stadium vote passes council", "Passed.", story_id="s2"),
        _hit("d", "Stadium vote passes review", "Passed.", story_id="s2"),
    ]
    views = build_perspectives(hits, max_views=1)
    assert [view.story_id for view in views] == ["s1"]


def test_extractive_fallback_stitches_markers() -> None:
    hits = [
        _hit("a", "T", "The bridge reopened on Monday. Crews finished early."),
        _hit("b", "T", "Officials confirmed the reopening. Traffic resumes."),
    ]
    answer = extractive_fallback_answer(hits)
    assert "[1]" in answer and "[2]" in answer
    assert "Monday" in answer and "confirmed" in answer


def test_extractive_fallback_dedupes_and_degrades() -> None:
    hits = [
        _hit("a", "T", "The bridge reopened on Monday."),
        _hit("b", "T", "The bridge reopened on Monday."),
    ]
    answer = extractive_fallback_answer(hits)
    assert answer.count("[") == 1
    assert extractive_fallback_answer([_hit("a", "T", "")]) == ""


class _StoryProvider(BaseSearchProvider):
    """Canned provider returning one story pair plus a loner."""

    def __init__(self, hits: list[SearchResult]) -> None:
        self._hits = list(hits)

    @property
    def name(self) -> str:
        return "story-fake"

    def search(self, request: SearchRequest) -> list[SearchResult]:
        return list(self._hits)

    def health_check(self) -> bool:
        return True


def _story_hits() -> list[SearchResult]:
    return [
        _hit("a", "Harbor bridge reopens Monday", "The bridge reopened Monday."),
        _hit("b", "Harbor bridge reopens Tuesday", "Officials confirm reopening."),
        _hit("c", "Unrelated weather post", "Sunny skies today."),
    ]


def test_engine_attaches_perspectives_to_response() -> None:
    engine = SearchEngine(providers=[_StoryProvider(_story_hits())])
    response = engine.search(SearchRequest(query="bridge", mode="default"))
    assert len(response.perspectives) == 1
    assert response.perspectives[0]["member_ids"] == ["a", "b"]


def test_wire_carries_perspectives_with_zero_schema_breaks() -> None:
    engine = SearchEngine(providers=[_StoryProvider(_story_hits())])
    core = engine.search(SearchRequest(query="bridge", mode="default"))
    body = SearchResponseSchema.from_core(core)
    assert len(body.perspectives) == 1
    view = body.perspectives[0]
    assert view.claim
    assert view.member_ids == ["a", "b"]
    # Degenerate core responses (no perspectives attr) still validate.
    bare = SearchResponse(query="q", mode="default", total=0)
    assert SearchResponseSchema.from_core(bare).perspectives == []


def test_from_mapping_degrades_malformed_entries() -> None:
    assert PerspectiveViewSchema.from_mapping(None).story_id == ""
    assert PerspectiveViewSchema.from_mapping({"stance": "bogus"}).stances == []
    coerced = PerspectiveViewSchema.from_mapping(
        {"story_id": "s", "stances": [{"source_id": "a", "stance": "bogus"}]}
    )
    assert coerced.stances[0].stance == "neutral"


def test_chat_outcome_carries_perspectives_without_model() -> None:
    engine = SearchEngine(providers=[_StoryProvider(_story_hits())])
    outcome = run_agent_chat("bridge reopening?", [], 5, engine)
    assert isinstance(outcome["perspectives"], list)
    assert outcome["perspectives"][0]["member_ids"] == ["a", "b"]

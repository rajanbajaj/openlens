"""Smoke tests for offline query-assistance helpers and their schemas.

Covers retrieval/suggest.py (prefix completion, edit-distance spelling,
related-query derivation) plus the additive wire fields in
api/schemas.py. Everything is offline over injectable data; the JSON
fixture is a small vocabulary, not a service.
"""

from __future__ import annotations

import json
from pathlib import Path

from openlens.api.schemas import (
    SearchResponseSchema,
    SuggestResponseSchema,
    compute_next_offset,
)
from openlens.core.interfaces import SearchResponse
from openlens.retrieval.suggest import (
    complete,
    correct_spelling,
    related_queries,
)

FIXTURE_PATH = Path(__file__).parent.parent / "fixtures" / "suggest_terms.json"


def _fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_complete_prefix_matches_sorted_and_limited() -> None:
    data = _fixture()
    assert complete("run", data["terms"]) == [
        "running shoes",
        "running shoes for flat feet",
        "running watch",
    ]
    assert complete("run", data["terms"], limit=2) == [
        "running shoes",
        "running shoes for flat feet",
    ]


def test_complete_case_insensitive_and_deterministic() -> None:
    data = _fixture()
    first = complete("TRAIL", data["terms"])
    assert first == ["trail running vest", "trail shoes"]
    # Deterministic across repeated calls and input orderings.
    assert complete("trail", data["terms"]) == first
    assert complete("trail", list(reversed(data["terms"]))) == first
    assert complete("trail", data["terms"]) == complete("trail", data["terms"])


def test_complete_empty_index_returns_empty() -> None:
    assert complete("run", []) == []
    assert complete("", ["running shoes"]) == []
    assert complete("   ", ["running shoes"]) == []


def test_complete_malformed_input_never_raises() -> None:
    assert complete(None, ["running shoes"]) == []
    assert complete(123, ["running shoes"]) == []
    assert complete("run", None) == []
    assert complete("run", "running shoes") == []
    assert complete("trail", [None, 42, "  ", "trail shoes"]) == ["trail shoes"]
    assert complete("run", ["running shoes"], limit=0) == []
    assert complete("run", ["running shoes"], limit=-3) == []


def test_correct_spelling_fixes_typo() -> None:
    assert correct_spelling("runing", ["running", "trail"]) == "running"
    assert correct_spelling("headpones", ["headphones", "charger"]) == "headphones"


def test_correct_spelling_exact_match_returns_none() -> None:
    assert correct_spelling("running", ["running", "trail"]) is None
    assert correct_spelling("Running", ["running"]) is None


def test_correct_spelling_empty_vocabulary_returns_none() -> None:
    assert correct_spelling("runing", []) is None
    assert correct_spelling("", ["running"]) is None
    assert correct_spelling("   ", ["running"]) is None


def test_correct_spelling_too_far_returns_none() -> None:
    assert correct_spelling("zzz", ["running", "trail"]) is None


def test_correct_spelling_malformed_input_never_raises() -> None:
    assert correct_spelling(None, ["running"]) is None
    assert correct_spelling("runing", None) is None
    assert correct_spelling(42, 42) is None
    assert correct_spelling("runing", ["running"], max_distance=0) is None


def test_correct_spelling_deterministic_tiebreak() -> None:
    assert correct_spelling("cat", ["bat", "hat", "cut"]) == "bat"
    assert correct_spelling("cat", ["hat", "cut", "bat"]) == "bat"


def test_related_queries_ranked_by_count() -> None:
    data = _fixture()
    assert related_queries("running", data["counts"]) == [
        "running shoes",
        "running watch",
        "running shoes for flat feet",
        "trail running vest",
    ]


def test_related_queries_excludes_self_and_non_overlapping() -> None:
    counts = {"running shoes": 10, "coffee maker": 99, "Running Shoes": 5}
    assert related_queries("running shoes", counts) == []


def test_related_queries_empty_store_returns_empty() -> None:
    assert related_queries("running", {}) == []
    assert related_queries("", {"running shoes": 3}) == []


def test_related_queries_malformed_input_never_raises() -> None:
    assert related_queries(None, {"running shoes": 3}) == []
    assert related_queries("running", None) == []
    assert related_queries("running", ["running shoes"]) == []
    assert related_queries("running", {"running shoes": "many"}) == []
    assert related_queries("running", {None: 3}) == []
    assert related_queries("running", {"running shoes": 3}, limit=0) == []


def test_compute_next_offset() -> None:
    assert compute_next_offset(25, 10, 0) == 10
    assert compute_next_offset(25, 10, 20) is None
    assert compute_next_offset(20, 10, 10) is None
    assert compute_next_offset(0, 10, 0) is None


def test_search_response_schema_from_core_additive() -> None:
    core = SearchResponse(query="q", mode="default", total=25)
    body = SearchResponseSchema.from_core(core)
    assert body.query == "q"
    assert body.total == 25
    assert body.results == []
    assert body.limit == 10
    assert body.offset == 0
    assert body.next_offset == 10
    assert body.suggestions == []
    assert body.spelling is None
    assert body.related == []


def test_search_response_schema_from_core_carries_assistance() -> None:
    core = SearchResponse(query="runing shoes", mode="default", total=3)
    body = SearchResponseSchema.from_core(
        core,
        limit=10,
        offset=0,
        suggestions=["running shoes"],
        spelling="running shoes",
        related=["trail shoes"],
    )
    assert body.suggestions == ["running shoes"]
    assert body.spelling == "running shoes"
    assert body.related == ["trail shoes"]
    assert body.next_offset is None


def test_suggest_response_schema() -> None:
    body = SuggestResponseSchema(q="run", suggestions=["running shoes"])
    assert body.q == "run"
    assert body.suggestions == ["running shoes"]
    assert SuggestResponseSchema(q="run").suggestions == []

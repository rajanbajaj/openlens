"""IMPL-D: AI intelligence, customization, transparency + cross-cutting gaps.

Covers Cat10 context-aware history (opt-in, request-scoped, never
persisted) and the expand-rewrite variant (off by default); Cat11
per-request weight overrides, personalize opt-out, named safety
presets, and the saved-prefs API; Cat12 deterministic confidence and
the history wire format. Every behavior ships with the repo gates:
ruff, mypy, pytest.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from openlens.cache.base import build_cache_key
from openlens.core.context import SearchContext
from openlens.core.engine import SearchEngine
from openlens.core.interfaces import (
    ConfigError,
    PluginError,
    SearchProvider,
    SearchRequest,
    SearchResult,
)
from openlens.core.pipeline import assemble_explanations
from openlens.main import create_app
from openlens.plugins.preference_scorer import PreferenceScorer
from openlens.plugins.safety_filter import SafetyFilter
from openlens.plugins.user_restrictions import UserRestrictionsFilter
from openlens.ranking.ranker import Ranker
from openlens.retrieval.query_expand import QueryExpander
from openlens.retrieval.query_processor import QueryProcessor


def _hit(
    id: str,
    title: str,
    snippet: str = "",
    original: float = 1.0,
    url: str | None = None,
) -> SearchResult:
    return SearchResult(
        id=id,
        title=title,
        snippet=snippet,
        original_score=original,
        final_score=original,
        url=url,
    )


def _ctx(
    hits: list[SearchResult],
    mode: str = "default",
    query: str = "q",
    filters: dict[str, Any] | None = None,
) -> SearchContext:
    ctx = SearchContext(
        request=SearchRequest(query=query, mode=mode, filters=filters or {})
    )
    ctx.results = hits
    return ctx


class FakeProvider(SearchProvider):
    """Offline provider returning canned hits."""

    def __init__(self, hits: list[SearchResult]) -> None:
        self._hits = hits

    @property
    def name(self) -> str:
        return "fake"

    def configure(self, config: dict[str, Any]) -> None:
        pass

    def search(self, request: SearchRequest) -> list[SearchResult]:
        return list(self._hits)

    def health_check(self) -> bool:
        return True


# --- Cat10: context-aware history -------------------------------------------


def _processor_with_context(enabled: bool = True) -> QueryProcessor:
    processor = QueryProcessor()
    processor.configure(
        {"query": {"context": {"enabled": enabled, "max_history_terms": 20}}}
    )
    return processor


def test_history_ignored_by_default() -> None:
    processor = QueryProcessor()
    processed = processor.process("dress", history=("cheap shoes",))
    assert processed.history_terms == []
    assert not any("context" in reason for reason in processed.reasons)


def test_history_recalled_when_enabled() -> None:
    processor = _processor_with_context()
    processed = processor.process("dress", history=("cheap shoes",))
    # "shoes" belongs to the footwear synonym set and is absent here.
    assert "shoes" in processed.history_terms
    assert any("context" in reason for reason in processed.reasons)


def test_history_never_invents_vocabulary() -> None:
    processor = _processor_with_context()
    processed = processor.process("dress", history=("xylophone zebras",))
    assert processed.history_terms == []


def test_history_respects_max_added_cap() -> None:
    processor = QueryProcessor()
    processor.configure(
        {
            "query": {
                "expansion": {"max_added_terms": 1},
                "context": {"enabled": True, "max_history_terms": 20},
            }
        }
    )
    processed = processor.process("dress phone", history=("cheap shoes",))
    assert len(processed.added) <= 1


def test_context_config_rejects_bad_shapes() -> None:
    processor = QueryProcessor()
    with pytest.raises(ConfigError):
        processor.configure({"query": {"context": {"enabled": "yes"}}})
    with pytest.raises(ConfigError):
        processor.configure(
            {"query": {"context": {"enabled": True, "max_history_terms": -1}}}
        )


def test_engine_wires_history_into_diagnostics() -> None:
    engine = SearchEngine(
        providers=[FakeProvider([_hit("d", "dress shop")])],
        plugins=[UserRestrictionsFilter()],
    )
    engine.configure(
        {"plugins": [], "query": {"context": {"enabled": True}}}  # type: ignore[arg-type]
    )
    resp = engine.search(
        SearchRequest(query="dress", filters={}, history=("cheap shoes",))
    )
    query_entry = resp.diagnostics["query"]
    assert isinstance(query_entry, dict)
    assert "shoes" in query_entry["history_terms"]


# --- Cat10: expand-rewrite variant -------------------------------------------


def test_rewrite_off_by_default() -> None:
    assert QueryExpander().rewrite("cheap dress") is None


def test_rewrite_substitutes_one_synonym_per_term() -> None:
    expander = QueryExpander()
    expander.configure({"expand_rewrite": True})
    rewritten = expander.rewrite("cheap dress")
    assert rewritten is not None
    assert rewritten != "cheap dress"
    assert len(rewritten.split()) == 2


def test_rewrite_rejects_non_bool_flag() -> None:
    expander = QueryExpander()
    with pytest.raises(ConfigError):
        expander.configure({"expand_rewrite": "yes"})


def test_engine_emits_rewrite_variant_only_when_enabled() -> None:
    engine = SearchEngine(
        providers=[FakeProvider([_hit("d", "dress shop")])], plugins=[]
    )
    base_variants = engine._expansion_variants(
        engine._query_processor.process("cheap dress")
    )
    assert not [v for v in base_variants if v[2] == "expand_rewrite"]
    engine._query_processor.configure(
        {"query": {"expansion": {"expand_rewrite": True}}}
    )
    enabled_variants = engine._expansion_variants(
        engine._query_processor.process("cheap dress")
    )
    assert [v for v in enabled_variants if v[2] == "expand_rewrite"]


# --- Cat11: weight overrides -------------------------------------------------


def test_weight_overrides_retune_ranking() -> None:
    ranker = Ranker()
    first = _hit("a", "alpha beta", original=1.0, url="https://a.example/")
    second = _hit("b", "gamma", original=2.0, url="https://b.example/")
    plain = ranker.run(_ctx([first, second]))
    boosted = ranker.run(
        _ctx(
            [_hit("a", "alpha beta", original=1.0), _hit("b", "gamma", original=2.0)],
            filters={"weight_overrides": {"relevance_weight": 0.0}},
        )
    )
    assert [r.id for r in plain.results] == ["b", "a"]
    assert boosted.applied_filters == ["ranker:weight_overrides"]
    assert any(
        "custom weights" in reason for r in boosted.results for reason in r.reasons
    )


def test_weight_overrides_reject_unknown_and_nonfinite() -> None:
    ranker = Ranker()
    with pytest.raises(PluginError):
        ranker.run(_ctx([_hit("a", "x")], filters={"weight_overrides": {"nope": 1.0}}))
    with pytest.raises(PluginError):
        ranker.run(
            _ctx(
                [_hit("a", "x")],
                filters={"weight_overrides": {"trust_weight": float("inf")}},
            )
        )
    with pytest.raises(PluginError):
        ranker.run(
            _ctx([_hit("a", "x")], filters={"weight_overrides": ["trust_weight"]})
        )


# --- Cat11: personalize + safety presets -------------------------------------


def test_personalize_false_neutralizes_preferences() -> None:
    scorer = PreferenceScorer()
    hit = _hit("a", "trusted review", url="https://preferred.example/x")
    ctx = _ctx([hit], filters={"prefer_domains": ["preferred.example"]})
    scored = scorer.run(ctx)
    assert scored.results[0].metadata["signals"]["preference"] > 0
    ctx = _ctx(
        [_hit("a", "trusted review", url="https://preferred.example/x")],
        filters={"prefer_domains": ["preferred.example"], "personalize": False},
    )
    neutral = scorer.run(ctx)
    assert neutral.results[0].metadata["signals"]["preference"] == 0.5
    assert any("personalization off" in r for r in neutral.results[0].reasons)


def test_control_keys_validated_fail_closed() -> None:
    plugin = UserRestrictionsFilter()
    plugin.configure({})
    with pytest.raises(PluginError):
        plugin._compile_predicate({"personalize": "yes"})
    with pytest.raises(PluginError):
        plugin._compile_predicate({"safety_preset": "paranoid"})
    plugin._compile_predicate({"personalize": False, "safety_preset": "strict"})


def test_strict_preset_arms_default_deny() -> None:
    safety = SafetyFilter()
    safety.configure({"allowed_domains": ["trusted.example"]})
    stranger = _hit("s", "nice page", url="https://stranger.example/x")
    friend = _hit("f", "nice page", url="https://trusted.example/x")
    open_ctx = _ctx([stranger, friend], filters={"safety_preset": "standard"})
    assert [r.id for r in safety.run(open_ctx).results] == ["s", "f"]
    strict_ctx = _ctx(
        [_hit("s", "nice page", url="https://stranger.example/x"), friend],
        filters={"safety_preset": "strict"},
    )
    assert [r.id for r in safety.run(strict_ctx).results] == ["f"]


def test_control_keys_recorded_in_applied_filters() -> None:
    plugin = UserRestrictionsFilter()
    plugin.configure({})
    ctx = _ctx(
        [_hit("a", "x")],
        filters={"personalize": False, "safety_preset": "strict"},
    )
    plugin.run(ctx)
    assert "safety_preset:strict" in ctx.applied_filters
    assert "personalize:false" in ctx.applied_filters


# --- Cat12: confidence --------------------------------------------------------


def test_confidence_single_hit_is_one() -> None:
    ctx = _ctx([_hit("a", "x", original=2.0)])
    ctx.results[0].final_score = 2.0
    stamped = assemble_explanations(ctx)
    assert stamped.results[0].metadata["confidence"] == 1.0


def test_confidence_spans_zero_to_one_in_rank_order() -> None:
    ctx = _ctx(
        [
            _hit("a", "x", original=3.0),
            _hit("b", "y", original=2.0),
            _hit("c", "z", original=1.0),
        ]
    )
    for hit in ctx.results:
        hit.final_score = hit.original_score
    stamped = assemble_explanations(ctx)
    confidences = [r.metadata["confidence"] for r in stamped.results]
    assert confidences == [1.0, 0.5, 0.0]
    # Deterministic: identical inputs agree exactly.
    again = assemble_explanations(
        _ctx([_hit("a", "x", original=3.0), _hit("b", "y", original=2.0)])
    )
    for hit in again.results:
        hit.final_score = hit.original_score
    assert [r.metadata["confidence"] for r in assemble_explanations(again).results] == [
        1.0,
        0.0,
    ]


# --- Cat11/12: saved prefs + history wire -------------------------------------


def _client() -> TestClient:
    from tests.unit.test_api import FakeEngine

    return TestClient(create_app(engine=FakeEngine()))


def test_saved_prefs_roundtrip() -> None:
    from openlens.api import routes

    routes._SAVED_PREFS.clear()
    client = _client()
    put = client.put(
        "/api/v1/prefs",
        json={"name": "strict-news", "filters": {"safety_preset": "strict"}},
    )
    assert put.status_code == 200, put.text
    assert put.json() == {
        "name": "strict-news",
        "filters": {"safety_preset": "strict"},
    }
    listed = client.get("/api/v1/prefs")
    assert listed.json() == {"prefs": {"strict-news": {"safety_preset": "strict"}}}
    routes._SAVED_PREFS.clear()


def test_saved_prefs_rejects_blanks_and_extra_fields() -> None:
    client = _client()
    assert (
        client.put("/api/v1/prefs", json={"name": "   ", "filters": {}}).status_code
        == 422
    )
    assert (
        client.put(
            "/api/v1/prefs", json={"name": "x", "filters": {}, "query": "leak"}
        ).status_code
        == 422
    )


def test_search_history_wire_reaches_engine() -> None:
    from tests.unit.test_api import FakeEngine

    engine: Any = FakeEngine()
    client = TestClient(create_app(engine=engine))
    resp = client.post(
        "/api/v1/search",
        json={"query": "dress", "history": [" cheap shoes ", ""]},
    )
    assert resp.status_code == 200, resp.text
    assert engine.last_request is not None
    assert engine.last_request.history == ("cheap shoes",)
    overlong = client.post(
        "/api/v1/search", json={"query": "dress", "history": ["x" * 1001]}
    )
    assert overlong.status_code == 422


def test_cache_key_covers_history_only_when_present() -> None:
    def key(history: Any = None) -> str:
        params: dict[str, Any] = {
            "normalized_query": "dress",
            "mode": "default",
            "filters": {},
            "provider_prefs": None,
            "provider_names": ["custom_index"],
            "ranking_profile": "default",
            "fetch_limit": 10,
        }
        if history is not None:
            params["history"] = history
        return build_cache_key(**params)  # type: ignore[arg-type]

    legacy = key()
    assert key([]) == legacy
    assert key(["cheap shoes"]) != legacy

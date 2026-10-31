"""Regression tests for retrieval multilingual/code-query gaps.

Pins the probe cases: café accent, CJK silent-zero-result, e-mail
escaping, version-string query, hyphenated product — plus semantic
normalization and suggest upgrades (last-token completion,
normalized-token related overlap, per-token spelling, synonym map).
"""

from __future__ import annotations

from openlens.core.interfaces import Document, SearchRequest
from openlens.models.manager import ModelManager
from openlens.providers.custom_index import CustomIndexProvider
from openlens.retrieval.query_parser import hyphen_compounds, parse_query
from openlens.retrieval.semantic import rank_ids_by_semantic
from openlens.retrieval.suggest import (
    complete,
    correct_spelling,
    related_queries,
)

PROBE_DOCS = [
    Document(
        id="doc-cafe",
        title="Café Reviews",
        content="best café in town with great espresso",
        url="https://example.com/cafe",
    ),
    Document(
        id="doc-cjk",
        title="CJK doc",
        content="中文搜索测试指南",
        url="https://example.com/cjk",
    ),
    Document(
        id="doc-email",
        title="email setup",
        content="how to set up your email inbox",
        url="https://example.com/email",
    ),
    Document(
        id="doc-version",
        title="python 3.11 release",
        content="python 3.11 release notes and downloads",
        url="https://example.com/python311",
    ),
    Document(
        id="doc-hyphen",
        title="node-js guide",
        content="node js runtime package manager tutorial",
        url="https://example.com/nodejs",
    ),
]


def make_provider() -> CustomIndexProvider:
    provider = CustomIndexProvider(db_path=":memory:")
    provider.configure({"db_path": ":memory:", "seed": False})
    provider.index_documents(list(PROBE_DOCS))
    return provider


def ids(provider: CustomIndexProvider, query: str) -> list[str]:
    return [hit.id for hit in provider.search(SearchRequest(query=query, limit=10))]


def test_accent_query_finds_accented_doc() -> None:
    provider = make_provider()
    assert "doc-cafe" in ids(provider, "café")
    assert "doc-cafe" in ids(provider, "cafe")


def test_cjk_query_returns_hits_not_silent_zero() -> None:
    provider = make_provider()
    assert ids(provider, "中文搜索"), "CJK prefix query must match CJK content"
    assert ids(provider, "中文搜索测试指南")


def test_email_query_does_not_raise_and_matches() -> None:
    provider = make_provider()
    assert "doc-email" in ids(provider, "e-mail")
    assert "doc-email" in ids(provider, "email")


def test_raw_operators_never_reach_match() -> None:
    parsed = parse_query('e-mail (test) "quoted" AND or')
    assert "-" not in parsed.fts_query
    assert '"' not in parsed.fts_query
    provider = make_provider()
    provider.search(SearchRequest(query='e-mail (test) "quoted"', limit=10))


def test_version_string_query_matches() -> None:
    provider = make_provider()
    assert "doc-version" in ids(provider, "python 3.11")
    assert "doc-version" in ids(provider, "3.11")


def test_hyphenated_product_query_matches() -> None:
    provider = make_provider()
    assert "doc-hyphen" in ids(provider, "node-js")
    assert hyphen_compounds("e-mail") == ["email"]
    assert "email" in parse_query("e-mail").fts_query


def test_parser_keeps_ascii_terms_stable() -> None:
    parsed = parse_query("python sqlite FTS5")
    assert parsed.terms == ["python", "sqlite", "fts5"]
    assert parsed.fts_query == "python AND sqlite AND fts5"


def test_parser_demotes_weak_terms_to_optional() -> None:
    parsed = parse_query("python 3.11")
    assert parsed.terms == ["python", "3", "11"]
    assert " OR " in parsed.fts_query
    assert parsed.fts_query.startswith("(python)")


def test_unicode_tokenizer_keeps_accents_and_cjk() -> None:
    assert parse_query("Café").terms == ["café"]
    assert parse_query("中文搜索").terms == ["中文搜索"]
    assert parse_query("!!!").fts_query == ""


def test_semantic_embeds_normalized_query() -> None:
    seen: list[list[str]] = []

    class RecordingModels:
        def embed(self, texts: list[str]) -> list[list[float]]:
            seen.append(list(texts))
            dim = 3
            return [[float(len(text) % 7)] * dim for text in texts]

    provider = make_provider()
    hits = provider.search(SearchRequest(query="python", limit=5))
    assert hits
    assert rank_ids_by_semantic("  CAFÉ Reviews! ", hits, RecordingModels())
    assert seen and seen[0][0] == "café reviews"


def test_semantic_case_variants_agree_with_fake_backend() -> None:
    models = ModelManager.from_config(
        {"models": {"fake": {"enabled": True, "type": "fake"}}}
    )
    provider = make_provider()
    hits = provider.search(SearchRequest(query="python", limit=5))
    assert rank_ids_by_semantic("CAFÉ REVIEWS", hits, models) == rank_ids_by_semantic(
        "café reviews", hits, models
    )


def test_complete_last_token_completion() -> None:
    terms = ["running shoes", "trail running vest", "coffee maker"]
    assert "trail running vest" in complete("shoes run", terms)
    # Single-token behavior unchanged.
    assert complete("run", terms) == ["running shoes"]


def test_complete_synonym_map() -> None:
    assert complete("js", ["javascript basics", "coffee maker"]) == [
        "javascript basics"
    ]
    assert complete("k8s", ["kubernetes guide"]) == ["kubernetes guide"]


def test_related_normalized_and_synonym_overlap() -> None:
    assert related_queries("e-mail setup", {"email setup": 5}) == ["email setup"]
    assert related_queries("js", {"javascript basics": 9}) == ["javascript basics"]
    assert related_queries("running", {"RUNNING shoes": 3}) == ["RUNNING shoes"]


def test_correct_spelling_per_token() -> None:
    assert correct_spelling("pythno djengo", ["python", "django"]) == "python django"
    # Whole-query path still wins when it matches.
    assert correct_spelling("runing", ["running", "trail"]) == "running"
    assert correct_spelling("zzz qqq", ["running", "trail"]) is None

"""Unit tests for the HTTP mapping core (integration plan step 1)."""

from openlens.providers.http_mapping import (
    MISSING,
    extract_path,
    map_payload_to_results,
)

FIELDS = {"id": "id", "title": "title", "snippet": "text", "url": "link"}


def test_extract_nested_paths() -> None:
    data = {"a": {"b": {"c": "deep"}}}
    assert extract_path(data, "a.b.c") == "deep"
    assert extract_path(data, "a.b") == {"c": "deep"}


def test_extract_list_indices() -> None:
    data = {"results": [{"id": "x"}, {"id": "y"}]}
    assert extract_path(data, "results.0.id") == "x"
    assert extract_path(data, "results.1.id") == "y"


def test_extract_missing_never_crashes() -> None:
    assert extract_path({"a": 1}, "a.b.c") is MISSING
    assert extract_path({"a": [1]}, "a.5") is MISSING
    assert extract_path({"a": [1]}, "a.nope") is MISSING
    assert extract_path(None, "a") is MISSING
    assert extract_path({"a": 1}, "") is MISSING
    assert extract_path({"a": 1}, None) is MISSING
    assert extract_path([1, 2], "0") == 1


def test_map_nested_fields_and_metadata() -> None:
    payload = {
        "data": {
            "hits": [
                {
                    "key": "1",
                    "head": {"t": "Title"},
                    "body": "Snippet",
                    "href": "http://x",
                }
            ]
        }
    }
    fields = {"id": "key", "title": "head.t", "snippet": "body", "url": "href"}
    results = map_payload_to_results(
        payload,
        results_path="data.hits",
        fields=fields,
        provider_name="myengine",
        engine_name="openlens",
        request_mode="web",
    )
    assert len(results) == 1
    hit = results[0]
    assert (hit.id, hit.title, hit.snippet, hit.url) == (
        "1",
        "Title",
        "Snippet",
        "http://x",
    )
    assert hit.original_score == hit.final_score == 1.0
    assert hit.provider == "myengine"
    assert hit.metadata["engine"] == "openlens"
    assert hit.metadata["request_mode"] == "web"


def test_map_missing_fields_skip_hits() -> None:
    payload = {
        "results": [
            {"id": "ok", "title": "T", "text": "S", "link": "http://ok"},
            {"id": "no-snippet", "title": "T"},
            {"title": "no-id", "text": "S"},
        ]
    }
    results = map_payload_to_results(payload, results_path="results", fields=FIELDS)
    assert [r.id for r in results] == ["ok"]


def test_map_rank_decay_default() -> None:
    payload = {"results": [{"id": str(i), "title": "T", "text": "S"} for i in range(3)]}
    results = map_payload_to_results(payload, results_path="results", fields=FIELDS)
    assert [r.original_score for r in results] == [1.0, 0.5, 1.0 / 3.0]
    assert all(r.original_score == r.final_score for r in results)


def test_map_numeric_score_field() -> None:
    payload = {
        "results": [
            {"id": "a", "title": "T", "text": "S", "relevance": 0.9},
            {"id": "b", "title": "T", "text": "S", "relevance": 0.1},
            {"id": "bad", "title": "T", "text": "S", "relevance": "n/a"},
            {"id": "missing", "title": "T", "text": "S"},
        ]
    }
    results = map_payload_to_results(
        payload, results_path="results", fields=FIELDS, score={"field": "relevance"}
    )
    assert [(r.id, r.original_score) for r in results] == [("a", 0.9), ("b", 0.1)]


def test_map_empty_payload_yields_empty() -> None:
    assert map_payload_to_results({}, results_path="results", fields=FIELDS) == []
    assert map_payload_to_results(None, results_path="results", fields=FIELDS) == []
    assert (
        map_payload_to_results(
            {"results": "not-a-list"}, results_path="results", fields=FIELDS
        )
        == []
    )
    assert (
        map_payload_to_results({"results": []}, results_path="results", fields=FIELDS)
        == []
    )
    assert (
        map_payload_to_results(
            {"other": []}, results_path="missing.path", fields=FIELDS
        )
        == []
    )

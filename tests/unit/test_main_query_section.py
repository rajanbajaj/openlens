"""Prod boot path: build_engine forwards the query: section to the engine."""

from __future__ import annotations

from openlens.main import build_engine
from openlens.retrieval.query_processor import QueryProcessor


def _config(**query_overrides: object) -> dict[str, object]:
    query: dict[str, object] = {
        "expansion": {"synonym_sets": {"apparel": ["dress", "gown"]}},
    }
    query.update(query_overrides)
    return {
        "providers": {
            "strategy": "single",
            "primary": "custom",
            "custom": {
                "enabled": True,
                "type": "custom_index",
                "db_path": ":memory:",
            },
        },
        "plugins": [],
        "ranking": {"default_profile": "default"},
        "query": query,
    }


def test_build_engine_forwards_query_section() -> None:
    """YAML query tuning reaches the engine's QueryProcessor (not defaults)."""
    engine = build_engine(_config())
    assert engine.query_processor.config.expansion.synonym_sets == {
        "apparel": ["dress", "gown"]
    }


def test_build_engine_without_query_section_uses_defaults() -> None:
    """Absent query: section still boots with code defaults."""
    config = _config()
    del config["query"]
    engine = build_engine(config)
    assert (
        engine.query_processor.config.expansion.synonym_sets
        == QueryProcessor().config.expansion.synonym_sets
    )

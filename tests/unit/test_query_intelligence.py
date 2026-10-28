"""Step-1 fixture tests: query-text normalization.

Covers unicode, mixed case, punctuation-only input, 1000-char input, and
over-cap input from a direct engine caller (no API wire cap assumed).
"""

import pytest

from openlens.core.interfaces import ConfigError
from openlens.models.fake import FakeModelProvider
from openlens.retrieval.query_expand import (
    DEFAULT_MAX_ADDED_TERMS,
    QueryExpander,
)
from openlens.retrieval.query_intent import (
    INTENT_TO_MODE,
    QueryIntentClassifier,
)
from openlens.retrieval.query_normalize import (
    DEFAULT_MAX_QUERY_LENGTH,
    QueryNormalizer,
    QueryNormalizerConfig,
    QueryTooLongError,
    normalize_query,
)
from openlens.retrieval.query_parser import parse_query
from openlens.retrieval.query_processor import (
    QueryProcessor,
    extract_query_section,
)

# (raw, expected normalized) fixture table.
FIXTURES = [
    ("  Hello   WORLD  ", "hello world"),
    ("Café Reviews", "café reviews"),
    ("café", "café"),  # combining accent composes under NFKC
    ("ﬁsh", "fish"),  # U+FB01 ligature expands under NFKC
    ("　ＡＢＣ　", "abc"),  # full-width spaces/letters fold to ASCII
    ("Hello,World!", "hello world"),  # punctuation splits, never merges
    ('python "exact" AND (fts5)', "python exact and fts5"),
    ("don't stop", "don t stop"),
    ("user@example.com", "user example com"),
    ("best dresses under 3000", "best dresses under 3000"),
    ("!!!", ""),
    ("   ", ""),
    ("a" * 1000, "a" * 1000),
]


@pytest.mark.parametrize(("raw", "expected"), FIXTURES)
def test_fixture_table(raw: str, expected: str) -> None:
    assert normalize_query(raw).normalized == expected


def test_punctuation_only_input_normalizes_to_empty() -> None:
    assert normalize_query("!!!...???").normalized == ""


def test_thousand_char_input_passes_untruncated() -> None:
    result = normalize_query("a" * DEFAULT_MAX_QUERY_LENGTH)
    assert result.normalized == "a" * DEFAULT_MAX_QUERY_LENGTH
    assert result.truncated is False


def test_over_cap_input_truncates_by_default() -> None:
    result = normalize_query("a" * (DEFAULT_MAX_QUERY_LENGTH + 500))
    assert len(result.normalized) <= DEFAULT_MAX_QUERY_LENGTH
    assert result.truncated is True


def test_over_cap_input_rejects_under_reject_policy() -> None:
    with pytest.raises(QueryTooLongError):
        normalize_query("a" * (DEFAULT_MAX_QUERY_LENGTH + 1), on_overflow="reject")


def test_non_string_input_raises_type_error() -> None:
    with pytest.raises(TypeError):
        normalize_query(None)  # type: ignore[arg-type]


def test_normalizer_class_honors_config_bound() -> None:
    normalizer = QueryNormalizer(
        QueryNormalizerConfig(max_length=10, on_overflow="reject")
    )
    assert normalizer.normalize("short").normalized == "short"
    with pytest.raises(QueryTooLongError):
        normalizer.normalize("this query is too long")


def test_normalize_then_parse_matches_raw_parse_on_ascii() -> None:
    # Parser-output stability: normalization must not change parse terms.
    for raw, _ in FIXTURES:
        if not raw.isascii():
            continue
        assert (
            parse_query(normalize_query(raw).normalized).terms == parse_query(raw).terms
        )


def test_normalize_then_parse_keeps_existing_parser_expectation() -> None:
    parsed = parse_query(normalize_query('python "exact" AND (fts5)').normalized)
    assert parsed.terms == ["python", "exact", "and", "fts5"]


# ---------------------------------------------------------------------------
# Steps 2-3: intent classification + expansion + QueryProcessor.
# ---------------------------------------------------------------------------

# (raw, normalized, intent, {added term: source set id}) fixture table.
INTENT_FIXTURES = [
    (
        "best dresses under 3000",
        "best dresses under 3000",
        "shopping",
        {"dress": "apparel", "gown": "apparel"},
    ),
    (
        "cheap flights to Paris",
        "cheap flights to paris",
        "shopping",  # tie: shopping > travel
        {
            "affordable": "pricing",
            "budget": "pricing",
            "airfare": "air-travel",
            "flight": "air-travel",
        },
    ),
    ("breaking election results today", "breaking election results today", "news", {}),
    ("how to bake sourdough bread", "how to bake sourdough bread", "informational", {}),
    ("Café Reviews", "café reviews", "informational", {}),
    ("login to my bank portal", "login to my bank portal", "navigational", {}),
    (
        "buy shoes online sale",
        "buy shoes online sale",
        "shopping",
        {"shoe": "footwear", "sneaker": "footwear", "sneakers": "footwear"},
    ),
    (
        "hotel booking in Tokyo",
        "hotel booking in tokyo",
        "travel",
        {"hotels": "lodging", "lodging": "lodging"},
    ),
    ("latest news headlines", "latest news headlines", "news", {}),
    (
        "what is the capital of France",
        "what is the capital of france",
        "informational",
        {},
    ),
    (
        "official python download page",
        "official python download page",
        "navigational",
        {},
    ),
    (
        "laptop price compare",
        "laptop price compare",
        "shopping",  # tie: shopping > informational
        {"laptops": "computing", "notebook": "computing"},
    ),
    (
        "tv reviews 2024",
        "tv reviews 2024",
        "informational",
        {"television": "television"},
    ),
    (
        "phone under 20000",
        "phone under 20000",
        "shopping",
        {"mobile": "mobile", "phones": "mobile", "smartphone": "mobile"},
    ),
    (
        "flights booking cheap",
        "flights booking cheap",
        "shopping",  # tie: shopping > travel
        {
            "airfare": "air-travel",
            "flight": "air-travel",
            "affordable": "pricing",
            "budget": "pricing",
        },
    ),
    (
        "shoes for running",
        "shoes for running",
        "shopping",
        {"shoe": "footwear", "sneaker": "footwear", "sneakers": "footwear"},
    ),
    (
        "dress sale today",
        "dress sale today",
        "shopping",  # tie: shopping > news
        {"dresses": "apparel", "gown": "apparel"},
    ),
    ("news about elections", "news about elections", "news", {}),
    (
        "travel guide to Italy",
        "travel guide to italy",
        "travel",  # tie: travel > informational
        {},
    ),
    ("how to fix a leaking faucet", "how to fix a leaking faucet", "informational", {}),
    ("!!!", "", "informational", {}),
    (
        "best hotel deals in Paris",
        "best hotel deals in paris",
        "shopping",  # tie: shopping > travel
        {"hotels": "lodging", "lodging": "lodging"},
    ),
    ("login page", "login page", "navigational", {}),
    ("election results", "election results", "news", {}),
]


@pytest.mark.parametrize(("raw", "normalized", "intent", "provenance"), INTENT_FIXTURES)
def test_intent_fixture_table(
    raw: str, normalized: str, intent: str, provenance: dict[str, str]
) -> None:
    processor = QueryProcessor.from_config({})
    result = processor.process(raw)
    assert result.normalized == normalized
    assert result.intent == intent
    assert result.suggested_mode == INTENT_TO_MODE[intent]
    assert result.provenance == provenance
    assert result.base_terms == normalized.split()
    # Suggestion recorded, effective mode follows it with no explicit mode.
    assert result.explicit_mode == "default"
    assert result.effective_mode == INTENT_TO_MODE[intent]
    # child-safe is never suggested.
    assert result.suggested_mode != "child-safe"


@pytest.mark.parametrize(("raw", "normalized", "intent", "provenance"), INTENT_FIXTURES)
def test_explicit_mode_passthrough(
    raw: str, normalized: str, intent: str, provenance: dict[str, str]
) -> None:
    """100% explicit-mode passthrough: explicit mode wins, suggestion kept."""
    processor = QueryProcessor.from_config({})
    for explicit in ("news", "child-safe", "shopping", "travel"):
        result = processor.process(raw, mode=explicit)
        assert result.effective_mode == explicit
        assert result.suggested_mode == INTENT_TO_MODE[intent]
        assert result.explicit_mode == explicit
        assert result.provenance == provenance


def test_expansion_term_cap_enforced() -> None:
    expander = QueryExpander()
    expander.configure({"max_added_terms": 2})
    expansion = expander.expand("phone shoes dress")
    assert len(expansion.added) == 2


def test_expansion_default_cap_bound() -> None:
    assert DEFAULT_MAX_ADDED_TERMS <= 8
    expander = QueryExpander()
    expander.configure({})
    expansion = expander.expand("phone shoes dress laptop tv flight hotel cheap")
    assert len(expansion.added) <= DEFAULT_MAX_ADDED_TERMS


def test_expansion_cap_is_config_tunable() -> None:
    expander = QueryExpander()
    expander.configure({"max_added_terms": 0})
    assert expander.expand("phone shoes").added == []


def test_semantic_hook_off_by_default() -> None:
    """With a backend attached but the hook disabled, rules decide."""
    processor = QueryProcessor.from_config({})
    processor.attach_model_backend(FakeModelProvider())
    result = processor.process("best dresses under 3000")
    assert result.intent == "shopping"
    assert all("semantic" not in reason for reason in result.reasons)


def test_semantic_hook_bounded_when_enabled() -> None:
    processor = QueryProcessor.from_config(
        {"intent": {"semantic_enabled": True, "semantic_timeout_s": 5.0}}
    )
    processor.attach_model_backend(FakeModelProvider())
    result = processor.process("best dresses under 3000")
    # Fake backend echoes first label found in text; "shopping" is absent so
    # it falls back to the first label ("navigational") — hook is bounded
    # and the pipeline still returns a valid shaped result.
    assert result.intent in ("navigational", "shopping")
    assert result.effective_mode == INTENT_TO_MODE[result.intent]


def test_tie_priority_is_deterministic() -> None:
    classifier = QueryIntentClassifier()
    classifier.configure({})
    # Matches both travel and shopping keyword sets.
    assert classifier.classify("cheap hotel").intent == "shopping"


def test_invalid_tie_priority_rejected() -> None:
    classifier = QueryIntentClassifier()
    with pytest.raises(ConfigError):
        classifier.configure({"tie_priority": ["shopping"]})


def test_unknown_explicit_mode_fails_loudly() -> None:
    processor = QueryProcessor.from_config({})
    with pytest.raises(ConfigError):
        processor.process("shoes", mode="no-such-mode")


def test_extract_query_section_defaults_to_empty() -> None:
    assert extract_query_section({}) == {}
    assert extract_query_section({"query": {"intent": {}}}) == {"intent": {}}


def test_classifier_sets_compiled_once_at_configure() -> None:
    classifier = QueryIntentClassifier()
    classifier.configure({})
    first = {k: set(v) for k, v in classifier._compiled.items()}
    classifier.classify("cheap flights")
    assert {k: set(v) for k, v in classifier._compiled.items()} == first

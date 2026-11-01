"""Coherent result descriptions: never cut mid-sentence (no server needed)."""

from openlens.core.interfaces import Document
from openlens.retrieval.normalizer import make_snippet, normalize


def test_snippet_keeps_whole_sentences() -> None:
    """A cut lands on a sentence boundary, not mid-sentence."""
    text = (
        "Manchester United lost two nil at Chelsea. "
        "Martinez was sent off in the second half for a rash challenge."
    )
    snippet = make_snippet(text, length=60)
    assert snippet == "Manchester United lost two nil at Chelsea."


def test_snippet_short_content_unchanged() -> None:
    assert make_snippet("Short and complete.", length=200) == "Short and complete."


def test_snippet_long_first_sentence_cuts_at_word_boundary() -> None:
    """One over-long sentence cuts between words (with ellipsis), never
    mid-word: the kept head is a prefix ending right before a space."""
    text = "The quick brown fox jumps over the lazy dog near the river bank."
    snippet = make_snippet(text, length=24)
    assert snippet.endswith("...")
    head = snippet[:-3]
    assert text.startswith(head)
    assert text[len(head)] == " "


def test_snippet_no_punctuation_cuts_at_word_boundary() -> None:
    text = "alpha beta gamma delta epsilon zeta eta theta iota kappa"
    snippet = make_snippet(text, length=20)
    assert snippet.endswith("...")
    head = snippet[:-3]
    assert text.startswith(head)
    assert text[len(head)] == " "


def test_snippet_empty_content() -> None:
    assert make_snippet("", length=200) == ""
    assert make_snippet("   ", length=200) == ""


def test_snippet_substantive_description_wins_over_content() -> None:
    description = (
        "A complete, substantive meta description that says what the page covers."
    )
    content = "Unrelated body prose that happens to mention zebras at length."
    assert make_snippet(content, description=description) == description


def test_snippet_long_description_truncates_on_sentence_boundary() -> None:
    description = (
        "First sentence of the meta description is self-contained. "
        "Second sentence adds detail that should not fit in sixty chars."
    )
    snippet = make_snippet("body", description=description, length=60)
    assert snippet == "First sentence of the meta description is self-contained."


def test_snippet_stub_description_falls_back_to_query_window() -> None:
    body = (
        "A long stretch of generic filler prose that says nothing in "
        "particular about anything at all, and then finally the rare "
        "zebracorn appears near the end of the passage for the test."
    )
    snippet = make_snippet(body, description="Products", query="zebracorn")
    assert "zebracorn" in snippet
    assert snippet.startswith("...")


def test_snippet_title_echo_description_falls_back_to_lead() -> None:
    content = "Lead sentence carries the meaning here. Trailing detail follows."
    snippet = make_snippet(
        content, description="Page Title Echo Words Here", query="missing"
    )
    assert snippet == content or snippet.startswith("Lead sentence")


def test_snippet_title_echo_description_is_not_substantive() -> None:
    content = "Body prose that never mentions the query term at all here."
    snippet = make_snippet(content, description="Body prose echo title", query="zzz")
    assert snippet == content


def test_snippet_query_window_without_description() -> None:
    body = (
        "A long stretch of generic filler prose that says nothing in "
        "particular about anything at all, and then finally the rare "
        "zebracorn appears near the end of the passage for the test."
    )
    snippet = make_snippet(body, query="zebracorn")
    assert "zebracorn" in snippet
    assert snippet.startswith("...")


def test_snippet_no_query_no_description_keeps_lead_sentences() -> None:
    text = (
        "Manchester United lost two nil at Chelsea. "
        "Martinez was sent off in the second half for a rash challenge."
    )
    assert make_snippet(text, length=60) == "Manchester United lost two nil at Chelsea."


def test_normalize_threads_metadata_description_and_query() -> None:
    doc = Document(
        id="d1",
        title="T",
        content="Body prose that never mentions the target word anywhere.",
        url="https://x.test/a",
        metadata={
            "description": (
                "A substantive meta description that names the zebracorn "
                "and explains the page in full sentences."
            )
        },
    )
    hit = normalize(doc, 1.0, "custom_index", query="zebracorn")
    assert "zebracorn" in hit.snippet
    assert hit.snippet.startswith("A substantive meta description")


def test_normalize_without_description_uses_query_window() -> None:
    doc = Document(
        id="d1",
        title="T",
        content=(
            "A long stretch of generic filler prose that says nothing in "
            "particular about anything at all, and then finally the rare "
            "zebracorn appears near the end of the passage for the test."
        ),
        url="https://x.test/a",
    )
    hit = normalize(doc, 1.0, "custom_index", query="zebracorn")
    assert "zebracorn" in hit.snippet
    assert hit.snippet.startswith("...")


def test_normalize_ignores_non_string_description() -> None:
    doc = Document(
        id="d1",
        title="T",
        content="Short body.",
        url="https://x.test/a",
        metadata={"description": 42},
    )
    assert normalize(doc, 1.0, "p").snippet == "Short body."

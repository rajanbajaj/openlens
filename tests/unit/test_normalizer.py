"""Coherent result descriptions: never cut mid-sentence (no server needed)."""

from openlens.retrieval.normalizer import make_snippet


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

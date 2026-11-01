"""Unit tests for retrieval.stance (model-first, keyword fallback)."""

from openlens.retrieval.stance import judge_stance


class _Script:
    def __init__(self, reply: str) -> None:
        self._reply = reply

    def complete(self, _prompt: str) -> str:
        return self._reply


def test_fallback_supports() -> None:
    verdict = judge_stance(
        "The trial succeeded",
        "The study confirms the trial succeeded and the data supports it.",
    )
    assert verdict.stance == "supports"
    assert verdict.evidence


def test_fallback_refutes() -> None:
    verdict = judge_stance(
        "The trial succeeded",
        "Officials denied the claim, calling it false and misleading.",
    )
    assert verdict.stance == "refutes"
    assert verdict.evidence


def test_fallback_neutral_on_boring_text() -> None:
    verdict = judge_stance(
        "The trial succeeded",
        "The committee met on Tuesday to discuss scheduling matters.",
    )
    assert verdict.stance == "neutral"


def test_empty_passage_is_neutral() -> None:
    assert judge_stance("Some claim", "").stance == "neutral"


def test_model_verdict_parsed() -> None:
    verdict = judge_stance(
        "The trial succeeded",
        "Some unrelated words here.",
        model=_Script("REFUTES\nOfficials called it false."),
    )
    assert verdict.stance == "refutes"
    assert "false" in verdict.evidence


def test_model_garbage_falls_back() -> None:
    verdict = judge_stance(
        "The trial succeeded",
        "The study confirms the trial succeeded.",
        model=_Script("MAYBE, HARD TO SAY"),
    )
    assert verdict.stance == "supports"

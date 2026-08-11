from __future__ import annotations

from a6api_auto.probe_prompts import (
    SHORT_PROBE_QUESTION_MAX_LENGTH,
    SHORT_PROBE_QUESTION_MIN_LENGTH,
    SHORT_PROBE_QUESTION_TYPE_COUNT,
    build_short_probe_question,
)


def test_short_probe_question_bank_is_compact_and_varied() -> None:
    questions = tuple(build_short_probe_question() for _ in range(256))

    assert SHORT_PROBE_QUESTION_TYPE_COUNT >= 40
    assert len(set(questions)) >= 245
    assert all(
        SHORT_PROBE_QUESTION_MIN_LENGTH <= len(question) <= SHORT_PROBE_QUESTION_MAX_LENGTH
        for question in questions
    )
    assert all(question.endswith("?") for question in questions)
    assert all("Answer briefly" not in question for question in questions)

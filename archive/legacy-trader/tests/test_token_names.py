from __future__ import annotations

from pathlib import Path

import pytest

from a6api_auto.token_names import (
    CORPUS_TOKEN_NAME_PATTERN,
    TOKEN_NAME_ALPHABET,
    TOKEN_NAME_INITIAL_ALPHABET,
    TOKEN_NAME_LENGTH,
    generate_anonymous_token_name,
    is_anonymous_token_name,
    is_identity_linked_token_name,
    token_name_corpus,
)
from a6api_auto.workflow import allocate_session


def test_anonymous_token_name_uses_corpus_and_injected_choice() -> None:
    calls: list[tuple[str, ...]] = []

    def choose(options) -> str:
        calls.append(tuple(options))
        return options[-1]

    value = generate_anonymous_token_name(choice=choose)

    assert value == token_name_corpus()[-1]
    assert is_anonymous_token_name(value)
    assert calls == [token_name_corpus()]


def test_token_name_corpus_is_large_varied_and_generic() -> None:
    corpus = token_name_corpus()

    assert len(corpus) >= 200
    assert len(set(corpus)) == len(corpus)
    assert all(CORPUS_TOKEN_NAME_PATTERN.fullmatch(value) for value in corpus)
    assert {
        "gpt",
        "GPT",
        "Codex",
        "claude code",
        "OpenAI",
        "deepseek",
        "GLM",
        "KIMI",
        "mykey",
        "API",
        "opencode",
        "reasonix",
        "harness",
        "LLM",
        "agent",
        "loop",
    } <= set(corpus)


def test_legacy_random_token_name_remains_recognized() -> None:
    legacy = TOKEN_NAME_INITIAL_ALPHABET[-1] + TOKEN_NAME_ALPHABET[-1] * (
        TOKEN_NAME_LENGTH - 1
    )

    assert is_anonymous_token_name(legacy)


def test_token_name_selector_must_return_a_corpus_entry() -> None:
    with pytest.raises(ValueError, match="anonymous_token_name_entropy_source_invalid"):
        generate_anonymous_token_name(choice=lambda _options: "merchant-7")


def test_identity_linked_name_detection_is_narrow_and_case_insensitive() -> None:
    assert is_identity_linked_token_name("merchant-deadbeef")
    assert is_identity_linked_token_name(" Merchant-DEADBEEF ")
    assert not is_identity_linked_token_name("callback")
    assert not is_identity_linked_token_name("key-random")


def test_new_session_uses_anonymous_token_name(tmp_path: Path) -> None:
    qq_file = tmp_path / "qq.txt"
    qq_file.write_text("fixture@qq.com\n")

    record, _store, _template = allocate_session(qq_file, tmp_path / "state")

    assert is_anonymous_token_name(record.token_name)
    assert record.token_name in token_name_corpus()
    lowered = record.token_name.casefold()
    assert all(term not in lowered for term in ("merchant", "fixture", "qq", "@"))

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.generate_callback_config import (
    CALLBACK_TEXT,
    MAX_PROMPT_TOKENS,
    PROMPT_BANDS,
    build_callback_settings,
    generate_usage_samples,
    prompt_band_counts,
)


def test_callback_usage_samples_have_exact_mixed_distribution() -> None:
    samples = generate_usage_samples()

    assert len(samples) == 1000
    assert prompt_band_counts(samples) == {
        name: count for name, _, _, count in PROMPT_BANDS
    }
    assert max(sample["prompt_tokens"] for sample in samples) <= MAX_PROMPT_TOKENS
    assert max(sample["completion_tokens"] for sample in samples) <= 2048
    assert sum(sample["prompt_tokens"] for sample in samples) / len(samples) < 50_000
    assert sum(sample["prompt_tokens"] >= 65_536 for sample in samples) == 200
    assert all(
        sample["cached_tokens"] / sample["prompt_tokens"] >= 0.8 for sample in samples
    )
    assert all(
        sample["cached_tokens"] + sample["cache_creation_tokens"]
        <= sample["prompt_tokens"]
        for sample in samples
    )
    assert len(
        {
            (
                sample["prompt_tokens"],
                sample["completion_tokens"],
                sample["cached_tokens"],
                sample["cache_creation_tokens"],
            )
            for sample in samples
        }
    ) >= 900


def test_callback_settings_are_callback_only_and_responses_compatible() -> None:
    settings = build_callback_settings(generate_usage_samples())

    assert settings["enabled"] is True
    assert [rule["id"] for rule in settings["rules"]] == ["callback-0001"]
    rule = settings["rules"][0]
    assert rule["endpoint"] == "responses"
    assert rule["response_delay_ms"] == 800
    assert rule["stream_max_duration_ms"] == 120_000
    assert rule["json_conditions"][-1]["value"] == CALLBACK_TEXT
    assert "PING" not in json.dumps(settings)
    assert "PONG" not in json.dumps(settings)
    assert "{{usage_prompt_tokens}}" in rule["sync_response_body"]
    assert rule["stream_response_body"].count("event: response.") == 9

# ruff: noqa: E501 - SSE data lines must remain single-line JSON frames.

from __future__ import annotations

import argparse
import json
import random
from collections import Counter
from pathlib import Path
from typing import Any

CALLBACK_TEXT = "CALLBACK 5db98307ce2d442ee568baacf8b0e7076fa657cbe2806ccff9da6386997915e2"
DEFAULT_SEED = 0xA6CB0001
SAMPLE_COUNT = 1000
MAX_PROMPT_TOKENS = 131_071

PROMPT_BANDS = (
    ("4k-16k", 4_096, 16_383, 100),
    ("16k-32k", 16_384, 32_767, 400),
    ("32k-64k", 32_768, 65_535, 300),
    ("64k-96k", 65_536, 98_303, 150),
    ("96k-128k", 98_304, MAX_PROMPT_TOKENS, 50),
)

COMPLETION_BANDS = (
    (32, 160, 0.65),
    (161, 512, 0.25),
    (513, 1_024, 0.08),
    (1_025, 2_048, 0.02),
)

SYNC_RESPONSE_BODY = (
    '{"id":"{{response_id}}","object":"response","created_at":{{unix}},'
    '"status":"completed","model":"{{model}}","output":[{"id":"msg_{{response_id}}",'
    '"type":"message","status":"completed","role":"assistant","content":[{"type":'
    '"output_text","annotations":[],"logprobs":[],"text":"CALLBACK"}]}],'
    '"parallel_tool_calls":true,"tool_choice":"auto","tools":[],"usage":{'
    '"input_tokens":{{usage_prompt_tokens}},"input_tokens_details":{'
    '"cached_tokens":{{usage_cached_tokens}},"cache_creation_tokens":'
    '{{usage_cache_creation_tokens}}},"output_tokens":{{usage_completion_tokens}},'
    '"total_tokens":{{usage_total_tokens}}}}'
)

STREAM_RESPONSE_BODY = """event: response.created
data: {"type":"response.created","sequence_number":0,"response":{"id":"{{response_id}}","object":"response","created_at":{{unix}},"status":"in_progress","model":"{{model}}","output":[]}}

event: response.in_progress
data: {"type":"response.in_progress","sequence_number":1,"response":{"id":"{{response_id}}","object":"response","created_at":{{unix}},"status":"in_progress","model":"{{model}}","output":[]}}

event: response.output_item.added
data: {"type":"response.output_item.added","sequence_number":2,"output_index":0,"item":{"id":"msg_{{response_id}}","type":"message","status":"in_progress","role":"assistant","content":[]}}

event: response.content_part.added
data: {"type":"response.content_part.added","sequence_number":3,"item_id":"msg_{{response_id}}","output_index":0,"content_index":0,"part":{"type":"output_text","annotations":[],"logprobs":[],"text":""}}

event: response.output_text.delta
data: {"type":"response.output_text.delta","sequence_number":4,"item_id":"msg_{{response_id}}","output_index":0,"content_index":0,"delta":"CALLBACK","logprobs":[]}

event: response.output_text.done
data: {"type":"response.output_text.done","sequence_number":5,"item_id":"msg_{{response_id}}","output_index":0,"content_index":0,"text":"CALLBACK","logprobs":[]}

event: response.content_part.done
data: {"type":"response.content_part.done","sequence_number":6,"item_id":"msg_{{response_id}}","output_index":0,"content_index":0,"part":{"type":"output_text","annotations":[],"logprobs":[],"text":"CALLBACK"}}

event: response.output_item.done
data: {"type":"response.output_item.done","sequence_number":7,"output_index":0,"item":{"id":"msg_{{response_id}}","type":"message","status":"completed","role":"assistant","content":[{"type":"output_text","annotations":[],"logprobs":[],"text":"CALLBACK"}]}}

event: response.completed
data: {"type":"response.completed","sequence_number":8,"response":{"id":"{{response_id}}","object":"response","created_at":{{unix}},"status":"completed","model":"{{model}}","output":[{"id":"msg_{{response_id}}","type":"message","status":"completed","role":"assistant","content":[{"type":"output_text","annotations":[],"logprobs":[],"text":"CALLBACK"}]}],"parallel_tool_calls":true,"tool_choice":"auto","tools":[],"usage":{"input_tokens":{{usage_prompt_tokens}},"input_tokens_details":{"cached_tokens":{{usage_cached_tokens}},"cache_creation_tokens":{{usage_cache_creation_tokens}}},"output_tokens":{{usage_completion_tokens}},"total_tokens":{{usage_total_tokens}}}}
"""


def _completion_tokens(rng: random.Random) -> int:
    marker = rng.random()
    cumulative = 0.0
    for lower, upper, weight in COMPLETION_BANDS:
        cumulative += weight
        if marker < cumulative:
            return rng.randint(lower, upper)
    lower, upper, _ = COMPLETION_BANDS[-1]
    return rng.randint(lower, upper)


def _sample_for_prompt(rng: random.Random, prompt_tokens: int) -> dict[str, int]:
    target_cached = int(prompt_tokens * rng.uniform(0.82, 0.985))
    cached_tokens = max(128, (target_cached // 128) * 128)
    while cached_tokens / prompt_tokens < 0.8:
        cached_tokens += 128
    cached_tokens = min(cached_tokens, prompt_tokens)

    remaining = prompt_tokens - cached_tokens
    cache_creation_tokens = 0
    if remaining >= 128 and rng.random() < 0.15:
        max_blocks = max(1, min(remaining // 128, 16))
        cache_creation_tokens = rng.randint(1, max_blocks) * 128

    return {
        "cached_tokens": cached_tokens,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": _completion_tokens(rng),
        "cache_creation_tokens": cache_creation_tokens,
    }


def generate_usage_samples(seed: int = DEFAULT_SEED) -> list[dict[str, int]]:
    rng = random.Random(seed)
    samples: list[dict[str, int]] = []
    seen: set[tuple[int, int, int, int]] = set()
    for _, lower, upper, count in PROMPT_BANDS:
        while count > 0:
            sample = _sample_for_prompt(rng, rng.randint(lower, upper))
            shape = (
                sample["prompt_tokens"],
                sample["completion_tokens"],
                sample["cached_tokens"],
                sample["cache_creation_tokens"],
            )
            if shape in seen:
                continue
            seen.add(shape)
            samples.append(sample)
            count -= 1
    rng.shuffle(samples)
    validate_usage_samples(samples)
    return samples


def prompt_band_counts(samples: list[dict[str, int]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for sample in samples:
        prompt = sample["prompt_tokens"]
        for name, lower, upper, _ in PROMPT_BANDS:
            if lower <= prompt <= upper:
                counts[name] += 1
                break
    return dict(counts)


def validate_usage_samples(samples: list[dict[str, int]]) -> None:
    if len(samples) != SAMPLE_COUNT:
        raise ValueError(f"expected {SAMPLE_COUNT} usage samples, got {len(samples)}")
    expected_counts = {name: count for name, _, _, count in PROMPT_BANDS}
    if prompt_band_counts(samples) != expected_counts:
        raise ValueError("prompt-token band counts do not match the configured distribution")

    distinct: set[tuple[int, int, int, int]] = set()
    for index, sample in enumerate(samples):
        prompt = sample["prompt_tokens"]
        completion = sample["completion_tokens"]
        cached = sample["cached_tokens"]
        cache_creation = sample["cache_creation_tokens"]
        if not 0 < prompt <= MAX_PROMPT_TOKENS:
            raise ValueError(f"sample[{index}] prompt_tokens is outside the limit")
        if not 0 < completion <= 2_048:
            raise ValueError(f"sample[{index}] completion_tokens is outside the limit")
        if cached <= 0 or cached / prompt < 0.8:
            raise ValueError(f"sample[{index}] cached/input ratio is below 0.80")
        if cache_creation < 0 or cached + cache_creation > prompt:
            raise ValueError(f"sample[{index}] cache token details exceed prompt_tokens")
        distinct.add((prompt, completion, cached, cache_creation))
    if len(distinct) < 900:
        raise ValueError(f"only {len(distinct)} distinct usage shapes were generated")


def build_callback_settings(samples: list[dict[str, int]]) -> dict[str, Any]:
    validate_usage_samples(samples)
    return {
        "enabled": True,
        "rules": [
            {
                "id": "callback-0001",
                "name": "callback-0001",
                "enabled": True,
                "endpoint": "responses",
                "match_mode": "all",
                "json_conditions": [
                    {"path": "input", "operator": "array_length_equals", "value": "1"},
                    {"path": "input.0.type", "operator": "equals", "value": "message"},
                    {"path": "input.0.role", "operator": "equals", "value": "user"},
                    {
                        "path": "input.0.content",
                        "operator": "array_length_equals",
                        "value": "1",
                    },
                    {
                        "path": "input.0.content.0.type",
                        "operator": "equals",
                        "value": "input_text",
                    },
                    {
                        "path": "input.0.content.0.text",
                        "operator": "equals",
                        "value": CALLBACK_TEXT,
                    },
                ],
                "http_status": 200,
                "response_delay_ms": 800,
                "stream_max_duration_ms": 120_000,
                "sync_content_type": "application/json; charset=utf-8",
                "stream_content_type": "text/event-stream; charset=utf-8",
                "sync_response_body": SYNC_RESPONSE_BODY,
                "stream_response_body": STREAM_RESPONSE_BODY,
                "usage_samples": samples,
            }
        ],
    }


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate callback-0001 usage/config payloads")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--samples-out",
        type=Path,
        default=Path("deploy/callback-usage-samples.json"),
    )
    parser.add_argument(
        "--settings-out",
        type=Path,
        default=Path("deploy/callback-probe-settings.json"),
    )
    args = parser.parse_args()

    samples = generate_usage_samples(args.seed)
    settings = build_callback_settings(samples)
    _write_json(args.samples_out, samples)
    _write_json(args.settings_out, settings)

    prompts = sorted(sample["prompt_tokens"] for sample in samples)
    mean_prompt = sum(prompts) / len(prompts)
    summary = {
        "samples": len(samples),
        "distinct": len(
            {
                (
                    item["prompt_tokens"],
                    item["completion_tokens"],
                    item["cached_tokens"],
                    item["cache_creation_tokens"],
                )
                for item in samples
            }
        ),
        "bands": prompt_band_counts(samples),
        "prompt_min": prompts[0],
        "prompt_p50": prompts[len(prompts) // 2],
        "prompt_mean": round(mean_prompt, 2),
        "prompt_max": prompts[-1],
        "completion_max": max(item["completion_tokens"] for item in samples),
    }
    print(json.dumps(summary, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

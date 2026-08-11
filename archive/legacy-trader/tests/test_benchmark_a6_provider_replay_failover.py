from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "benchmark_a6_provider_replay_failover.py"
SPEC = importlib.util.spec_from_file_location("benchmark_a6_provider_replay_failover", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_select_strict_pair_requires_exact_a_then_b_with_same_hash() -> None:
    event = MODULE.FixtureEvent
    pair = MODULE._select_strict_pair(
        [event("b", "hash", 200), event("a", "hash", 100)]
    )
    assert pair is not None
    assert [item.route for item in pair] == ["a", "b"]
    assert MODULE._select_strict_pair([event("b", "hash", 100)]) is None
    assert MODULE._select_strict_pair(
        [event("a", "first", 100), event("b", "second", 200)]
    ) is None


def test_percentile_uses_nearest_rank() -> None:
    values = [float(value) for value in range(1, 21)]
    assert MODULE._percentile(values, 0.50) == 10.0
    assert MODULE._percentile(values, 0.95) == 19.0


def test_provider_a_reset_requires_exact_a_success() -> None:
    result = MODULE.RequestResult(200, b'{"text":"reset-from-a"}', 0.1)
    event = MODULE.FixtureEvent("a", "hash", 100)

    assert MODULE._provider_a_reset_succeeded(result, [event]) is True
    assert MODULE._provider_a_reset_succeeded(result, [event, event]) is False
    assert MODULE._provider_a_reset_succeeded(
        MODULE.RequestResult(200, b'{"text":"winner-from-b"}', 0.1),
        [event],
    ) is False

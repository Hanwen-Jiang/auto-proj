from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from a6api_auto.trade_repository import PostgresTradeRepository


class RecordingCursor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def execute(self, query: str, params: Any = None) -> None:
        self.calls.append((" ".join(query.split()), tuple(params or ())))


def test_collector_preserves_first_pin_confirmation_time() -> None:
    cursor = RecordingCursor()
    observed_at = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
    batch = SimpleNamespace(
        pins=(
            SimpleNamespace(
                token_id=14027,
                model_name="gpt-5.6-sol",
                channel_id=963,
                fallback_to_smart_routing=False,
            ),
        )
    )

    PostgresTradeRepository._reconcile_pins(
        cursor,
        batch,
        {14027: 3},
        observed_at,
    )

    confirmation = next(query for query, _params in cursor.calls if "SET confirmed_at" in query)
    assert "SET confirmed_at = coalesce(confirmed_at, %s)" in confirmation
    assert "decision_id = coalesce(decision_id" in confirmation
    assert "AND (confirmed_at IS NULL OR decision_id IS NULL)" in confirmation
    assert "GREATEST(confirmed_at" not in confirmation

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from a6api_auto.trade_economic_facts import _quota_progress_regressed
from a6api_auto.trade_economic_source import SourceQuotaSnapshot

NOW = datetime(2026, 8, 4, tzinfo=timezone.utc)


def quota(used: str, total: str, **changes: object) -> SourceQuotaSnapshot:
    used_a = Decimal(used)
    total_a = Decimal(total)
    values = {
        "snapshot_id": 1,
        "source_id": 1,
        "contract_id": 1,
        "quota_epoch": "week-1",
        "used_a": used_a,
        "remaining_a": total_a - used_a,
        "total_a": total_a,
        "reset_at": NOW + timedelta(days=7),
        "observed_at": NOW,
        "payload_hash": "a" * 64,
    }
    values.update(changes)
    return SourceQuotaSnapshot(**values)


def test_total_recalibration_compares_utilization_instead_of_absolute_used_a() -> None:
    latest = quota("30", "100")
    lower_absolute_used = quota("29", "90", snapshot_id=2)

    assert (
        _quota_progress_regressed(
            latest,
            lower_absolute_used,
            allow_total_change=True,
        )
        is False
    )
    assert (
        _quota_progress_regressed(
            latest,
            lower_absolute_used,
            allow_total_change=False,
        )
        is True
    )


def test_total_recalibration_still_rejects_utilization_regression() -> None:
    latest = quota("30", "100")
    regressed = quota("26", "90", snapshot_id=2)

    assert _quota_progress_regressed(latest, regressed, allow_total_change=True) is True


def test_compute_channel_net_k_cny_basic() -> None:
    from decimal import Decimal

    from a6api_auto.trade_economic_facts import compute_channel_net_k_cny

    k = compute_channel_net_k_cny(Decimal("10"), 100_000, fx_cny_per_usd=Decimal("7.2"))
    assert k == Decimal("0.72")


def test_compute_channel_net_k_cny_rounding_and_zero() -> None:
    import pytest

    from a6api_auto.trade_economic_facts import compute_channel_net_k_cny
    from a6api_auto.trade_economic_source import EconomicRoutingError

    k = compute_channel_net_k_cny("0.005", 1_000, fx_cny_per_usd="7.2")
    assert k == Decimal("0.036")
    with pytest.raises(EconomicRoutingError):
        compute_channel_net_k_cny("0", 100, fx_cny_per_usd="7.2")
    with pytest.raises(EconomicRoutingError):
        compute_channel_net_k_cny("1", 0, fx_cny_per_usd="7.2")
    with pytest.raises(EconomicRoutingError):
        compute_channel_net_k_cny("1", 100, fx_cny_per_usd="0")


def test_reconciler_records_one_snapshot_per_billed_channel() -> None:
    from datetime import datetime, timezone
    from decimal import Decimal

    from a6api_auto.trade_economic_facts import (
        PostgresChannelRevenueReconciler,
        compute_channel_net_k_cny,
    )

    now = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)

    class FakeCursor:
        def __init__(self) -> None:
            self.executed = []

        def __enter__(self) -> FakeCursor:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def execute(self, sql: str, params: tuple[object, ...] | None = None) -> None:
            self.executed.append((sql, params))

        def fetchall(self) -> list[tuple[object, ...]]:
            market_query = "avg(" in self.executed[-1][0] and "market_fx" in self.executed[-1][0]
            if market_query:
                return []
            return [
                (55, 1, 101, Decimal("10.50"), 150_000),
                (58, 4, 102, Decimal("0.25"), 2_000),
            ]

    class FakeConnection:
        def __init__(self) -> None:
            self.cursor_holder = FakeCursor()
            self.closed = False

        def cursor(self) -> FakeCursor:
            return self.cursor_holder

        def close(self) -> None:
            self.closed = True

    connection = FakeConnection()
    recorded: list[dict[str, object]] = []

    class FakeStore:
        def record_channel_revenue(self, **kwargs: object) -> None:
            recorded.append(kwargs)

    reconciler = PostgresChannelRevenueReconciler(
        lambda: connection,  # type: ignore[arg-type]
        store=FakeStore(),  # type: ignore[arg-type]
    )
    result = reconciler.reconcile("gpt-5.6-sol", now=now)
    assert result["channel_count"] == 2
    assert connection.closed
    assert len(recorded) == 2
    first = recorded[0]
    assert first["channel_id"] == 101
    assert first["enodot_api_key_id"] == 55
    assert first["channel_net_k_cny"] == compute_channel_net_k_cny(
        Decimal("10.50"), 150_000, fx_cny_per_usd=Decimal("7.2")
    )
    assert first["settlement_contract_ref"] == "sub2api-usage-settlement-v1"
    assert first["controlled_listing_refs"] == ["a6trader:seller:merchant-1"]
    second = recorded[1]
    assert second["channel_id"] == 102
    assert second["enodot_api_key_id"] == 58


def test_reconciler_prefers_market_derived_fx_over_configured() -> None:
    from datetime import datetime, timezone
    from decimal import Decimal

    from a6api_auto.trade_economic_facts import PostgresChannelRevenueReconciler

    now = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)

    class FakeCursor:
        def __init__(self) -> None:
            self.executed = []

        def __enter__(self) -> FakeCursor:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def execute(self, sql: str, params: tuple[object, ...] | None = None) -> None:
            self.executed.append((sql, params))

        def fetchall(self) -> list[tuple[object, ...]]:
            if "market_fx" in self.executed[-1][0]:
                return [(Decimal("6.7526"),)]
            return [(55, 1, 101, Decimal("10.50"), 150_000)]

    class FakeConnection:
        def __init__(self) -> None:
            self.cursor_holder = FakeCursor()
            self.closed = False

        def cursor(self) -> FakeCursor:
            return self.cursor_holder

        def close(self) -> None:
            self.closed = True

    recorded: list[dict[str, object]] = []

    class FakeStore:
        def record_channel_revenue(self, **kwargs: object) -> None:
            recorded.append(kwargs)

    reconciler = PostgresChannelRevenueReconciler(
        lambda: FakeConnection(),  # type: ignore[arg-type]
        store=FakeStore(),  # type: ignore[arg-type]
    )
    result = reconciler.reconcile(
        "gpt-5.6-sol",
        now=now,
        fx_cny_per_usd=Decimal("7.2"),
        usage_window_seconds=900,
    )
    assert result["channel_count"] == 1
    assert recorded[0]["source_payload"]["fx_source"] == "market_quotes"
    assert recorded[0]["source_payload"]["fx_cny_per_usd"] == "6.7526"
    expected = Decimal("10.50") * Decimal("6.7526") / (Decimal(150_000) / Decimal(1000))
    assert recorded[0]["channel_net_k_cny"] == expected.quantize(Decimal("0.000000001"))

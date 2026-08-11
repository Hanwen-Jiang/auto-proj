from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from a6api_auto.trade_profit_settings import (
    ModelProfitSetting,
    PostgresModelProfitSettingStore,
    ProfitSettingError,
    profit_percent_to_bps,
)


class ProfitCursor:
    def __init__(self, row=None) -> None:
        self.row = row
        self.queries: list[tuple[str, tuple[object, ...]]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def execute(self, query: str, params: tuple[object, ...]) -> None:
        self.queries.append((query, params))
        if "INSERT INTO" in query:
            self.row = (
                params[0],
                params[1],
                datetime(2026, 8, 4, 5, 0, tzinfo=timezone.utc),
                params[2],
            )

    def fetchone(self):
        return self.row


class ProfitConnection:
    def __init__(self, row=None) -> None:
        self.cursor_value = ProfitCursor(row)
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return self.cursor_value

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


def test_sixty_percent_maps_to_bps_and_requested_gross_floor() -> None:
    setting = ModelProfitSetting("gpt-5.6-sol", profit_percent_to_bps("60"))

    assert setting.minimum_profit_on_cost_bps == 6000
    assert setting.margin == Decimal("0.6")
    assert setting.public()["profit_percent"] == "60"
    assert setting.public()["net_multiplier"] == "1.6"
    assert setting.public()["gross_listing_multiplier"] == "1.92"


@pytest.mark.parametrize("value", [True, None, "-1", "1000.01", "60.001", "nan"])
def test_profit_percent_rejects_invalid_or_overprecise_values(value: object) -> None:
    with pytest.raises(ProfitSettingError, match="^profit_percent_"):
        profit_percent_to_bps(value)


def test_postgres_profit_setting_store_upserts_and_reads_exact_value() -> None:
    write_connection = ProfitConnection()
    written = PostgresModelProfitSettingStore(lambda: write_connection).put(
        "gpt-5.6-sol",
        6000,
        updated_by="dashboard",
    )

    assert written.minimum_profit_on_cost_bps == 6000
    assert written.gross_listing_multiplier == Decimal("1.92")
    assert write_connection.committed is True
    assert write_connection.closed is True
    assert write_connection.cursor_value.queries[0][1] == (
        "gpt-5.6-sol",
        6000,
        "dashboard",
    )

    read_connection = ProfitConnection(
        ("gpt-5.6-sol", 6000, "2026-08-04T05:00:00+00:00", "dashboard")
    )
    loaded = PostgresModelProfitSettingStore(lambda: read_connection).get("gpt-5.6-sol")

    assert loaded is not None
    assert loaded.profit_percent == 60
    assert read_connection.closed is True

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

DEFAULT_PROFIT_ON_COST_BPS = 6_000
MAX_PROFIT_ON_COST_BPS = 100_000
A6_GROSS_LISTING_FACTOR = Decimal("1.2")


class ProfitSettingError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ModelProfitSetting:
    model: str
    minimum_profit_on_cost_bps: int
    updated_at: datetime | str | None = None
    updated_by: str = "unknown"

    def __post_init__(self) -> None:
        model = _model(self.model)
        bps = _profit_bps(self.minimum_profit_on_cost_bps)
        updated_by = str(self.updated_by or "").strip()
        if not updated_by or len(updated_by) > 128:
            raise ProfitSettingError("profit_setting_updated_by_invalid")
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "minimum_profit_on_cost_bps", bps)
        object.__setattr__(self, "updated_by", updated_by)

    @property
    def profit_percent(self) -> Decimal:
        return Decimal(self.minimum_profit_on_cost_bps) / Decimal(100)

    @property
    def net_multiplier(self) -> Decimal:
        return Decimal(1) + Decimal(self.minimum_profit_on_cost_bps) / Decimal(10_000)

    @property
    def gross_listing_multiplier(self) -> Decimal:
        return self.net_multiplier * A6_GROSS_LISTING_FACTOR

    @property
    def margin(self) -> Decimal:
        return Decimal(self.minimum_profit_on_cost_bps) / Decimal(10_000)

    def public(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "profit_percent": _decimal_text(self.profit_percent),
            "minimum_profit_on_cost_bps": self.minimum_profit_on_cost_bps,
            "net_multiplier": _decimal_text(self.net_multiplier),
            "gross_listing_multiplier": _decimal_text(self.gross_listing_multiplier),
            "updated_at": _timestamp_text(self.updated_at),
            "updated_by": self.updated_by,
        }


class PostgresModelProfitSettingStore:
    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        self.connection_factory = connection_factory

    def get(self, model: str) -> ModelProfitSetting | None:
        normalized_model = _model(model)
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT model, minimum_profit_on_cost_bps, updated_at, updated_by
                    FROM a6_trade.model_profit_settings
                    WHERE model = %s
                    """,
                    (normalized_model,),
                )
                row = cursor.fetchone()
        finally:
            _close(connection)
        return None if row is None else _setting_from_row(row)

    def put(
        self,
        model: str,
        minimum_profit_on_cost_bps: int,
        *,
        updated_by: str,
    ) -> ModelProfitSetting:
        normalized_model = _model(model)
        bps = _profit_bps(minimum_profit_on_cost_bps)
        actor = str(updated_by or "").strip()
        if not actor or len(actor) > 128:
            raise ProfitSettingError("profit_setting_updated_by_invalid")
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO a6_trade.model_profit_settings (
                        model, minimum_profit_on_cost_bps, updated_at, updated_by
                    )
                    VALUES (%s, %s, now(), %s)
                    ON CONFLICT (model) DO UPDATE SET
                        minimum_profit_on_cost_bps = EXCLUDED.minimum_profit_on_cost_bps,
                        updated_at = now(),
                        updated_by = EXCLUDED.updated_by
                    RETURNING model, minimum_profit_on_cost_bps, updated_at, updated_by
                    """,
                    (normalized_model, bps, actor),
                )
                row = cursor.fetchone()
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)
        if row is None:
            raise ProfitSettingError("profit_setting_write_readback_failed")
        return _setting_from_row(row)


def profit_percent_to_bps(value: object) -> int:
    if isinstance(value, bool) or value is None:
        raise ProfitSettingError("profit_percent_invalid")
    try:
        percent = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise ProfitSettingError("profit_percent_invalid") from None
    if not percent.is_finite():
        raise ProfitSettingError("profit_percent_invalid")
    scaled = percent * Decimal(100)
    if scaled != scaled.to_integral_value():
        raise ProfitSettingError("profit_percent_precision_invalid")
    try:
        return _profit_bps(int(scaled))
    except ProfitSettingError:
        raise ProfitSettingError("profit_percent_invalid") from None


def _setting_from_row(row: object) -> ModelProfitSetting:
    if isinstance(row, Mapping):
        values = (
            row.get("model"),
            row.get("minimum_profit_on_cost_bps"),
            row.get("updated_at"),
            row.get("updated_by"),
        )
    elif isinstance(row, (tuple, list)) and len(row) >= 4:
        values = (row[0], row[1], row[2], row[3])
    else:
        raise ProfitSettingError("profit_setting_readback_invalid")
    return ModelProfitSetting(
        model=str(values[0] or ""),
        minimum_profit_on_cost_bps=_profit_bps(values[1]),
        updated_at=values[2],
        updated_by=str(values[3] or ""),
    )


def _model(value: object) -> str:
    import re

    model = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,95}", model):
        raise ProfitSettingError("profit_setting_model_invalid")
    return model


def _profit_bps(value: object) -> int:
    if isinstance(value, bool):
        raise ProfitSettingError("profit_setting_bps_invalid")
    try:
        bps = int(value)
    except (TypeError, ValueError):
        raise ProfitSettingError("profit_setting_bps_invalid") from None
    if str(value).strip() != str(bps) or not 0 <= bps <= MAX_PROFIT_ON_COST_BPS:
        raise ProfitSettingError("profit_setting_bps_invalid")
    return bps


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _timestamp_text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    text = str(value).strip()
    return text or None


def _close(connection: Any) -> None:
    close = getattr(connection, "close", None)
    if callable(close):
        close()

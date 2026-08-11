from __future__ import annotations

from decimal import Decimal

import pytest

from scripts.reconcile_a6_panel_profit_gates import _profit_rate


def test_profit_rate_6000_bps_matches_0_92() -> None:
    # 6000bps 净利 → 1.6 倍；×1.2 毛因子 → 1.92 → profit rate 0.92
    assert _profit_rate(6_000) == Decimal("0.92")


def test_profit_rate_2000_bps_matches_0_44() -> None:
    # 2000bps 提供方利润 → 1.2 倍；×1.2 毛因子 → 1.44 → profit rate 0.44
    assert _profit_rate(2_000) == Decimal("0.44")


def test_profit_rate_zero_and_high_bps() -> None:
    assert _profit_rate(0) == Decimal("0.2")
    assert _profit_rate(100_000) == Decimal("12.2")


def test_profit_rate_is_quantized_to_six_decimals() -> None:
    result = _profit_rate(3_333)
    assert result == Decimal("0.599960")
    assert result.as_tuple().exponent == -6


def test_profit_rate_rejects_invalid_inputs() -> None:
    with pytest.raises((TypeError, ValueError)):
        _profit_rate("x")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        _profit_rate(None)  # type: ignore[arg-type]

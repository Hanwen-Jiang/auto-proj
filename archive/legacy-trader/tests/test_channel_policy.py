from __future__ import annotations

import pytest

from a6api_auto.channel_policy import (
    is_cleanup_forbidden_channel_name,
    is_forbidden_channel_name,
    is_generated_channel_name,
)


@pytest.mark.parametrize(
    "name",
    ("eqgb5qrv35", "expdpxroye", "e2qxc53e2j", "正常API中转站"),
)
def test_management_excludes_generated_and_fixed_forbidden_names(name: str) -> None:
    assert is_forbidden_channel_name(name) is True


@pytest.mark.parametrize(
    "name",
    (
        "quietumbrella",
        "easygarden",
        "exampleapi",
        "lover",
        "PlusOne",
        "现货-SOL",
        "秋分 Memo",
        "e12345678",
    ),
)
def test_management_accepts_normal_channel_names(name: str) -> None:
    assert is_forbidden_channel_name(name) is False


def test_generated_name_shape_is_exact() -> None:
    assert is_generated_channel_name("eqgb5qrv35") is True
    assert is_generated_channel_name("Eqgb5qrv35") is False
    assert is_generated_channel_name("eqgb5qrv350") is False


def test_remote_cleanup_remains_limited_to_fixed_forbidden_name() -> None:
    assert is_cleanup_forbidden_channel_name("正常API中转站") is True
    assert is_cleanup_forbidden_channel_name("eqgb5qrv35") is False

from __future__ import annotations

import re
from typing import Any

FORBIDDEN_CHANNEL_NAME = "正常API中转站"
GENERATED_CHANNEL_NAME_PATTERN = r"^e[a-z0-9]{9}$"
GENERATED_CHANNEL_NAME_SIGNAL_PATTERN = r"[0-9]|[^aeiou0-9]{4}"
_GENERATED_CHANNEL_NAME = re.compile(GENERATED_CHANNEL_NAME_PATTERN, re.ASCII)
_GENERATED_CHANNEL_NAME_SIGNAL = re.compile(
    GENERATED_CHANNEL_NAME_SIGNAL_PATTERN,
    re.ASCII,
)


def is_generated_channel_name(value: Any) -> bool:
    normalized = str(value or "").strip()
    return bool(
        _GENERATED_CHANNEL_NAME.fullmatch(normalized)
        and _GENERATED_CHANNEL_NAME_SIGNAL.search(normalized)
    )


def is_forbidden_channel_name(value: Any) -> bool:
    normalized = str(value or "").strip()
    return normalized == FORBIDDEN_CHANNEL_NAME or is_generated_channel_name(normalized)


def is_cleanup_forbidden_channel_name(value: Any) -> bool:
    return str(value or "").strip() == FORBIDDEN_CHANNEL_NAME

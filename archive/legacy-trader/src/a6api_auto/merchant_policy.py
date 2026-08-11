from __future__ import annotations

import re
from typing import Any

DEFAULT_OPERATING_GROUP = "standard"
FOCUS_OPERATING_GROUP = "重点组"
_OPERATING_GROUP = re.compile(r"^[^\x00-\x1f\x7f]{1,64}$")
_SECRET_MARKER = re.compile(
    r"(?:secret|password|cookie|bearer|token|api[-_]?key)",
    re.IGNORECASE,
)


def normalize_operating_group(value: Any) -> str:
    group = str(value or DEFAULT_OPERATING_GROUP).strip()
    if (
        not _OPERATING_GROUP.fullmatch(group)
        or "://" in group
        or "@" in group
        or _SECRET_MARKER.search(group)
    ):
        raise ValueError("operating_group_invalid")
    return group

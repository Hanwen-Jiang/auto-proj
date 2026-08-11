from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .merchant_policy import DEFAULT_OPERATING_GROUP, normalize_operating_group

DEFAULT_QUOTA_PER_UNIT = 500_000


@dataclass(frozen=True)
class MerchantTemplate:
    template_id: str
    supplier_name: str
    supplier_description: str
    channel_name: str
    channel_nickname: str
    channel_description: str
    model_description: str = ""
    channel_type: int = 1

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> MerchantTemplate:
        return cls(**value)


@dataclass
class SessionRecord:
    schema_version: int
    origin: str
    email: str
    username: str
    password: str = field(repr=False)
    user_id: int | None = None
    quota_per_unit: int = DEFAULT_QUOTA_PER_UNIT
    cookies: dict[str, str] = field(default_factory=dict, repr=False)
    status: str = "allocated"
    token_name: str = ""
    a6api_key: str = field(default="", repr=False)
    template_id: str = ""
    operating_group: str = DEFAULT_OPERATING_GROUP
    supplier_id: int | None = None
    request_id: int | None = None
    upstream_base_url: str = ""
    upstream_api_key_last4: str = ""
    models: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        if (
            isinstance(self.quota_per_unit, bool)
            or not isinstance(self.quota_per_unit, int)
            or self.quota_per_unit < 1
        ):
            raise ValueError("quota_per_unit_invalid")
        self.operating_group = normalize_operating_group(self.operating_group)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> SessionRecord:
        return cls(**value)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

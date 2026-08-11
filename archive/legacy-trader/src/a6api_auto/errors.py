from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any


class A6AutoError(RuntimeError):
    """Stable operational error without provider response or secret material."""


class A6HttpError(A6AutoError):
    """Secret-free endpoint failure retaining only its HTTP classification."""

    def __init__(self, code: str, *, status_code: int) -> None:
        super().__init__(code)
        self.status_code = int(status_code)

    @property
    def transient(self) -> bool:
        return self.status_code == 429 or 500 <= self.status_code <= 599


class A6MutationError(A6AutoError):
    """Secret-free mutation failure with stable provider diagnostics."""

    def __init__(
        self,
        code: str,
        *,
        operation: str,
        status_code: int,
        provider_error_code: str | None = None,
    ) -> None:
        super().__init__(code)
        self.code = str(code)
        self.operation = str(operation)
        self.status_code = int(status_code)
        self.provider_error_code = (
            str(provider_error_code).strip().casefold()
            if provider_error_code is not None
            else None
        )
        self.diagnostics = MappingProxyType(
            {
                "operation": self.operation,
                "http_status": self.status_code,
                "provider_error_code": self.provider_error_code,
            }
        )

    @property
    def transient(self) -> bool:
        return self.status_code == 429 or 500 <= self.status_code <= 599


class A6ProbeTransportError(A6AutoError):
    """Secret-free transport failure with bounded connection-stage evidence."""

    def __init__(self, code: str, diagnostics: Mapping[str, Any]) -> None:
        super().__init__(code)
        self.code = str(code)
        self.diagnostics = MappingProxyType(dict(diagnostics))


class A6TransportError(A6AutoError):
    """Secret-free dashboard/API transport failure with bounded request evidence."""

    def __init__(self, code: str, diagnostics: Mapping[str, Any]) -> None:
        super().__init__(code)
        self.code = str(code)
        self.diagnostics = MappingProxyType(dict(diagnostics))

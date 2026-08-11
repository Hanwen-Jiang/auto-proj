from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .trade_channel_failure import classify_channel_failure

CHANNEL_HEALTH_STATES = frozenset(
    {
        "cooling",
        "unavailable",
        "degraded",
        "unlabeled",
        "idle",
        "healthy",
        "unknown",
    }
)

_COOLING_STATES = frozenset({"cooldown", "cooling", "open"})
_UNAVAILABLE_STATES = frozenset({"blocked", "disabled", "unavailable"})


@dataclass(frozen=True)
class ChannelHealthInput:
    available: bool
    route_status: str = ""
    circuit_state: str = ""
    circuit_cooling_until: int | None = None
    cooldown_state: str = ""
    cooldown_remaining_seconds: int | None = None
    cooldown_expected_recovery_at: int | None = None
    smart_routing_status: str = ""
    smart_routing_labels: Sequence[str] = ()
    sample_count: int | None = None
    recent_success_rate: int | None = None
    last_success_at: int | None = None
    last_failure_at: int | None = None
    last_failure_code: str = ""
    last_failure_responsibility: str = ""


def classify_channel_health(
    evidence: ChannelHealthInput,
    *,
    now_epoch: int | None = None,
    degraded_success_rate: int = 8000,
    idle_seconds: int = 1800,
) -> str:
    """Classify one marketplace channel without collapsing the underlying evidence."""

    route = _state(evidence.route_status)
    circuit = _state(evidence.circuit_state)
    cooldown = _state(evidence.cooldown_state)
    remaining = evidence.cooldown_remaining_seconds

    circuit_cooling = circuit in _COOLING_STATES and not _expired_cooling_state(
        circuit,
        evidence.circuit_cooling_until,
        now_epoch,
    )
    cooldown_cooling = cooldown in _COOLING_STATES and not _expired_cooling_state(
        cooldown,
        evidence.cooldown_expected_recovery_at,
        now_epoch,
    )
    if circuit_cooling or cooldown_cooling or (remaining is not None and remaining > 0):
        return "cooling"
    if (
        not evidence.available
        or route in _UNAVAILABLE_STATES
        or circuit in _UNAVAILABLE_STATES
        or cooldown in _UNAVAILABLE_STATES
    ):
        return "unavailable"

    samples = evidence.sample_count
    success_rate = evidence.recent_success_rate
    failure_responsibility = str(evidence.last_failure_responsibility or "").strip().lower()
    if failure_responsibility not in {"client", "provider", "platform", "unknown"}:
        failure_responsibility = classify_channel_failure(evidence.last_failure_code)
    low_window_success = (
        samples is not None
        and samples >= 10
        and success_rate is not None
        and success_rate < degraded_success_rate
    )
    if (
        evidence.last_failure_at is not None
        and evidence.last_failure_code.strip()
        and failure_responsibility in {"provider", "platform"}
        and (
            evidence.last_success_at is None
            or evidence.last_failure_at > evidence.last_success_at
        )
    ):
        return "degraded"

    routing = _state(evidence.smart_routing_status)
    labels = tuple(
        str(value).strip()
        for value in evidence.smart_routing_labels
        if str(value).strip()
    )
    if routing and (routing != "labeled" or not labels):
        return "unlabeled"

    if evidence.last_success_at is None:
        return "idle" if samples is not None else "unknown"
    if now_epoch is not None and now_epoch - evidence.last_success_at > idle_seconds:
        return "idle"
    if low_window_success:
        if failure_responsibility == "client":
            return "healthy"
        # A rolling success window decays more slowly than the latest event.  Once a
        # later success has proved recovery, an old provider failure must not keep the
        # channel degraded until all 100 historical samples have rotated out.
        if (
            evidence.last_failure_at is None
            or evidence.last_success_at >= evidence.last_failure_at
        ):
            return "healthy"
        return "unknown"
    return "healthy"


def channel_health_evidence(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return the bounded, secret-free subset suitable for transition audit rows."""

    return {
        "route_status": str(value.get("route_status") or "")[:64],
        "circuit_state": str(value.get("circuit_state") or "")[:64],
        "circuit_probe_status": str(value.get("circuit_probe_status") or "")[:64],
        "circuit_last_error_kind": str(value.get("circuit_last_error_kind") or "")[:128],
        "circuit_probe_success_required": value.get(
            "circuit_probe_success_required"
        ),
        "circuit_probe_success_streak": value.get("circuit_probe_success_streak"),
        "circuit_scope": str(value.get("circuit_scope") or "")[:64],
        "circuit_recovery_mode": str(value.get("circuit_recovery_mode") or "")[:64],
        "circuit_cooling_until": value.get("circuit_cooling_until"),
        "circuit_last_cooling_at": value.get("circuit_last_cooling_at"),
        "circuit_last_probe_at": value.get("circuit_last_probe_at"),
        "circuit_next_probe_at": value.get("circuit_next_probe_at"),
        "cooldown_state": str(value.get("cooldown_state") or "")[:64],
        "cooldown_label": str(value.get("cooldown_label") or "")[:128],
        "cooldown_remaining_seconds": value.get("cooldown_remaining_seconds"),
        "cooldown_expected_recovery_at": value.get(
            "cooldown_expected_recovery_at"
        ),
        "cooldown_next_probe_at": value.get("cooldown_next_probe_at"),
        "cooldown_sources": list(value.get("cooldown_sources") or ())[:20],
        "smart_routing_status": str(value.get("smart_routing_status") or "")[:64],
        "smart_routing_labels": list(value.get("smart_routing_labels") or ())[:20],
        "sample_count": value.get("sample_count"),
        "recent_success_rate": value.get("recent_success_rate"),
        "last_failure_code": str(value.get("last_failure_code") or "")[:128],
        "last_failure_responsibility": classify_channel_failure(
            value.get("last_failure_code")
        ),
        "recent_p50_ms": value.get("recent_p50_ms"),
        "p50_ttft_ms": value.get("p50_ttft_ms"),
    }


def _state(value: object) -> str:
    return str(value or "").strip().lower()


def _expired_cooling_state(
    state: str,
    deadline_epoch: int | None,
    now_epoch: int | None,
) -> bool:
    return (
        state in {"cooldown", "cooling"}
        and deadline_epoch is not None
        and now_epoch is not None
        and deadline_epoch <= now_epoch
    )

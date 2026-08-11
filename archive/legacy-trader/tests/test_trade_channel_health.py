from __future__ import annotations

from a6api_auto.trade_channel_health import ChannelHealthInput, classify_channel_health


def test_health_prefers_cooling_over_derived_unavailability() -> None:
    assert (
        classify_channel_health(
            ChannelHealthInput(
                available=False,
                circuit_state="cooling",
                cooldown_remaining_seconds=120,
                sample_count=57,
                recent_success_rate=5789,
            )
        )
        == "cooling"
    )


def test_health_detects_degraded_provider_attributed_success_rate() -> None:
    assert (
        classify_channel_health(
            ChannelHealthInput(
                available=True,
                smart_routing_status="labeled",
                smart_routing_labels=("fast",),
                sample_count=20,
                recent_success_rate=7999,
                last_success_at=995,
                last_failure_at=996,
                last_failure_code="do_request_failed",
            ),
            now_epoch=1000,
            degraded_success_rate=8000,
        )
        == "degraded"
    )


def test_health_clears_stale_degradation_after_later_success() -> None:
    assert (
        classify_channel_health(
            ChannelHealthInput(
                available=True,
                smart_routing_status="labeled",
                smart_routing_labels=("fast",),
                sample_count=100,
                recent_success_rate=5500,
                last_success_at=999,
                last_failure_at=900,
                last_failure_code="do_request_failed",
            ),
            now_epoch=1000,
            degraded_success_rate=8000,
        )
        == "healthy"
    )


def test_expired_cooling_string_does_not_override_current_availability() -> None:
    assert (
        classify_channel_health(
            ChannelHealthInput(
                available=True,
                circuit_state="cooling",
                circuit_cooling_until=900,
                cooldown_state="cooling",
                cooldown_expected_recovery_at=900,
                cooldown_remaining_seconds=0,
                smart_routing_status="labeled",
                smart_routing_labels=("fast",),
                sample_count=20,
                recent_success_rate=10000,
                last_success_at=999,
            ),
            now_epoch=1000,
        )
        == "healthy"
    )


def test_health_does_not_blame_channel_for_client_request_failure() -> None:
    assert (
        classify_channel_health(
            ChannelHealthInput(
                available=True,
                smart_routing_status="labeled",
                smart_routing_labels=("fast",),
                sample_count=100,
                recent_success_rate=7300,
                last_success_at=995,
                last_failure_at=996,
                last_failure_code="invalid_prompt",
            ),
            now_epoch=1000,
            degraded_success_rate=9000,
        )
        == "healthy"
    )


def test_health_keeps_unclassified_low_success_as_unknown() -> None:
    assert (
        classify_channel_health(
            ChannelHealthInput(
                available=True,
                smart_routing_status="labeled",
                smart_routing_labels=("fast",),
                sample_count=100,
                recent_success_rate=7300,
                last_success_at=995,
                last_failure_at=996,
                last_failure_code="bad_response_status_code",
            ),
            now_epoch=1000,
            degraded_success_rate=9000,
        )
        == "unknown"
    )


def test_health_detects_missing_smart_routing_label() -> None:
    assert (
        classify_channel_health(
            ChannelHealthInput(
                available=True,
                smart_routing_status="observing",
                smart_routing_labels=(),
                sample_count=20,
                recent_success_rate=10000,
                last_success_at=995,
            ),
            now_epoch=1000,
        )
        == "unlabeled"
    )


def test_health_detects_idle_and_healthy_channels() -> None:
    common = {
        "available": True,
        "smart_routing_status": "labeled",
        "smart_routing_labels": ("cheap",),
        "sample_count": 50,
        "recent_success_rate": 10000,
    }
    assert (
        classify_channel_health(
            ChannelHealthInput(**common, last_success_at=100),
            now_epoch=2000,
            idle_seconds=1800,
        )
        == "idle"
    )
    assert (
        classify_channel_health(
            ChannelHealthInput(**common, last_success_at=1995),
            now_epoch=2000,
            idle_seconds=1800,
        )
        == "healthy"
    )

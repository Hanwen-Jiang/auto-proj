from datetime import datetime, timedelta, timezone

from a6api_auto.trade_market_traffic import build_market_traffic_snapshot

NOW = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)


def event(
    *,
    channel_id: int,
    seconds_ago: int,
    ratio: str | None,
    observers=(1,),
    owned=False,
):
    return {
        "channel_id": channel_id,
        "first_observed_at": NOW - timedelta(seconds=seconds_ago),
        "realtime_ratio_cny": ratio,
        "observer_ids": list(observers),
        "is_owned": owned,
    }


def test_market_traffic_uses_fixed_cny_bands_and_equal_comparison_windows() -> None:
    snapshot = build_market_traffic_snapshot(
        (
            event(channel_id=101, seconds_ago=5, ratio="0.075", observers=(1, 2)),
            event(channel_id=101, seconds_ago=15, ratio="0.075"),
            event(channel_id=102, seconds_ago=75, ratio="0.075"),
            event(channel_id=103, seconds_ago=25, ratio="0.15", owned=True),
            event(channel_id=104, seconds_ago=10, ratio=None),
        ),
        (
            {
                "model": "gpt-5.6-sol",
                "channel_id": 101,
                "realtime_ratio_cny": "0.075",
                "available": True,
                "last_observed_at": NOW - timedelta(seconds=2),
            },
            {
                "model": "gpt-5.6-sol",
                "channel_id": 103,
                "realtime_ratio_cny": "0.15",
                "available": False,
                "last_observed_at": NOW - timedelta(seconds=3),
            },
        ),
        model="gpt-5.6-sol",
        window="5m",
        now=NOW,
    )

    low = next(item for item in snapshot["bands"] if item["label"] == "K_CNY 0.07-0.08")
    middle = next(item for item in snapshot["bands"] if item["label"] == "K_CNY 0.14-0.16")

    assert snapshot["totals"] == {
        "event_lower_bound": 4,
        "quorum_event_count": 1,
        "active_channel_count": 3,
        "owned_event_count": 1,
        "unpriced_event_count": 1,
    }
    assert snapshot["freshness"]["current_channel_count"] == 2
    assert snapshot["freshness"]["available_channel_count"] == 1
    assert snapshot["freshness"]["age_seconds"] == 2
    assert low["event_lower_bound"] == 3
    assert low["active_channel_count"] == 2
    assert low["just_now_rate_per_minute"] == 2
    assert low["one_minute_ago_rate_per_minute"] == 1
    assert low["trend"] == "up"
    assert low["current_channel_count"] == 1
    assert sum(item["event_lower_bound"] for item in low["series"]) == 3
    assert middle["owned_event_count"] == 1
    assert middle["current_channel_count"] == 1
    assert len(low["series"]) == 30


def test_market_traffic_excludes_out_of_window_and_other_model_signals() -> None:
    snapshot = build_market_traffic_snapshot(
        (
            event(channel_id=1, seconds_ago=301, ratio="0.08"),
            event(channel_id=2, seconds_ago=-10, ratio="0.08"),
        ),
        (
            {
                "model": "gpt-5.6-terra",
                "channel_id": 9,
                "realtime_ratio_cny": "0.08",
                "available": True,
                "last_observed_at": NOW,
            },
            {
                "model": "gpt-5.6-sol",
                "channel_id": 10,
                "realtime_ratio_cny": "0.08",
                "available": True,
                "last_observed_at": NOW - timedelta(seconds=31),
            },
        ),
        model="gpt-5.6-sol",
        window="5m",
        now=NOW,
    )

    assert snapshot["totals"]["event_lower_bound"] == 0
    assert snapshot["freshness"]["current_channel_count"] == 0
    assert snapshot["method"]["current_signal_max_age_seconds"] == 30


def test_market_traffic_rejects_non_realtime_window() -> None:
    try:
        build_market_traffic_snapshot((), (), model="gpt-5.6-sol", window="all", now=NOW)
    except ValueError as exc:
        assert str(exc) == "market_traffic_window_invalid"
    else:
        raise AssertionError("expected market_traffic_window_invalid")

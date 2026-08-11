from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from a6api_auto import cli
from a6api_auto.models import SessionRecord
from a6api_auto.storage import SessionStore
from a6api_auto.trade_managed_autoonboard import (
    DEFAULT_SEED_PRICES,
    PRICE_SEED_FOLLOWER_STRATEGY,
    ManagedAutoOnboardError,
    ManagedAutoOnboardPricePlan,
    ManagedAutoOnboardRequest,
    ManagedAutoOnboardTarget,
    ManagedChannelAutoOnboarder,
    PostgresManagedAutoOnboardDiscovery,
    PostgresManagedAutoOnboardPriceRecorder,
    auto_channel_logical_key,
)
from a6api_auto.trade_managed_listings import (
    ExistingChannelSelection,
    ManagedDecision,
    ManagedListingError,
    stable_managed_uid,
)


class FakeDiscovery:
    def __init__(self, targets: tuple[ManagedAutoOnboardTarget, ...]) -> None:
        self.targets = targets
        self.calls: list[dict[str, Any]] = []

    def list_targets(self, **kwargs: Any) -> tuple[ManagedAutoOnboardTarget, ...]:
        self.calls.append(dict(kwargs))
        return self.targets


class FakeController:
    def __init__(
        self,
        target: ManagedAutoOnboardTarget,
        *,
        listing: dict[str, Any] | None = None,
        fail: str | None = None,
        listing_fail: str | None = None,
    ) -> None:
        self.target = target
        self.selection = ExistingChannelSelection(
            channel={
                "id": target.a6_channel_id,
                "status": 1,
                "availability": 1,
                "models": "gpt-5.5",
            },
            listing=listing,
        )
        self.fail = fail
        self.listing_fail = listing_fail
        self.selection_calls: list[tuple[str, int | None]] = []
        self.channel_calls: list[tuple[str, bool]] = []
        self.channel_update_calls: list[tuple[str, dict[str, Any], bool]] = []
        self.listing_calls: list[tuple[str, Any, bool]] = []
        self.direct_listing_calls: list[tuple[str, Any, bool]] = []
        self.listing_update_calls: list[tuple[str, Any, int | None, bool]] = []

    def select_existing_channel(
        self,
        _merchant: Any,
        *,
        model: str,
        preferred_channel_id: int | None = None,
    ) -> ExistingChannelSelection:
        self.selection_calls.append((model, preferred_channel_id))
        if self.fail:
            raise ManagedAutoOnboardError(self.fail)
        return self.selection

    def ensure_existing_channel(
        self,
        merchant: Any,
        logical_key: str,
        _selection: ExistingChannelSelection,
        *,
        model: str,
        managed_uid: str | None = None,
        apply: bool,
    ) -> ManagedDecision:
        assert model == "gpt-5.6-sol"
        self.channel_calls.append((logical_key, apply))
        managed_uid = managed_uid or stable_managed_uid(
            "channel", merchant.merchant_id, logical_key
        )
        return ManagedDecision(
            merchant.merchant_id,
            "channel",
            managed_uid,
            "adopt_existing",
            "verified" if apply else "planned",
            apply,
            external_id=self.target.a6_channel_id,
        )

    def ensure_listing_on_existing_channel(
        self,
        merchant: Any,
        channel_managed_uid: str,
        _selection: ExistingChannelSelection,
        spec: Any,
        *,
        apply: bool,
    ) -> ManagedDecision:
        self.listing_calls.append((channel_managed_uid, spec, apply))
        if self.listing_fail:
            raise ManagedListingError(self.listing_fail)
        return ManagedDecision(
            merchant.merchant_id,
            "listing",
            f"listing:{channel_managed_uid}",
            "adopt_existing" if self.selection.listing else "create",
            "verified" if apply else "planned",
            apply,
            external_id=501 if apply else None,
        )

    def ensure_listing(
        self,
        merchant: Any,
        channel_managed_uid: str,
        spec: Any,
        *,
        apply: bool,
    ) -> ManagedDecision:
        self.direct_listing_calls.append((channel_managed_uid, spec, apply))
        return ManagedDecision(
            merchant.merchant_id,
            "listing",
            f"listing:{channel_managed_uid}",
            "retry_create",
            "verified" if apply else "error",
            apply,
            external_id=501 if apply else None,
        )

    def update_listing(
        self,
        merchant: Any,
        managed_uid: str,
        spec: Any,
        *,
        expected_status: int | None = None,
        apply: bool,
    ) -> ManagedDecision:
        self.listing_update_calls.append((managed_uid, spec, expected_status, apply))
        return ManagedDecision(
            merchant.merchant_id,
            "listing",
            managed_uid,
            "update",
            "verified" if apply else "planned",
            apply,
            external_id=self.target.a6_listing_id,
        )

    def update_channel(
        self,
        merchant: Any,
        managed_uid: str,
        patch: dict[str, Any],
        *,
        apply: bool,
    ) -> ManagedDecision:
        self.channel_update_calls.append((managed_uid, dict(patch), apply))
        assert isinstance(self.selection.channel, dict)
        self.selection.channel["models"] = patch["models"]
        return ManagedDecision(
            merchant.merchant_id,
            "channel",
            managed_uid,
            "update",
            "verified" if apply else "planned",
            apply,
            external_id=self.target.a6_channel_id,
        )


def _target(
    session_path: Path,
    *,
    merchant_id: int = 5,
    user_id: int = 1005,
    supplier_id: int = 205,
    channel_id: int = 42,
    managed_uid: str | None = None,
) -> ManagedAutoOnboardTarget:
    return ManagedAutoOnboardTarget(
        merchant_id=merchant_id,
        a6_user_id=user_id,
        supplier_id=supplier_id,
        roles=("seller",),
        session_ref=str(session_path),
        a6_channel_id=channel_id,
        channel_managed_uid=managed_uid,
        channel_control_state="verified" if managed_uid else None,
    )


def _session(path: Path, target: ManagedAutoOnboardTarget) -> None:
    SessionStore(path).save(
        SessionRecord(
            schema_version=1,
            origin="https://a6.fixture.invalid",
            email=f"merchant-{target.merchant_id}@fixture.invalid",
            username=f"merchant-{target.merchant_id}",
            password="fixture-password",
            user_id=target.a6_user_id,
            supplier_id=target.supplier_id,
            request_id=700 + target.merchant_id,
            status="supplier_submitted",
            template_id="merchant-001",
            cookies={"session": "fixture-cookie"},
        )
    )


def _runner(
    sessions: Path,
    targets: tuple[ManagedAutoOnboardTarget, ...],
    controllers: dict[int, FakeController],
    *,
    price_recorder: Any | None = None,
) -> ManagedChannelAutoOnboarder:
    return ManagedChannelAutoOnboarder(
        FakeDiscovery(targets),
        listing_store=object(),  # type: ignore[arg-type]
        client_factory=lambda target, _record: controllers[target.a6_channel_id],
        sessions_dir=sessions,
        session_refresher=lambda _store, _record, _client: None,
        controller_factory=lambda _store, client: client,
        price_recorder=price_recorder,
    )


def _price_plan() -> ManagedAutoOnboardPricePlan:
    return ManagedAutoOnboardPricePlan(
        source_action_id=901,
        source_entity_id="a6-managed:listing:m5:source",
        rank=3,
        tier="floor",
        price_zone="low",
        log_position="0.12",
        delta="0.4",
        target_final_multiplier="0.16",
        panel_multiplier="0.114286",
    )


class FakePriceRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[ManagedAutoOnboardTarget, str, str]] = []

    def confirm(
        self,
        target: ManagedAutoOnboardTarget,
        *,
        listing_managed_uid: str,
        model: str,
    ) -> bool:
        self.calls.append((target, listing_managed_uid, model))
        return True


def test_new_seller_channel_is_adopted_and_gets_seeded_listing(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    target = _target(sessions / "session-5.json")
    _session(Path(target.session_ref), target)
    controller = FakeController(target)
    runner = _runner(sessions, (target,), {target.a6_channel_id: controller})

    summary = runner.execute(ManagedAutoOnboardRequest(), apply=True).summary()

    logical_key = auto_channel_logical_key(target.a6_channel_id)
    expected_uid = stable_managed_uid("channel", target.merchant_id, logical_key)
    assert controller.selection_calls == [("gpt-5.6-sol", target.a6_channel_id)]
    assert controller.channel_calls == [(logical_key, True)]
    assert controller.listing_calls[0][0] == expected_uid
    assert (
        controller.listing_calls[0][1].input_price_micros,
        controller.listing_calls[0][1].output_price_micros,
        controller.listing_calls[0][1].cache_read_price_micros,
        controller.listing_calls[0][1].cache_write_price_micros,
    ) == DEFAULT_SEED_PRICES
    assert summary["success"] is True
    assert summary["verified_count"] == 1
    assert summary["targets"][0]["listing_seed_source"] == "default_seed"


def test_production_seed_policy_waits_instead_of_copying_unconfirmed_default(
    tmp_path: Path,
) -> None:
    sessions = tmp_path / "sessions"
    target = _target(sessions / "session-5.json")
    _session(Path(target.session_ref), target)
    controller = FakeController(target)
    runner = _runner(sessions, (target,), {target.a6_channel_id: controller})

    summary = runner.execute(
        ManagedAutoOnboardRequest(require_confirmed_price_seed=True),
        apply=True,
    ).summary()

    assert summary["success"] is False
    assert summary["targets"][0]["error_class"] == (
        "managed_autoonboard_confirmed_price_seed_missing"
    )
    assert controller.channel_calls == []
    assert controller.listing_calls == []


def test_existing_managed_channel_adds_model_before_preserving_listing_price(
    tmp_path: Path,
) -> None:
    sessions = tmp_path / "sessions"
    managed_uid = stable_managed_uid("channel", 5, "seller-primary")
    target = _target(sessions / "session-5.json", managed_uid=managed_uid)
    _session(Path(target.session_ref), target)
    existing = {
        "id": 700,
        "channel_id": target.a6_channel_id,
        "own_model": "gpt-5.6-sol",
        "platform_model": "gpt-5.6-sol",
        "input_price_micros": 610_001,
        "output_price_micros": 3_660_006,
        "cache_read_price_micros": 61_001,
        "cache_write_price_micros": 762_501,
        "description": "Team + Plus混池",
        "status": 0,
    }
    controller = FakeController(target, listing=existing)
    runner = _runner(sessions, (target,), {target.a6_channel_id: controller})

    summary = runner.execute(ManagedAutoOnboardRequest(), apply=True).summary()

    assert controller.channel_calls == []
    assert controller.channel_update_calls == [
        (managed_uid, {"models": "gpt-5.5,gpt-5.6-sol"}, True)
    ]
    assert controller.listing_calls[0][0] == managed_uid
    spec = controller.listing_calls[0][1]
    assert (
        spec.input_price_micros,
        spec.output_price_micros,
        spec.cache_read_price_micros,
        spec.cache_write_price_micros,
    ) == (610_001, 3_660_006, 61_001, 762_501)
    assert spec.description == "Team + Plus混池"
    assert summary["targets"][0]["channel"]["action"] == "update"
    assert summary["targets"][0]["listing_seed_source"] == "existing_listing"


def test_existing_managed_channel_with_model_skips_channel_mutation(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    managed_uid = stable_managed_uid("channel", 5, "seller-primary")
    target = _target(sessions / "session-5.json", managed_uid=managed_uid)
    _session(Path(target.session_ref), target)
    controller = FakeController(target)
    assert isinstance(controller.selection.channel, dict)
    controller.selection.channel["models"] = "gpt-5.5,gpt-5.6-sol"
    runner = _runner(sessions, (target,), {target.a6_channel_id: controller})

    summary = runner.execute(ManagedAutoOnboardRequest(), apply=True).summary()

    assert controller.channel_calls == []
    assert controller.channel_update_calls == []
    assert summary["targets"][0]["channel"]["action"] == "already_managed"
    assert summary["verified_count"] == 1


def test_unknown_channel_model_update_is_reconciled_before_listing(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    managed_uid = stable_managed_uid("channel", 5, "seller-primary")
    base = _target(sessions / "session-5.json", managed_uid=managed_uid)
    target = ManagedAutoOnboardTarget(
        **{**base.__dict__, "channel_control_state": "update_unknown"}
    )
    _session(Path(target.session_ref), target)
    controller = FakeController(target)
    assert isinstance(controller.selection.channel, dict)
    controller.selection.channel["models"] = "gpt-5.5,gpt-5.6-sol"
    runner = _runner(sessions, (target,), {target.a6_channel_id: controller})

    summary = runner.execute(ManagedAutoOnboardRequest(), apply=True).summary()

    assert controller.channel_update_calls == [
        (managed_uid, {"models": "gpt-5.5,gpt-5.6-sol"}, True)
    ]
    assert controller.listing_calls
    assert summary["verified_count"] == 1


@pytest.mark.parametrize(
    "control_state",
    ["planned", "creating", "create_unknown", "verifying", "error", "retired"],
)
def test_abnormal_channel_binding_is_readopted_in_place(
    tmp_path: Path,
    control_state: str,
) -> None:
    sessions = tmp_path / "sessions"
    managed_uid = stable_managed_uid("channel", 5, "existing-managed-channel")
    base = _target(sessions / "session-5.json", managed_uid=managed_uid)
    target = ManagedAutoOnboardTarget(**{**base.__dict__, "channel_control_state": control_state})
    _session(Path(target.session_ref), target)
    controller = FakeController(target)
    runner = _runner(sessions, (target,), {target.a6_channel_id: controller})

    summary = runner.execute(ManagedAutoOnboardRequest(), apply=True).summary()

    assert controller.channel_calls == [(auto_channel_logical_key(42), True)]
    assert controller.listing_calls[0][0] == managed_uid
    assert summary["verified_count"] == 1
    assert summary["failed"] == 0


def test_failed_listing_create_binding_reenters_create_retry(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    managed_uid = stable_managed_uid("channel", 5, "seller-primary")
    target = _target(sessions / "session-5.json", managed_uid=managed_uid)
    _session(Path(target.session_ref), target)
    controller = FakeController(
        target,
        listing_fail="managed_listing_not_verified",
    )
    assert isinstance(controller.selection.channel, dict)
    controller.selection.channel["models"] = "gpt-5.6-sol"
    runner = _runner(sessions, (target,), {target.a6_channel_id: controller})

    summary = runner.execute(ManagedAutoOnboardRequest(), apply=True).summary()

    assert len(controller.listing_calls) == 1
    assert len(controller.direct_listing_calls) == 1
    assert controller.direct_listing_calls[0][0] == managed_uid
    assert summary["verified_count"] == 1
    assert summary["failed"] == 0


def test_missing_listing_uses_fresh_managed_fleet_seed_before_official_default(
    tmp_path: Path,
) -> None:
    sessions = tmp_path / "sessions"
    base = _target(sessions / "session-5.json")
    target = ManagedAutoOnboardTarget(
        **{
            **base.__dict__,
            "listing_seed_prices": (1_500_000, 9_000_000, 150_000, 1_875_000),
            "listing_seed_source": "fleet_managed_listing",
        }
    )
    _session(Path(target.session_ref), target)
    controller = FakeController(target)
    runner = _runner(sessions, (target,), {target.a6_channel_id: controller})

    summary = runner.execute(ManagedAutoOnboardRequest(), apply=True).summary()

    spec = controller.listing_calls[0][1]
    assert (
        spec.input_price_micros,
        spec.output_price_micros,
        spec.cache_read_price_micros,
        spec.cache_write_price_micros,
    ) == (1_500_000, 9_000_000, 150_000, 1_875_000)
    assert summary["targets"][0]["listing_seed_source"] == "fleet_managed_listing"


@pytest.mark.parametrize("listing_status", (0, 2))
def test_existing_unpriced_listing_inherits_confirmed_seed_without_changing_status(
    tmp_path: Path,
    listing_status: int,
) -> None:
    sessions = tmp_path / "sessions"
    channel_uid = stable_managed_uid("channel", 5, "existing-channel")
    listing_uid = stable_managed_uid("listing", 5, "existing-listing")
    base = _target(sessions / "session-5.json", managed_uid=channel_uid)
    target = ManagedAutoOnboardTarget(
        **{
            **base.__dict__,
            "listing_managed_uid": listing_uid,
            "a6_listing_id": 700,
            "listing_status": listing_status,
            "listing_seed_prices": (620_000, 3_720_000, 62_000, 775_000),
            "listing_seed_source": "merchant_managed_listing",
            "listing_seed_plan": _price_plan(),
        }
    )
    _session(Path(target.session_ref), target)
    existing = {
        "id": 700,
        "channel_id": target.a6_channel_id,
        "own_model": "gpt-5.6-sol",
        "platform_model": "gpt-5.6-sol",
        "input_price_micros": 2_431_836,
        "output_price_micros": 14_591_016,
        "cache_read_price_micros": 243_184,
        "cache_write_price_micros": 3_039_795,
        "description": "Team + Plus混池",
        "status": listing_status,
    }
    controller = FakeController(target, listing=existing)
    assert isinstance(controller.selection.channel, dict)
    controller.selection.channel["models"] = "gpt-5.6-sol"
    price_recorder = FakePriceRecorder()
    runner = _runner(
        sessions,
        (target,),
        {target.a6_channel_id: controller},
        price_recorder=price_recorder,
    )

    summary = runner.execute(ManagedAutoOnboardRequest(), apply=True).summary()

    assert controller.listing_calls == []
    assert controller.direct_listing_calls == []
    assert len(controller.listing_update_calls) == 1
    updated_uid, spec, expected_status, apply = controller.listing_update_calls[0]
    assert (updated_uid, expected_status, apply) == (listing_uid, listing_status, True)
    assert (
        spec.input_price_micros,
        spec.output_price_micros,
        spec.cache_read_price_micros,
        spec.cache_write_price_micros,
    ) == target.listing_seed_prices
    assert spec.description == "Team + Plus混池"
    assert price_recorder.calls == [(target, listing_uid, "gpt-5.6-sol")]
    assert summary["success"] is True
    assert summary["targets"][0]["listing_seed_source"] == ("merchant_managed_listing")


def test_price_seed_confirmation_is_deferred_during_dry_run(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    channel_uid = stable_managed_uid("channel", 5, "existing-channel")
    listing_uid = stable_managed_uid("listing", 5, "existing-listing")
    base = _target(sessions / "session-5.json", managed_uid=channel_uid)
    target = ManagedAutoOnboardTarget(
        **{
            **base.__dict__,
            "listing_managed_uid": listing_uid,
            "a6_listing_id": 700,
            "listing_status": 2,
            "listing_seed_prices": (620_000, 3_720_000, 62_000, 775_000),
            "listing_seed_source": "merchant_managed_listing",
            "listing_seed_plan": _price_plan(),
        }
    )
    _session(Path(target.session_ref), target)
    controller = FakeController(
        target,
        listing={
            "id": 700,
            "channel_id": target.a6_channel_id,
            "own_model": "gpt-5.6-sol",
            "platform_model": "gpt-5.6-sol",
            "input_price_micros": 2_431_836,
            "output_price_micros": 14_591_016,
            "cache_read_price_micros": 243_184,
            "cache_write_price_micros": 3_039_795,
            "description": "Team + Plus混池",
            "status": 2,
        },
    )
    assert isinstance(controller.selection.channel, dict)
    controller.selection.channel["models"] = "gpt-5.6-sol"
    price_recorder = FakePriceRecorder()
    runner = _runner(
        sessions,
        (target,),
        {target.a6_channel_id: controller},
        price_recorder=price_recorder,
    )

    summary = runner.execute(ManagedAutoOnboardRequest(), apply=False).summary()

    assert controller.listing_update_calls[0][2:] == (2, False)
    assert price_recorder.calls == []
    assert summary["planned_count"] == 1


def test_channel_identity_is_stable_and_distinct_for_later_channels() -> None:
    first = stable_managed_uid("channel", 19, auto_channel_logical_key(1001))
    repeated = stable_managed_uid("channel", 19, auto_channel_logical_key(1001))
    later = stable_managed_uid("channel", 19, auto_channel_logical_key(1002))
    other_merchant = stable_managed_uid("channel", 20, auto_channel_logical_key(1001))

    assert first == repeated
    assert len({first, later, other_merchant}) == 3


def test_all_new_channels_for_the_same_merchant_are_independently_onboarded(
    tmp_path: Path,
) -> None:
    sessions = tmp_path / "sessions"
    session_path = sessions / "session-5.json"
    first = _target(session_path, channel_id=41)
    second = _target(session_path, channel_id=42)
    _session(session_path, first)
    controllers = {41: FakeController(first), 42: FakeController(second)}
    runner = _runner(sessions, (first, second), controllers)

    summary = runner.execute(ManagedAutoOnboardRequest(), apply=True).summary()

    assert summary["target_count"] == 2
    assert summary["verified_count"] == 2
    assert summary["failed"] == 0
    assert controllers[41].channel_calls == [(auto_channel_logical_key(41), True)]
    assert controllers[42].channel_calls == [(auto_channel_logical_key(42), True)]
    listing_uids = {controllers[channel_id].listing_calls[0][0] for channel_id in (41, 42)}
    assert len(listing_uids) == 2


def test_one_remote_failure_does_not_block_later_channel(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    first = _target(sessions / "session-5.json", channel_id=41)
    second = _target(
        sessions / "session-6.json",
        merchant_id=6,
        user_id=1006,
        supplier_id=206,
        channel_id=42,
    )
    _session(Path(first.session_ref), first)
    _session(Path(second.session_ref), second)
    controllers = {
        41: FakeController(first, fail="fixture_remote_failed"),
        42: FakeController(second),
    }
    runner = _runner(sessions, (first, second), controllers)

    summary = runner.execute(ManagedAutoOnboardRequest(), apply=True).summary()

    assert summary["success"] is False
    assert summary["failed"] == 1
    assert summary["verified_count"] == 1
    assert summary["targets"][0]["error_class"] == "fixture_remote_failed"
    assert controllers[42].listing_calls


def test_session_must_match_database_target_and_stay_under_root(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    outside = tmp_path / "outside.json"
    target = _target(outside)
    _session(outside, target)
    controller = FakeController(target)
    runner = _runner(sessions, (target,), {target.a6_channel_id: controller})

    summary = runner.execute(ManagedAutoOnboardRequest(), apply=True).summary()

    assert summary["failed"] == 1
    assert summary["targets"][0]["error_class"] == "managed_autoonboard_session_outside_root"
    assert controller.selection_calls == []


class FakeCursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.values = rows
        self.calls: list[tuple[str, Any]] = []

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def execute(self, query: str, params: Any) -> None:
        self.calls.append((" ".join(query.split()), params))

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self.values)


class FakeConnection:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.cursor_instance = FakeCursor(rows)
        self.closed = False

    def cursor(self) -> FakeCursor:
        return self.cursor_instance

    def close(self) -> None:
        self.closed = True


def test_postgres_discovery_selects_fresh_active_unretired_missing_listings() -> None:
    connection = FakeConnection(
        [
            {
                "merchant_id": 5,
                "a6_user_id": 1005,
                "supplier_id": 205,
                "roles": ["seller", "provider"],
                "session_ref": "/sessions/session-5.json",
                "a6_channel_id": 42,
                "channel_managed_uid": None,
                "channel_control_state": None,
                "seed_input_price_micros": 1_500_000,
                "seed_output_price_micros": 9_000_000,
                "seed_cache_read_price_micros": 150_000,
                "seed_cache_write_price_micros": 1_875_000,
                "listing_seed_source": "fleet_managed_listing",
                "seed_source_action_id": 901,
                "seed_source_entity_id": "a6-managed:listing:m7:source",
                "seed_rank": 2,
                "seed_tier": "floor",
                "seed_price_zone": "low",
                "seed_log_position": "0.1",
                "seed_delta": "0.5",
                "seed_target_final_multiplier": "0.15",
                "seed_panel_multiplier": "0.1",
            }
        ]
    )
    discovery = PostgresManagedAutoOnboardDiscovery(lambda: connection)

    targets = discovery.list_targets(
        model="gpt-5.6-sol",
        inventory_max_age_seconds=900,
        limit=100,
    )

    query, params = connection.cursor_instance.calls[0]
    assert "merchant.status = 'supplier_submitted'" in query
    assert "merchant.roles @> ARRAY['seller']::text[]" in query
    assert "channel.status = 1" in query
    assert "channel.availability = 1" in query
    assert "channel.channel_type = 1" in query
    assert "merchant.poll_error_count = 0" in query
    assert "channel.observed_at = merchant.last_poll_at" in query
    assert "retirement.completed_at IS NOT NULL" in query
    assert "listing_binding.control_state = 'verified'" in query
    assert "seed.price_zone = 'low'" in query
    assert "seed.source_entity_id = target_listing.managed_uid" in query
    assert "seed.merchant_id = merchant.id" not in query
    assert "managed_listing_price_seed_follower_v1" in query
    assert "latest_canonical_price_actions AS MATERIALIZED" in query
    assert "FROM latest_canonical_price_actions action" in query
    assert "confirmed_price_actions AS MATERIALIZED" in query
    assert "confirmed_follower_actions AS MATERIALIZED" in query
    assert "LEFT JOIN LATERAL (" in query
    assert "follower.expected_listing_status = target_listing.listing_status::text" in query
    assert "action.observed_after ->> 'listing_status'" in query
    assert "seed.source_action_id::text = sticky_follower.source_price_action_id" in query
    assert "follower_action.completed_at >= target_listing.observed_at AS fresh_write" in query
    assert "follower_action.source_price_action_id = price_seed.source_action_id::text" in query
    assert "follower_coverage.fresh_write IS NOT TRUE" in query
    assert "follower_coverage.source_covered IS NOT TRUE" in query
    assert "target_listing.managed_uid IS DISTINCT FROM price_seed.source_entity_id" in query
    assert "IS NOT DISTINCT FROM ROW(" in query
    assert "md5(concat(" in query
    assert "input_price_micros DESC" not in query
    assert "target_listing.managed_uid IS NULL" in query
    assert "ROW(" in query
    assert "current_listing.status IN (0, 2)" in query
    assert "current_listing.observed_at" in query
    assert "(merchant.operating_group = %s) DESC" in query
    assert "channel_binding.control_state IN" not in query
    assert params == (
        "gpt-5.6-sol",
        "gpt-5.6-sol",
        900,
        "gpt-5.6-sol",
        "gpt-5.6-sol",
        "正常API中转站",
        r"^e[a-z0-9]{9}$",
        r"[0-9]|[^aeiou0-9]{4}",
        900,
        "重点组",
        100,
    )
    assert targets[0].roles == ("seller", "provider")
    assert targets[0].a6_channel_id == 42
    assert targets[0].listing_seed_prices == (
        1_500_000,
        9_000_000,
        150_000,
        1_875_000,
    )
    assert targets[0].listing_seed_source == "fleet_managed_listing"
    assert targets[0].listing_seed_plan is not None
    assert targets[0].listing_seed_plan.source_action_id == 901
    assert targets[0].listing_seed_plan.price_zone == "low"
    assert connection.closed is True


class PriceRecorderCursor:
    def __init__(self, binding_row: dict[str, Any]) -> None:
        self.binding_row = binding_row
        self.calls: list[tuple[str, Any]] = []
        self.next_row: dict[str, Any] | None = None

    def __enter__(self) -> PriceRecorderCursor:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def execute(self, query: str, params: Any) -> None:
        normalized = " ".join(query.split())
        self.calls.append((normalized, params))
        if "FOR UPDATE OF binding" in normalized:
            self.next_row = dict(self.binding_row)
        elif "RETURNING id" in normalized:
            self.next_row = {"id": 990}
        else:
            self.next_row = None

    def fetchone(self) -> dict[str, Any] | None:
        value = self.next_row
        self.next_row = None
        return value


class PriceRecorderConnection:
    def __init__(self, binding_row: dict[str, Any]) -> None:
        self.cursor_instance = PriceRecorderCursor(binding_row)
        self.commits = 0
        self.rollbacks = 0
        self.closed = 0

    def cursor(self) -> PriceRecorderCursor:
        return self.cursor_instance

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed += 1


def test_price_recorder_confirms_seed_as_traceable_price_band_action(
    tmp_path: Path,
) -> None:
    prices = (620_000, 3_720_000, 62_000, 775_000)
    target = ManagedAutoOnboardTarget(
        merchant_id=5,
        a6_user_id=1005,
        supplier_id=205,
        roles=("seller",),
        session_ref=str(tmp_path / "session.json"),
        a6_channel_id=42,
        channel_managed_uid=stable_managed_uid("channel", 5, "existing"),
        channel_control_state="verified",
        listing_managed_uid=stable_managed_uid("listing", 5, "existing"),
        a6_listing_id=700,
        listing_status=0,
        listing_seed_prices=prices,
        listing_seed_source="merchant_managed_listing",
        listing_seed_plan=_price_plan(),
    )
    connection = PriceRecorderConnection(
        {
            "merchant_id": 5,
            "a6_listing_id": 700,
            "control_state": "verified",
            "listing_status": 0,
            "input_price_micros": prices[0],
            "output_price_micros": prices[1],
            "cache_read_price_micros": prices[2],
            "cache_write_price_micros": prices[3],
        }
    )
    recorder = PostgresManagedAutoOnboardPriceRecorder(
        lambda: connection,
        config_version="price-seed-follower-v1",
    )

    recorded = recorder.confirm(
        target,
        listing_managed_uid=str(target.listing_managed_uid),
        model="gpt-5.6-sol",
    )

    assert recorded is True
    insert = next(
        params
        for query, params in connection.cursor_instance.calls
        if "INSERT INTO a6_trade.trade_control_actions" in query
    )
    desired = json.loads(insert[2])
    observation = json.loads(insert[3])
    assert desired["pricing_strategy"] == PRICE_SEED_FOLLOWER_STRATEGY
    assert desired["price_zone"] == "low"
    assert desired["price_seed_source"] == "merchant_managed_listing"
    assert desired["source_price_action_id"] == 901
    assert desired["prices_micros"] == {
        "input": prices[0],
        "output": prices[1],
        "cache_read": prices[2],
        "cache_write": prices[3],
    }
    assert observation["listing_status"] == 0
    assert any(
        "INSERT INTO a6_trade.trade_control_action_attempts" in query
        for query, _params in connection.cursor_instance.calls
    )
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert connection.closed == 1


def test_price_recorder_uses_created_listing_readback_status(tmp_path: Path) -> None:
    prices = (620_000, 3_720_000, 62_000, 775_000)
    target = ManagedAutoOnboardTarget(
        merchant_id=5,
        a6_user_id=1005,
        supplier_id=205,
        roles=("seller",),
        session_ref=str(tmp_path / "session.json"),
        a6_channel_id=42,
        channel_managed_uid=stable_managed_uid("channel", 5, "existing"),
        channel_control_state="verified",
        listing_managed_uid=None,
        a6_listing_id=None,
        listing_status=None,
        listing_seed_prices=prices,
        listing_seed_source="merchant_managed_listing",
        listing_seed_plan=_price_plan(),
    )
    connection = PriceRecorderConnection(
        {
            "merchant_id": 5,
            "a6_listing_id": 700,
            "control_state": "verified",
            "listing_status": 2,
            "input_price_micros": prices[0],
            "output_price_micros": prices[1],
            "cache_read_price_micros": prices[2],
            "cache_write_price_micros": prices[3],
        }
    )
    recorder = PostgresManagedAutoOnboardPriceRecorder(
        lambda: connection,
        config_version="price-seed-follower-v1",
    )

    assert recorder.confirm(
        target,
        listing_managed_uid=stable_managed_uid("listing", 5, "created"),
        model="gpt-5.6-sol",
    )

    insert = next(
        params
        for query, params in connection.cursor_instance.calls
        if "INSERT INTO a6_trade.trade_control_actions" in query
    )
    desired = json.loads(insert[2])
    assert desired["expected_listing_status"] == 2


def test_price_recorder_rejects_activation_drift_before_audit(tmp_path: Path) -> None:
    prices = (620_000, 3_720_000, 62_000, 775_000)
    target = ManagedAutoOnboardTarget(
        merchant_id=5,
        a6_user_id=1005,
        supplier_id=205,
        roles=("seller",),
        session_ref=str(tmp_path / "session.json"),
        a6_channel_id=42,
        channel_managed_uid=stable_managed_uid("channel", 5, "existing"),
        channel_control_state="verified",
        listing_managed_uid=stable_managed_uid("listing", 5, "existing"),
        a6_listing_id=700,
        listing_status=0,
        listing_seed_prices=prices,
        listing_seed_source="merchant_managed_listing",
        listing_seed_plan=_price_plan(),
    )
    connection = PriceRecorderConnection(
        {
            "merchant_id": 5,
            "a6_listing_id": 700,
            "control_state": "verified",
            "listing_status": 2,
            "input_price_micros": prices[0],
            "output_price_micros": prices[1],
            "cache_read_price_micros": prices[2],
            "cache_write_price_micros": prices[3],
        }
    )
    recorder = PostgresManagedAutoOnboardPriceRecorder(
        lambda: connection,
        config_version="price-seed-follower-v1",
    )

    with pytest.raises(
        ManagedAutoOnboardError,
        match="managed_autoonboard_listing_activation_changed",
    ):
        recorder.confirm(
            target,
            listing_managed_uid=str(target.listing_managed_uid),
            model="gpt-5.6-sol",
        )

    assert not any(
        "INSERT INTO a6_trade.trade_control_actions" in query
        for query, _params in connection.cursor_instance.calls
    )
    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert connection.closed == 1


@pytest.mark.parametrize(
    "changes,error",
    [
        ({"model": "gpt-5.6-terra"}, "managed_autoonboard_model_unsupported"),
        ({"seed_prices": (1, 2, 3, 0)}, "managed_autoonboard_seed_prices_invalid"),
        ({"inventory_max_age_seconds": 59}, "managed_autoonboard_inventory_age_invalid"),
        ({"target_limit": 0}, "managed_autoonboard_target_limit_invalid"),
        (
            {"require_confirmed_price_seed": 1},
            "managed_autoonboard_price_seed_policy_invalid",
        ),
    ],
)
def test_request_rejects_unsafe_bounds(changes: dict[str, Any], error: str) -> None:
    with pytest.raises(ManagedAutoOnboardError, match=error):
        ManagedAutoOnboardRequest(**changes)


def test_cli_defaults_to_dry_run_with_official_seed_prices() -> None:
    args = cli.parser().parse_args(
        [
            "trade-auto-onboard-seller-channels",
            "--proxy-map-file",
            "proxy-map.json",
        ]
    )

    assert args.apply is False
    assert args.model == "gpt-5.6-sol"
    assert args.inventory_max_age_seconds == 900
    assert args.target_limit == 100
    assert args.allow_configured_price_seed is False
    assert (
        args.input_price_micros,
        args.output_price_micros,
        args.cache_read_price_micros,
        args.cache_write_price_micros,
    ) == DEFAULT_SEED_PRICES


def test_cli_wires_request_without_reading_runtime_secrets() -> None:
    captured: dict[str, Any] = {}

    class Result:
        def summary(self) -> dict[str, Any]:
            return {"success": True, "fixture": "autoonboard"}

    class Reconciler:
        def execute(self, request: ManagedAutoOnboardRequest, *, apply: bool) -> Result:
            captured.update(request=request, apply=apply)
            return Result()

    args = cli.parser().parse_args(
        [
            "trade-auto-onboard-seller-channels",
            "--proxy-map-file",
            "/not-read-when-injected.json",
            "--inventory-max-age-seconds",
            "600",
            "--target-limit",
            "17",
            "--input-price-micros",
            "101",
            "--output-price-micros",
            "202",
            "--cache-read-price-micros",
            "11",
            "--cache-write-price-micros",
            "33",
            "--apply",
        ]
    )

    result = cli.trade_auto_onboard_seller_channels_command(
        args,
        reconciler=Reconciler(),  # type: ignore[arg-type]
    )

    request = captured["request"]
    assert result == {"success": True, "fixture": "autoonboard"}
    assert captured["apply"] is True
    assert request.inventory_max_age_seconds == 600
    assert request.target_limit == 17
    assert request.seed_prices == (101, 202, 11, 33)
    assert request.require_confirmed_price_seed is True


def test_cli_requires_explicit_flag_for_unconfirmed_bootstrap_seed() -> None:
    args = cli.parser().parse_args(
        [
            "trade-auto-onboard-seller-channels",
            "--proxy-map-file",
            "proxy-map.json",
            "--allow-configured-price-seed",
        ]
    )

    assert args.allow_configured_price_seed is True


def test_systemd_timer_queues_durable_onboarding_under_shared_seller_lock() -> None:
    root = Path(__file__).resolve().parents[1]
    service = (root / "deploy/systemd/a6-trader-seller-channel-onboard.service").read_text()
    timer = (root / "deploy/systemd/a6-trader-seller-channel-onboard.timer").read_text()
    collector = (root / "deploy/systemd/a6-trader-collector.service").read_text()
    seller_pilot = (root / "deploy/systemd/a6-trader-seller-pilot.service").read_text()
    seller_retirement = (root / "deploy/systemd/a6-trader-seller-retirement.service").read_text()
    channel_growth = (root / "deploy/systemd/a6-trader-channel-growth.service").read_text()

    assert "a6-trader-collector.service" in service
    for unit in (service, seller_pilot, seller_retirement, channel_growth):
        assert "/var/lib/a6-trader/seller-control.lock" in unit
    assert "flock --exclusive /var/lib/a6-trader/seller-control.lock" in service
    assert "--nonblock" not in service
    assert "--conflict-exit-code" not in service
    assert "trade-auto-onboard-seller-channels" in service
    assert "--proxy-map-file /var/lib/a6-trader/secrets/proxy-map.json" in service
    assert "--model gpt-5.6-sol" in service
    assert "--target-limit 100" in service
    assert "--allow-configured-price-seed" not in service
    assert "--apply" in service
    assert "--systemd-accept-partial-result" in service
    assert "SuccessExitStatus=" not in service
    assert "TimeoutStartSec=30min" in service
    assert "--nonblock --conflict-exit-code 75" in channel_growth
    assert "--systemd-accept-partial-result" in channel_growth
    assert "SuccessExitStatus=75" in channel_growth
    assert "OnUnitInactiveSec=60s" in timer
    assert "Unit=a6-trader-seller-channel-onboard.service" in timer
    assert "OnSuccess=a6-trader-seller-channel-onboard.service" in collector
    assert "OnSuccess=a6-trader-seller-channel-onboard.service" in seller_pilot
    assert "--maximum-price-multiplier 10" in seller_pilot
    assert "--maximum-final-multiplier 10" in seller_pilot
    assert "phase3-all-default-listings-v15-all-controlled-price-jump" in seller_pilot
    assert "/venv/bin/python -m a6api_auto.cli trade-apply-seller-price-band" in seller_pilot

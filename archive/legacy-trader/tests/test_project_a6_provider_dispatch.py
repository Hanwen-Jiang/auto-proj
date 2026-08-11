from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from a6api_auto.trade_provider_dispatch import (
    ProviderDispatchBundle,
    ProviderDispatchError,
    ProviderDispatchSlot,
)
from a6api_auto.trade_provider_regional_runtime import ProviderRegionalRuntimeError
from scripts import project_a6_provider_dispatch as command


def dispatch_bundle() -> ProviderDispatchBundle:
    return ProviderDispatchBundle(
        model="gpt-5.6-sol",
        routing_generation=42,
        cost_generation=19,
        source_hash="b" * 64,
        cost_input_hash="c" * 64,
        cost_output_hash="d" * 64,
        ready_slots=(
            ProviderDispatchSlot(
                slot_no=1,
                account_id=51,
                token_ref_id=41,
                external_token_id=14093,
                channel_id=1008,
                rank=1,
                priority=2,
                cost_multiplier=Decimal("1.25"),
            ),
        ),
    )


class Source:
    def __init__(self, value):
        self.value = value
        self.calls = []

    def load_active(self, model):
        self.calls.append(model)
        return self.value


class Sink:
    def __init__(self, *, changed=True):
        self.changed = changed
        self.calls = []

    def publish(self, bundle):
        self.calls.append(bundle)
        return SimpleNamespace(changed=self.changed, key="dispatch-key")


def test_default_off_stays_cold() -> None:
    assert command.resolve_enabled(False, environ={}) is False
    assert command.run_model(
        "gpt-5.6-sol",
        enabled=False,
        publish=True,
    ) == {
        "success": True,
        "model": "gpt-5.6-sol",
        "enabled": False,
        "publish": False,
        "action": "disabled",
    }


def test_dry_run_reads_active_bundle_without_redis() -> None:
    source = Source(dispatch_bundle())

    result = command.run_model(
        "gpt-5.6-sol",
        enabled=True,
        publish=False,
        source=source,
    )

    assert result["action"] == "planned"
    assert result["published"] is False
    assert result["routing_generation"] == 42
    assert result["cost_generation"] == 19
    assert result["ready_count"] == 1
    assert source.calls == ["gpt-5.6-sol"]


def test_publish_repairs_active_bundle_and_reports_idempotency() -> None:
    source = Source(dispatch_bundle())
    sink = Sink(changed=False)

    result = command.run_model(
        "gpt-5.6-sol",
        enabled=True,
        publish=True,
        source=source,
        sink=sink,
    )

    assert result["action"] == "unchanged"
    assert result["published"] is True
    assert result["changed"] is False
    assert sink.calls == [dispatch_bundle()]


def test_absent_bundle_publishes_a_tombstone() -> None:
    tombstone = ProviderDispatchBundle.tombstone(
        "gpt-5.6-sol",
        routing_generation=41,
        cost_generation=18,
    )
    sink = Sink()
    result = command.run_model(
        "gpt-5.6-sol",
        enabled=True,
        publish=True,
        source=Source(tombstone),
        sink=sink,
    )

    assert result["action"] == "tombstoned"
    assert result["published"] is True
    assert result["state"] == "absent"
    assert sink.calls == [tombstone]


def test_enabled_publish_requires_sink() -> None:
    with pytest.raises(ProviderDispatchError, match="provider_dispatch_sink_missing"):
        command.run_model(
            "gpt-5.6-sol",
            enabled=True,
            publish=True,
            source=Source(dispatch_bundle()),
        )


def test_regional_projection_is_independently_default_off_and_requires_publish() -> None:
    assert command.resolve_enabled(
        False,
        env_name="PROVIDER_REGIONAL_COORDINATION_ENABLED",
        environ={},
    ) is False
    assert command.run_regional_model(
        "gpt-5.6-sol",
        enabled=False,
        publish=True,
        region="hk",
    )["action"] == "disabled"
    with pytest.raises(
        ProviderRegionalRuntimeError,
        match="provider_regional_publish_required",
    ):
        command.run_regional_model(
            "gpt-5.6-sol",
            enabled=True,
            publish=False,
            region="hk",
        )


def test_regional_projector_dsn_is_explicit_and_never_falls_back_to_main_dsn() -> None:
    with pytest.raises(
        ProviderRegionalRuntimeError,
        match="provider_regional_database_dsn_missing",
    ):
        command.resolve_regional_projector_dsn(
            "PROVIDER_REGIONAL_PROJECTOR_DATABASE_DSN",
            environ={"A6_TRADE_DATABASE_DSN": "main-role-dsn"},
        )
    assert (
        command.resolve_regional_projector_dsn(
            "PROVIDER_REGIONAL_PROJECTOR_DATABASE_DSN",
            environ={"PROVIDER_REGIONAL_PROJECTOR_DATABASE_DSN": "projector-role-dsn"},
        )
        == "projector-role-dsn"
    )


def test_regional_projection_delegates_to_exact_runtime() -> None:
    class Runtime:
        def __init__(self) -> None:
            self.calls = []

        def reconcile(self, model, *, region):
            self.calls.append((model, region))
            return {"success": True, "ready": False}

    runtime = Runtime()
    result = command.run_regional_model(
        "gpt-5.6-sol",
        enabled=True,
        publish=True,
        region="hk",
        runtime=runtime,  # type: ignore[arg-type]
    )

    assert result == {"success": True, "ready": False}
    assert runtime.calls == [("gpt-5.6-sol", "hk")]


def test_systemd_projection_is_default_off_and_periodic() -> None:
    service = Path("deploy/systemd/a6-trader-provider-dispatch.service").read_text(
        encoding="utf-8"
    )
    timer = Path("deploy/systemd/a6-trader-provider-dispatch.timer").read_text(
        encoding="utf-8"
    )

    assert "Environment=PROVIDER_DISPATCH_RECONCILE_ENABLED=true" not in service
    assert "PROVIDER_REGIONAL_COORDINATION_ENABLED=true" not in service
    assert "--regional-enabled" not in service
    assert "project_a6_provider_dispatch.py --model gpt-5.6-sol --publish" in service
    assert "ReadWritePaths=/var/lib/a6-trader" in service
    assert "OnUnitActiveSec=30s" in timer

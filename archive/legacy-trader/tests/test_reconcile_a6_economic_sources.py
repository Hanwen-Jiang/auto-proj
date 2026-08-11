from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pytest

from a6api_auto.trade_economic_source import (
    EconomicGenerationActivation,
    EconomicPlan,
    EconomicProjectionBundle,
    EconomicProjectionPublishResult,
    EconomicRoutingConfig,
    EconomicRoutingError,
)
from scripts.reconcile_a6_economic_sources import resolve_config, run_model

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)


def routing_config(**overrides: Any) -> EconomicRoutingConfig:
    values = {
        "enabled": True,
        "plan_only": True,
        "projection_apply_enabled": False,
    }
    values.update(overrides)
    return EconomicRoutingConfig(**values)


@dataclass
class FakeStore:
    config: EconomicRoutingConfig
    receipts: list[dict[str, Any]] = field(default_factory=list)

    def build_plan(self, model: str, *, config: EconomicRoutingConfig, now: datetime):
        assert config == self.config
        return (
            EconomicPlan(
                model=model,
                parent_generation=None,
                input_hash="a" * 64,
                output_hash="b" * 64,
                evaluated_at=now,
                config=config,
                records=(),
            ),
            (),
            None,
        )

    def activate(self, plan: EconomicPlan) -> EconomicGenerationActivation:
        return EconomicGenerationActivation(
            model=plan.model,
            economic_generation=7,
            generation_id=9,
            input_hash=plan.input_hash,
            output_hash=plan.output_hash,
            changed=True,
            plan_only=plan.config.plan_only,
            activated_at=plan.evaluated_at,
        )

    def record_projection_receipt(
        self,
        bundle: EconomicProjectionBundle,
        **values: Any,
    ) -> None:
        self.receipts.append({"bundle": bundle, **values})


@dataclass
class FakeSink:
    bundles: list[EconomicProjectionBundle] = field(default_factory=list)

    def publish(self, bundle: EconomicProjectionBundle) -> EconomicProjectionPublishResult:
        self.bundles.append(bundle)
        return EconomicProjectionPublishResult(
            key=f"a6:economic:priority1:v1:{bundle.model}",
            digest=bundle.digest(),
            economic_generation=bundle.economic_generation,
            state=bundle.state,
            changed=True,
        )


def test_disabled_reconciler_does_not_require_database_or_projection() -> None:
    result = run_model(
        "gpt-5.6-sol",
        reconcile_enabled=False,
        config=routing_config(),
    )

    assert result == {
        "success": True,
        "model": "gpt-5.6-sol",
        "reconcile_enabled": False,
        "action": "disabled",
    }


def test_plan_only_activates_postgres_generation_without_writing_redis() -> None:
    cfg = routing_config()
    store = FakeStore(cfg)

    result = run_model(
        "gpt-5.6-sol",
        reconcile_enabled=True,
        config=cfg,
        store=store,
        now=NOW,
    )

    assert result["action"] == "planned"
    assert result["published"] is False
    assert result["economic_generation"] == 7
    assert store.receipts[0]["projection_state"] == "plan_only"


def test_apply_publishes_active_projection_only_after_plan_only_is_off() -> None:
    cfg = routing_config(plan_only=False, projection_apply_enabled=True)
    store = FakeStore(cfg)
    sink = FakeSink()

    result = run_model(
        "gpt-5.6-sol",
        reconcile_enabled=True,
        config=cfg,
        store=store,
        sink=sink,
        now=NOW,
    )

    assert result["action"] == "published"
    assert result["published"] is True
    assert sink.bundles[0].state == "active"
    assert store.receipts[0]["projection_state"] == "projected"


def test_disabled_economic_feature_with_apply_withdraws_only_priority1_projection() -> None:
    cfg = routing_config(enabled=False, plan_only=False, projection_apply_enabled=True)
    store = FakeStore(cfg)
    sink = FakeSink()

    result = run_model(
        "gpt-5.6-sol",
        reconcile_enabled=True,
        config=cfg,
        store=store,
        sink=sink,
        now=NOW,
    )

    assert result["action"] == "withdrawn"
    assert sink.bundles[0].state == "absent"
    assert store.receipts[0]["projection_state"] == "withdrawn"


def test_environment_contract_defaults_to_fully_inert_plan_only() -> None:
    cfg = resolve_config({})

    assert cfg.enabled is False
    assert cfg.plan_only is True
    assert cfg.projection_apply_enabled is False
    assert cfg.api_sources_enabled is False
    assert cfg.account_sources_enabled is False
    assert cfg.a6_gateway_enabled is False
    assert cfg.minimum_profit_on_cost_bps == 6000


def test_environment_contract_rejects_ambiguous_boolean() -> None:
    with pytest.raises(
        EconomicRoutingError,
        match="economic_env_flag_invalid:ECONOMIC_PRIORITY1_ENABLED",
    ):
        resolve_config({"ECONOMIC_PRIORITY1_ENABLED": "sometimes"})

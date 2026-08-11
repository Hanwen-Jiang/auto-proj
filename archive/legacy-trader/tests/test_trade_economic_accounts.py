from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from a6api_auto.trade_economic_accounts import (
    AccountQuotaObservation,
    EconomicAccountSyncConfig,
    PostgresEconomicAccountStore,
    _next_weekly_reset,
    _stabilize_observation_epoch,
    estimate_weekly_total_a,
    normalize_reset_at,
    parse_account_quota_observation,
    parse_cycle_cost_cny,
    parse_owned_account,
    sync_owned_accounts,
)
from a6api_auto.trade_economic_source import EconomicRoutingError

NOW = datetime(2026, 8, 4, 3, 21, 36, tzinfo=timezone.utc)
RESET = datetime(2026, 8, 11, 3, 15, tzinfo=timezone.utc)


def account(account_id: int = 45920, **changes: Any) -> dict[str, Any]:
    value = {
        "id": account_id,
        "name": "gpt-plus",
        "platform": "openai",
        "type": "oauth",
        "status": "active",
        "schedulable": True,
        "group_ids": [3, 26],
        "notes": "\u00a510",
        "created_at": "2026-08-04T11:14:48.007249+08:00",
    }
    value.update(changes)
    return value


def usage(utilization: int = 3, cost: str = "14.1771335") -> dict[str, Any]:
    return {
        "updated_at": NOW.isoformat(),
        "seven_day": {
            "utilization": utilization,
            "resets_at": (RESET + timedelta(seconds=29)).isoformat(),
            "window_stats": {
                "requests": 71,
                "tokens": 3_544_593,
                "cost": cost,
            },
        },
    }


def evidence(utilization: str, cost: str, **changes: Any) -> AccountQuotaObservation:
    value = {
        "observed_at": NOW,
        "upstream_updated_at": NOW,
        "reset_at": RESET,
        "quota_epoch": f"codex-7d:{RESET.isoformat()}",
        "utilization_percent": Decimal(utilization),
        "window_cost_a": Decimal(cost),
        "requests": 1,
        "tokens": 1,
    }
    value.update(changes)
    return AccountQuotaObservation(**value)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("\u00a59", Decimal("9.000000")),
        ("\uffe59.6", Decimal("9.600000")),
        ("CNY 10", Decimal("10.000000")),
        ("10 \u5143", Decimal("10.000000")),
    ],
)
def test_cycle_cost_note_is_strict_but_accepts_expected_currency_forms(
    raw: str, expected: Decimal
) -> None:
    assert parse_cycle_cost_cny(raw) == expected


@pytest.mark.parametrize("raw", ("", "cost=10", "10 / 12", "-1"))
def test_cycle_cost_note_rejects_ambiguous_values(raw: str) -> None:
    with pytest.raises(EconomicRoutingError, match="economic_account_cost_note_invalid"):
        parse_cycle_cost_cny(raw)


def test_account_discovery_requires_name_oauth_and_target_group() -> None:
    config = EconomicAccountSyncConfig()

    candidate = parse_owned_account(account(), config)

    assert candidate is not None
    assert candidate.account_id == 45920
    assert candidate.cycle_cost_cny == Decimal("10.000000")
    assert parse_owned_account(account(name="other"), config) is None
    assert parse_owned_account(account(type="apikey"), config) is None
    assert parse_owned_account(account(group_ids=[3]), config) is None


def test_reset_jitter_is_bucketed_and_usage_sample_keeps_raw_cost() -> None:
    config = EconomicAccountSyncConfig()

    observation = parse_account_quota_observation(usage(), observed_at=NOW, config=config)

    assert observation.reset_at == RESET
    assert observation.quota_epoch == f"codex-7d:{RESET.isoformat()}"
    assert observation.utilization_percent == Decimal("3")
    assert observation.window_cost_a == Decimal("14.1771335")
    assert normalize_reset_at(RESET + timedelta(seconds=31)) == RESET


def test_usage_accepts_go_rfc3339_nanosecond_timestamp() -> None:
    payload = usage()
    payload["updated_at"] = "2026-08-04T11:49:55.473188223+08:00"

    observation = parse_account_quota_observation(
        payload,
        observed_at=NOW,
        config=EconomicAccountSyncConfig(),
    )

    assert observation.upstream_updated_at == datetime(
        2026, 8, 4, 3, 49, 55, 473188, tzinfo=timezone.utc
    )


def test_zero_percent_without_upstream_reset_uses_account_creation_epoch() -> None:
    payload = usage(0, "0")
    payload["seven_day"]["resets_at"] = None
    fallback = datetime(2026, 8, 11, 3, 14, 48, tzinfo=timezone.utc)

    observation = parse_account_quota_observation(
        payload,
        observed_at=NOW,
        config=EconomicAccountSyncConfig(),
        fallback_reset_at=fallback,
    )

    assert observation.reset_at == datetime(2026, 8, 11, 3, 15, tzinfo=timezone.utc)
    assert observation.utilization_percent == 0
    assert observation.reset_inferred is True


def test_creation_epoch_fallback_advances_to_the_next_week() -> None:
    assert _next_weekly_reset(RESET, RESET + timedelta(days=15)) == RESET + timedelta(days=21)


def test_authoritative_active_epoch_absorbs_one_bucket_reset_regression() -> None:
    stale = evidence(
        "0",
        "0",
        reset_at=RESET - timedelta(minutes=5),
        quota_epoch=f"codex-7d:{(RESET - timedelta(minutes=5)).isoformat()}",
    )

    stabilized = _stabilize_observation_epoch(
        stale,
        active_reset_at=RESET,
        active_epoch_authoritative=True,
        reset_bucket_seconds=300,
    )

    assert stabilized.reset_at == RESET
    assert stabilized.quota_epoch == f"codex-7d:{RESET.isoformat()}"
    assert stabilized.reset_stabilized is True


def test_first_inferred_epoch_does_not_hide_a_later_authoritative_bucket() -> None:
    authoritative = evidence("1", "2", reset_at=RESET + timedelta(minutes=5))

    unchanged = _stabilize_observation_epoch(
        authoritative,
        active_reset_at=RESET,
        active_epoch_authoritative=False,
        reset_bucket_seconds=300,
    )

    assert unchanged is authoritative


def test_zero_or_sparse_evidence_uses_provisional_100_a() -> None:
    result = estimate_weekly_total_a(
        (evidence("0", "0"), evidence("3", "14.1771335")),
        EconomicAccountSyncConfig(),
    )

    assert result.state == "PROVISIONAL"
    assert result.total_a == Decimal("100.000000")


def test_delta_slope_ignores_absolute_startup_offset_and_converges() -> None:
    result = estimate_weekly_total_a(
        (
            evidence("10", "100"),
            evidence("20", "115"),
            evidence("30", "130"),
        ),
        EconomicAccountSyncConfig(),
    )

    assert result.state == "CONVERGED"
    assert result.total_a == Decimal("150.000000")
    assert result.slope_count == 3
    assert result.relative_mad == 0


def test_last_converged_estimate_is_held_during_noisy_followup() -> None:
    result = estimate_weekly_total_a(
        (
            evidence(
                "10",
                "10",
                estimate_state="CONVERGED",
                estimated_total_a=Decimal("148.620000"),
            ),
            evidence("11", "30"),
        ),
        EconomicAccountSyncConfig(),
    )

    assert result.state == "CONVERGED"
    assert result.method == "held_last_converged"
    assert result.total_a == Decimal("148.620000")


class FakeAdmin:
    def __init__(self, accounts: list[dict[str, Any]], usages: dict[int, dict[str, Any]]) -> None:
        self.accounts = accounts
        self.usages = usages
        self.usage_calls: list[tuple[int, dict[str, Any]]] = []

    def iter_accounts(self, **_: Any):
        yield from self.accounts

    def get_account_usage(self, account_id: int, **options: Any) -> dict[str, Any]:
        self.usage_calls.append((account_id, options))
        return self.usages[account_id]


class FakeStore:
    def __init__(self) -> None:
        self.reconciled: list[int] = []
        self.expected: tuple[int, ...] = ()

    def reconcile_account(self, candidate: Any, observation: Any, **_: Any) -> dict[str, Any]:
        self.reconciled.append(candidate.account_id)
        return {
            "account_id": candidate.account_id,
            "estimate_state": "PROVISIONAL",
            "estimated_total_a": "100.000000",
        }

    def disable_missing_accounts(
        self, model: str, account_ids: Any, *, observed_at: datetime
    ) -> int:
        assert model == "gpt-5.6-sol"
        assert observed_at == NOW
        self.expected = tuple(account_ids)
        return 0


def test_sync_auto_discovers_duplicate_names_by_account_id() -> None:
    accounts = [account(45918, notes="\u00a59"), account(45920), account(1, name="other")]
    usages = {45918: usage(10, "20"), 45920: usage()}
    admin = FakeAdmin(accounts, usages)
    store = FakeStore()

    result = sync_owned_accounts(
        admin,
        store,
        config=EconomicAccountSyncConfig(),
        usage_client_factory=lambda: admin,
        now=lambda: NOW,
    )

    assert result["success"] is True
    assert result["matched_account_count"] == 2
    assert result["synced_account_count"] == 2
    assert store.reconciled == [45918, 45920]
    assert store.expected == (45918, 45920)
    assert sorted(admin.usage_calls) == [
        (45918, {"force": True, "source": "active"}),
        (45920, {"force": True, "source": "active"}),
    ]


def test_account_sync_refreshes_contract_once_when_snapshot_sees_superseded_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Facts:
        def __init__(self) -> None:
            self.contract_ids: list[int] = []

        def upsert_source(self, **_: Any) -> SimpleNamespace:
            return SimpleNamespace(source_id=12)

        def record_quota_snapshot(self, *, contract_id: int, **_: Any) -> SimpleNamespace:
            self.contract_ids.append(contract_id)
            if len(self.contract_ids) == 1:
                raise EconomicRoutingError("economic_quota_contract_inactive")
            return SimpleNamespace(snapshot_id=34)

        def update_source_state(self, *_: Any, **__: Any) -> None:
            return None

    store = PostgresEconomicAccountStore(lambda: None)
    facts = Facts()
    store.facts = facts  # type: ignore[assignment]
    contract_ids = iter((56, 57))
    monkeypatch.setattr(store, "_ensure_contract", lambda *_: next(contract_ids))
    monkeypatch.setattr(store, "_load_evidence", lambda *_: ())
    monkeypatch.setattr(store, "_stabilize_epoch", lambda _, item, **__: item)

    result = store.reconcile_account(
        parse_owned_account(account(), EconomicAccountSyncConfig()),  # type: ignore[arg-type]
        evidence("3", "14.1771335"),
        config=EconomicAccountSyncConfig(),
    )

    assert facts.contract_ids == [56, 57]
    assert result["contract_id"] == 57
    assert result["snapshot_id"] == 34


def test_account_sync_units_are_fast_and_owned_by_global_control() -> None:
    service = Path("deploy/systemd/a6-trader-economic-account-sync.service").read_text()
    timer = Path("deploy/systemd/a6-trader-economic-account-sync.timer").read_text()
    target = Path("deploy/systemd/a6-trader-control.target").read_text()

    assert "sync_a6_economic_accounts.py" in service
    assert "TimeoutStartSec=25s" in service
    assert "OnUnitInactiveSec=30s" in timer
    assert "Wants=a6-trader-economic-account-sync.timer" in target
    assert "PropagatesStopTo=a6-trader-economic-account-sync.service" in target

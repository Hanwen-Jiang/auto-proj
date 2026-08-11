from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from a6api_auto.trade_model_publication import (
    CompositePublicationRemote,
    ListingPublicationState,
    ManagedPublicationListing,
    PostgresPublicationDiscovery,
    PublicationDecision,
    PublicationReconcileReport,
    PublicationRecord,
    PublicationState,
    SellerListingCostEvidence,
    plan_seller_cost_guard,
)
from scripts import reconcile_a6_model_publication as command


class _Cursor:
    def __init__(self, database):
        self.database = database
        self.result = None
        self.results = []
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params):
        sql = " ".join(query.split())
        self.database.queries.append((sql, params))
        if "CROSS JOIN unnest" in sql:
            self.results = list(self.database.guard_rows)
            self.result = None
        elif "FROM a6_trade.a6_managed_listing_bindings" in sql:
            self.results = list(self.database.listing_rows)
            self.result = None
        elif "AS provider_callable" in sql:
            self.result = self.database.schedulable_row
            self.results = []
        else:
            self.result = self.database.pool_row
            self.results = []

    def fetchone(self):
        return self.result

    def fetchall(self):
        return self.results


class _Connection:
    def __init__(self, database):
        self.database = database
        self.closed = False

    def cursor(self):
        return _Cursor(self.database)

    def close(self):
        self.closed = True


class _Database:
    def __init__(self):
        self.queries = []
        self.listing_rows = [
            {
                "merchant_id": 17,
                "a6_user_id": 11117,
                "session_ref": "/sessions/merchant-17.json",
                "managed_uid": "listing-17",
                "listing_id": 1397,
                "channel_id": 1397,
                "model": "gpt-5.6-sol",
            },
            {
                "merchant_id": 19,
                "a6_user_id": 11461,
                "session_ref": "/sessions/merchant-19.json",
                "managed_uid": "listing-19",
                "listing_id": 1442,
                "channel_id": 1442,
                "model": "gpt-5.6-sol",
            },
        ]
        self.pool_row = {
            "generation": 157,
            "cost_generation": 157,
            "pricing_state": "active",
            "healthy_slots": 2,
            "controls_fresh": True,
        }
        self.schedulable_row = {
            "account_callable": True,
            "provider_callable": False,
        }
        self.guard_rows = [
            {
                "cost_generation": 157,
                "pool_cost_multiplier": "0.06",
                "pricing_state": "active",
                "managed_uid": "listing-17",
                "price_multiplier": "0.06",
                "delta": "0",
                "conversion_factor": "0.833333333333",
                "margin": "0.20",
                "headroom": "0",
                "price_plan_active": True,
                "remote_price_readback_confirmed": True,
                "seller_binding_verified": True,
                "collector_fresh": True,
                "session_healthy": True,
                "channel_healthy": True,
                "recent_external_success_at": datetime(2026, 8, 1, tzinfo=UTC),
                "recent_external_success_latency_ms": 900,
            },
            {
                "cost_generation": 157,
                "pool_cost_multiplier": "0.06",
                "pricing_state": "active",
                "managed_uid": "listing-19",
                "price_multiplier": "0.12",
                "delta": "0",
                "conversion_factor": "0.833333333333",
                "margin": "0.20",
                "headroom": "0",
                "price_plan_active": True,
                "remote_price_readback_confirmed": True,
                "seller_binding_verified": True,
                "collector_fresh": True,
                "session_healthy": True,
                "channel_healthy": True,
                "recent_external_success_at": datetime(2026, 8, 1, 0, 1, tzinfo=UTC),
                "recent_external_success_latency_ms": 800,
            },
        ]

    def connect(self):
        return _Connection(self)


def test_postgres_discovery_finds_every_managed_current_listing_and_pool_evidence() -> None:
    database = _Database()
    discovery = PostgresPublicationDiscovery(database.connect)

    listings = discovery.listings("gpt-5.6-sol")
    decision = discovery.decision(
        "gpt-5.6-sol",
        minimum_healthy_slots=1,
        redundancy_target=3,
    )

    assert [(item.a6_user_id, item.listing_id) for item in listings] == [
        (11117, 1397),
        (11461, 1442),
    ]
    assert decision.desired_state is PublicationState.DEGRADED
    assert decision.generation == 157
    assert decision.healthy_slots == 2
    rendered = "\n".join(query for query, _ in database.queries)
    assert "binding.control_state <> 'retired'" in rendered
    assert "JOIN a6_trade.a6_supplier_channels_current channel" in rendered
    assert "btrim(channel.name) <> %s" in rendered
    assert "btrim(channel.name) ~ %s" in rendered
    listing_query = next(
        item for item in database.queries if "FROM a6_trade.a6_managed_listing_bindings" in item[0]
    )
    assert listing_query[1] == (
        "gpt-5.6-sol",
        "正常API中转站",
        r"^e[a-z0-9]{9}$",
        r"[0-9]|[^aeiou0-9]{4}",
    )
    assert "merchant.roles @> ARRAY['seller']::text[]" in rendered
    assert "generation.generation_state = 'active'" in rendered
    assert "slot.probe_confirmed" in rendered
    assert "slot.cost_generation = generation.cost_generation" in rendered
    assert "a6_channel_observer_health_current" in rendered
    assert "observer.available" in rendered
    assert "observer.observed_at" in rendered
    assert "count(DISTINCT observer.observer_merchant_id)" in rendered
    assert "generation.updated_at >=" not in rendered
    assert "cost.updated_at >=" not in rendered
    assert "slot.updated_at >=" not in rendered


def test_discovery_keeps_old_stable_generation_publishable_with_fresh_observer_evidence() -> None:
    database = _Database()
    database.pool_row["controls_fresh"] = True

    decision = PostgresPublicationDiscovery(database.connect).decision(
        "gpt-5.6-sol",
        minimum_healthy_slots=1,
        redundancy_target=2,
        control_max_age_seconds=90,
        minimum_health_observers=2,
    )

    assert decision.desired_state is PublicationState.PUBLISHED
    assert decision.reason_code == "healthy"


def test_discovery_builds_per_listing_guard_from_active_cost_and_verified_price_plan() -> None:
    database = _Database()
    discovery = PostgresPublicationDiscovery(database.connect)
    listings = discovery.listings("gpt-5.6-sol")

    plan = discovery.seller_cost_guard_plan("gpt-5.6-sol", listings)

    assert plan.cost_generation == 157
    assert [item.desired_state for item in plan.targets] == [
        ListingPublicationState.UNPUBLISHED,
        ListingPublicationState.PUBLISHED,
    ]
    query, params = next(item for item in database.queries if "CROSS JOIN unnest" in item[0])
    assert "cost.pricing_state = 'active'" in query
    assert "candidate.status = 'completed'" in query
    assert "action.observed_after #>> '{listing,state}'" in query
    assert "listing.input_price_micros" in query
    assert "seller.observed_delta" in query
    assert "candidate.action_type = 'emergency_model_pause'" in query
    assert "action.completed_at > pause.completed_at" in query
    assert "LEFT JOIN a6_trade.enodot_managed_seller_resources" in query
    assert "action.desired_state ->> 'a6_panel_multiplier'" in query
    assert "settlement_fee_rate" not in query
    assert "merchant.last_poll_at >=" in query
    assert "recent_success.occurred_at" in query
    assert "earning.counterparty_class = 'external'" in query
    assert params == (
        900,
        ["listing-17", "listing-19"],
        "gpt-5.6-sol",
        "gpt-5.6-sol",
        "gpt-5.6-sol",
        86400,
        "gpt-5.6-sol",
    )


def test_discovery_without_active_bundle_decides_unpublished() -> None:
    database = _Database()
    database.pool_row = None

    decision = PostgresPublicationDiscovery(database.connect).decision("gpt-5.6-sol")

    assert decision.desired_state is PublicationState.UNPUBLISHED
    assert decision.generation is None


def test_discovery_treats_stale_control_evidence_as_repairing() -> None:
    database = _Database()
    database.pool_row["controls_fresh"] = False

    decision = PostgresPublicationDiscovery(database.connect).decision(
        "gpt-5.6-sol",
        control_max_age_seconds=90,
    )

    assert decision.desired_state is PublicationState.REPAIRING
    assert decision.reason_code == "control_evidence_stale"


class _Discovery:
    def __init__(self) -> None:
        self.calls = []

    def decision(
        self,
        model,
        *,
        minimum_healthy_slots,
        redundancy_target,
        control_max_age_seconds,
        minimum_health_observers,
    ):
        self.calls.append(
            (
                "decision",
                model,
                minimum_healthy_slots,
                redundancy_target,
                control_max_age_seconds,
                minimum_health_observers,
            )
        )
        return PublicationDecision(
            model=model,
            desired_state=PublicationState.DEGRADED,
            healthy_slots=1,
            minimum_healthy_slots=minimum_healthy_slots,
            redundancy_target=redundancy_target,
            reason_code="below_redundancy_target",
            generation=7,
            cost_generation=11,
        )

    def listings(self, model):
        self.calls.append(("listings", model))
        return (
            ManagedPublicationListing(
                merchant_id=1,
                a6_user_id=11117,
                session_ref="/sessions/merchant-1.json",
                managed_uid="listing-1",
                listing_id=1397,
                channel_id=1397,
                model=model,
            ),
        )

    def seller_cost_guard_plan(self, model, listings, **_kwargs):
        return plan_seller_cost_guard(
            model,
            protected_provider_cost="0.01",
            cost_generation=11,
            cost_generation_active=True,
            listings=tuple(
                SellerListingCostEvidence(
                    listing=listing,
                    price_multiplier="0.12",
                    delta="0",
                    conversion_factor="0.833333333333",
                    margin="0.20",
                    headroom="0",
                    price_plan_active=True,
                    remote_price_readback_confirmed=True,
                    seller_binding_verified=True,
                    collector_fresh=True,
                    session_healthy=True,
                    channel_healthy=True,
                    recent_external_success_at=datetime(2026, 8, 1, tzinfo=UTC),
                    recent_external_success_latency_ms=800,
                )
                for listing in listings
            ),
        )


def test_command_is_default_off_and_does_not_touch_discovery() -> None:
    result = command.run_model(
        "gpt-5.6-sol",
        enabled=False,
        apply=True,
    )

    assert result == {
        "success": True,
        "model": "gpt-5.6-sol",
        "enabled": False,
        "apply": False,
        "action": "disabled",
    }
    assert command.parser().parse_args(["--model", "gpt-5.6-sol"]).apply is False
    assert command.parser().parse_args(["--model", "gpt-5.6-sol"]).minimum_healthy_slots == 1
    assert command.parser().parse_args(["--model", "gpt-5.6-sol"]).redundancy_target == 3
    assert (
        command.parser().parse_args(["--model", "gpt-5.6-sol"]).model_publication_reconcile_enabled
        is False
    )
    assert command.resolve_enabled(False, environ={}) is False
    assert (
        command.resolve_enabled(
            False,
            environ={"MODEL_PUBLICATION_RECONCILE_ENABLED": "true"},
        )
        is True
    )
    with pytest.raises(command.ModelPublicationCommandError):
        command.resolve_enabled(
            False,
            environ={"MODEL_PUBLICATION_RECONCILE_ENABLED": "sometimes"},
        )


def test_enabled_dry_run_only_builds_postgres_plan() -> None:
    discovery = _Discovery()

    result = command.run_model(
        "gpt-5.6-sol",
        enabled=True,
        apply=False,
        discovery=discovery,
        controller_factory=lambda _targets: pytest.fail("controller must stay cold"),
        minimum_healthy_slots=1,
        redundancy_target=3,
    )

    assert result["action"] == "planned"
    assert result["target_listing_count"] == 1
    assert discovery.calls == [
        ("decision", "gpt-5.6-sol", 1, 3, 120, 2),
        ("listings", "gpt-5.6-sol"),
    ]


def test_missing_per_listing_cost_guard_fails_closed_before_publication() -> None:
    class MissingGuardDiscovery(_Discovery):
        seller_cost_guard_plan = None

    result = command.run_model(
        "gpt-5.6-sol",
        enabled=True,
        apply=False,
        discovery=MissingGuardDiscovery(),
    )

    assert result["desired_state"] == "unpublished"
    assert result["reason_code"] == "publication_cost_guard_evidence_missing"
    assert "seller_cost_guard" not in result


def test_historical_aggregate_profit_is_not_a_publication_gate() -> None:
    class LegacyProfitDiscovery(_Discovery):
        def runtime_profit_evidence(self, *_args, **_kwargs):
            pytest.fail("historical aggregate profit must not control publication")

    result = command.run_model(
        "gpt-5.6-sol",
        enabled=True,
        apply=False,
        discovery=LegacyProfitDiscovery(),
    )

    assert result["desired_state"] == "degraded"
    assert "runtime_profit_guard" not in result
    assert result["seller_cost_guard"]["publishable_listing_count"] == 1


def test_no_active_generation_dry_run_stays_unpublished_without_loading_cost_guard() -> None:
    class NoActiveDiscovery(_Discovery):
        def decision(self, model, **kwargs):
            del kwargs
            return PublicationDecision(
                model=model,
                desired_state=PublicationState.UNPUBLISHED,
                healthy_slots=0,
                minimum_healthy_slots=1,
                redundancy_target=3,
                reason_code="no_healthy_slots",
                generation=None,
                cost_generation=None,
            )

        def seller_cost_guard_plan(self, *_args):
            pytest.fail("inactive routing must not load residual cost evidence")

    result = command.run_model(
        "gpt-5.6-sol",
        enabled=True,
        apply=False,
        discovery=NoActiveDiscovery(),
    )

    assert result["desired_state"] == "unpublished"
    assert result["reason_code"] == "no_healthy_slots"
    assert result["generation"] is None
    assert result["cost_generation"] is None
    assert "seller_cost_guard" not in result


def test_main_default_off_needs_no_database_or_secret(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "sys.argv",
        ["reconcile_a6_model_publication.py", "--model", "gpt-5.6-sol", "--apply"],
    )
    monkeypatch.delenv("MODEL_PUBLICATION_RECONCILE_ENABLED", raising=False)
    monkeypatch.delenv("A6_TRADE_DATABASE_DSN", raising=False)

    command.main()

    assert json.loads(capsys.readouterr().out)["action"] == "disabled"


class _Client:
    instances = []

    def __init__(self, origin, *, proxy_url="", timeout=0):
        self.origin = origin.rstrip("/")
        self.proxy_url = proxy_url
        self.timeout = timeout
        self.cookies = None
        self.login_calls = []
        self.__class__.instances.append(self)

    def import_cookies(self, cookies):
        self.cookies = dict(cookies)

    def self_user(self, _user_id):
        return None

    def login(self, username, _password):
        self.login_calls.append(username)
        user_id = 11117 if self.proxy_url.endswith("17") else 11461
        self.cookies = {"session": f"refreshed-{user_id}"}
        return {"id": user_id, "username": username}

    def export_cookies(self):
        return dict(self.cookies or {})


class _SessionStore:
    saved = []

    def __init__(self, path):
        self.path = path

    def load(self):
        user_id = 11117 if "17" in self.path.name else 11461
        return SimpleNamespace(
            user_id=user_id,
            origin="https://a6api.com",
            cookies={"session": f"expired-{user_id}"},
            status="supplier_submitted",
            username=f"merchant-{user_id}",
            email=f"merchant-{user_id}@qq.com",
            password="fixture-password",
            supplier_id=1,
            request_id=1,
            updated_at="fixture",
        )

    def save(self, record):
        self.__class__.saved.append((record.user_id, dict(record.cookies)))


def test_apply_wiring_separates_panel_clients_from_one_direct_probe_client(
    monkeypatch, tmp_path
) -> None:
    _Client.instances = []
    _SessionStore.saved = []
    monkeypatch.setattr(command, "A6ApiClient", _Client)
    monkeypatch.setattr(command, "SessionStore", _SessionStore)
    targets = (
        ManagedPublicationListing(
            17,
            11117,
            str(tmp_path / "merchant-17.json"),
            "listing-17",
            1397,
            1397,
            "gpt-5.6-sol",
        ),
        ManagedPublicationListing(
            19,
            11461,
            str(tmp_path / "merchant-19.json"),
            "listing-19",
            1442,
            1442,
            "gpt-5.6-sol",
        ),
    )

    controller = command.build_controller(
        targets,
        connection_factory=lambda: None,
        sessions_dir=tmp_path,
        proxy_by_user_id={11117: "http://panel-17", 11461: "http://panel-19"},
        probe_api_key="sk-probe-fixture",
        config_version="publication-fixture",
    )

    assert isinstance(controller.remote, CompositePublicationRemote)
    primitives = controller.remote.remotes
    assert [remote.listing_client.proxy_url for remote in primitives] == [
        "http://panel-17",
        "http://panel-19",
    ]
    assert primitives[0].probe_client is primitives[1].probe_client
    assert primitives[0].probe_client.origin == "https://a6.a6api.com"
    assert primitives[0].probe_client.proxy_url == ""
    assert primitives[0].probe_client not in {remote.listing_client for remote in primitives}
    assert [remote.listing_client.login_calls for remote in primitives] == [
        ["merchant-11117"],
        ["merchant-11461"],
    ]
    assert _SessionStore.saved == [
        (11117, {"session": "refreshed-11117"}),
        (11461, {"session": "refreshed-11461"}),
    ]


def test_enabled_apply_command_invokes_controller_after_discovery() -> None:
    discovery = _Discovery()

    class _Controller:
        def reconcile(self, decision):
            record = PublicationRecord(
                model=decision.model,
                desired_state=decision.desired_state,
                observed_state=decision.desired_state,
                healthy_slots=decision.healthy_slots,
                minimum_healthy_slots=decision.minimum_healthy_slots,
                redundancy_target=decision.redundancy_target,
                reason_code=decision.reason_code,
                generation=decision.generation,
                state_version=3,
            )
            return PublicationReconcileReport(
                record=record,
                target_listing_state=ListingPublicationState.PUBLISHED,
                observed_listing_state=ListingPublicationState.PUBLISHED,
                action="publish",
                action_disposition="claimed",
                remote_write_performed=True,
                probe_request_id="resp_fixture",
            )

    result = command.run_model(
        "gpt-5.6-sol",
        enabled=True,
        apply=True,
        discovery=discovery,
        controller_factory=lambda _targets, _plan=None: _Controller(),
        minimum_healthy_slots=1,
        redundancy_target=3,
    )

    assert result["action"] == "publish"
    assert result["remote_write_performed"] is True
    assert result["probe_request_id"] == "resp_fixture"


def test_enabled_apply_completes_claimed_durable_reconcile_request() -> None:
    discovery = _Discovery()
    events = []
    claim = SimpleNamespace(
        request_id=73,
        request_source="provider_failure",
        attempt_count=2,
    )

    class _Queue:
        def claim_next(self, model):
            events.append(("claim", model))
            return claim

        def complete(self, received, result):
            events.append(("complete", received.request_id, result["observed_state"]))

        def fail(self, *_args):
            pytest.fail("successful reconcile must not fail its request")

    class _Controller:
        def reconcile(self, decision):
            return PublicationReconcileReport(
                record=PublicationRecord(
                    model=decision.model,
                    desired_state=decision.desired_state,
                    observed_state=decision.desired_state,
                    healthy_slots=decision.healthy_slots,
                    minimum_healthy_slots=decision.minimum_healthy_slots,
                    redundancy_target=decision.redundancy_target,
                    reason_code=decision.reason_code,
                    generation=decision.generation,
                    state_version=3,
                ),
                target_listing_state=ListingPublicationState.PUBLISHED,
                observed_listing_state=ListingPublicationState.PUBLISHED,
                action="publish",
                action_disposition="completed",
                remote_write_performed=False,
            )

    result = command.run_model(
        "gpt-5.6-sol",
        enabled=True,
        apply=True,
        discovery=discovery,
        controller_factory=lambda _targets, _plan=None: _Controller(),
        request_queue=_Queue(),  # type: ignore[arg-type]
    )

    assert result["reconcile_request"] == {
        "id": 73,
        "source": "provider_failure",
        "attempt_count": 2,
    }
    assert events == [
        ("claim", "gpt-5.6-sol"),
        ("complete", 73, "degraded"),
    ]


def test_failed_remote_saga_leaves_durable_request_pending_for_next_run() -> None:
    calls = []

    class _Queue:
        def claim_next(self, model):
            calls.append(("claim", model))
            pytest.fail("request must stay pending until remote readback succeeds")

        def complete(self, *_args):
            pytest.fail("failed reconcile must not complete its request")

    class _Controller:
        def reconcile(self, _decision):
            raise RuntimeError("REMOTE_SECRET_MUST_NOT_LEAK")

    with pytest.raises(RuntimeError, match="REMOTE_SECRET_MUST_NOT_LEAK"):
        command.run_model(
            "gpt-5.6-sol",
            enabled=True,
            apply=True,
            discovery=_Discovery(),
            controller_factory=lambda _targets, _plan=None: _Controller(),
            request_queue=_Queue(),  # type: ignore[arg-type]
        )

    assert calls == []


def test_systemd_template_is_default_off_and_uses_separate_probe_secret() -> None:
    root = Path(__file__).resolve().parents[1]
    unit = (root / "deploy/systemd/a6-trader-model-publication.service").read_text()
    timer = (root / "deploy/systemd/a6-trader-model-publication.timer").read_text()
    path = (root / "deploy/systemd/a6-trader-model-publication.path").read_text()

    assert "MODEL_PUBLICATION_RECONCILE_ENABLED" in unit
    assert "--model-publication-reconcile-enabled" not in unit
    assert "--probe-api-key-file" in unit
    assert "--proxy-map-file" in unit
    assert "--minimum-healthy-slots 1" in unit
    assert "--redundancy-target 1" in unit
    assert "/usr/bin/flock --exclusive /var/lib/a6-trader/seller-control.lock" in unit
    assert "--runtime-profit" not in unit
    assert "--seller-collector-freshness-seconds 900" in unit
    assert "--seller-success-lookback-seconds 86400" in unit
    assert "--maximum-published-listings" not in unit
    assert "--config-version a6-provider-publication-v6-per-listing-cost" in unit
    assert "TimeoutStartSec=10min" in unit
    assert "reconcile_a6_model_publication.py" in unit
    assert "ReadWritePaths=/var/lib/a6-trader" in unit
    assert "OnUnitActiveSec=5s" in timer
    assert "PathChanged=/run/a6-trader/model-publication-request" in path
    assert "Unit=a6-trader-model-publication.service" in path

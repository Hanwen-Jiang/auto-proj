from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from a6api_auto.trade_model_publication import (
    A6SupplierListingPublicationRemote,
    CompositePublicationRemote,
    InMemoryPublicationActionLedger,
    InMemoryPublicationStore,
    ListingPublicationState,
    PostgresPublicationActionLedger,
    PostgresPublicationModelLease,
    PostgresPublicationStore,
    PublicationActionDisposition,
    PublicationController,
    PublicationError,
    PublicationProbeResult,
    PublicationReconciler,
    PublicationState,
    SellerCostGuardPublicationRemote,
    SellerListingCostEvidence,
    apply_seller_cost_guard,
    decide_publication,
    plan_seller_cost_guard,
    seller_listing_provider_cost_ceiling,
)


class _PublicationCursor:
    def __init__(self, connection):
        self.connection = connection
        self.row = None
        self.description = None
        self.rowcount = -1

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=None):
        normalized = " ".join(query.split())
        self.connection.queries.append((normalized, params))
        if normalized.startswith("SELECT model, desired_state"):
            self.row = self.connection.row
        elif normalized.startswith("INSERT INTO a6_trade.model_publications"):
            (
                model,
                desired,
                healthy,
                minimum,
                redundancy_target,
                reason,
                generation,
                cost_generation,
            ) = params
            previous = self.connection.row
            observed = "unpublished" if previous is None else previous["observed_state"]
            self.connection.row = {
                "model": model,
                "desired_state": desired,
                "observed_state": observed,
                "healthy_slots": healthy,
                "min_healthy_slots": minimum,
                "redundancy_target": redundancy_target,
                "reason_code": reason,
                "desired_generation": generation,
                "desired_cost_generation": cost_generation,
                "state_version": 1 if previous is None else previous["state_version"] + 1,
                "publication_scope": (
                    None if previous is None else previous.get("publication_scope")
                ),
                "action_started_at": None,
                "confirmed_at": None if previous is None else previous.get("confirmed_at"),
                "last_real_probe_at": (
                    None if previous is None else previous.get("last_real_probe_at")
                ),
                "last_real_probe_request_id": (
                    None if previous is None else previous.get("last_real_probe_request_id")
                ),
            }
            self.row = self.connection.row
        elif normalized.startswith("UPDATE a6_trade.model_publications"):
            if "SET publication_scope" in normalized:
                scope, model, version, _ = params
                if (
                    self.connection.row is None
                    or self.connection.row["model"] != model
                    or self.connection.row["state_version"] != version
                    or self.connection.row.get("publication_scope") == scope
                ):
                    self.row = None
                else:
                    self.connection.row["publication_scope"] = scope
                    self.connection.row["action_started_at"] = None
                    self.connection.row["last_real_probe_at"] = None
                    self.connection.row["last_real_probe_request_id"] = None
                    self.connection.row["state_version"] += 1
                    self.row = self.connection.row
                return self
            if "SET action_started_at" in normalized:
                model, version = params
                if (
                    self.connection.row is None
                    or self.connection.row["model"] != model
                    or self.connection.row["state_version"] != version
                ):
                    self.row = None
                else:
                    self.connection.row["action_started_at"] = "started"
                    self.connection.row["state_version"] += 1
                    self.row = self.connection.row
                return self
            state, probe_confirmed, _, probe_request_id, model, version = params
            if (
                self.connection.row is None
                or self.connection.row["model"] != model
                or self.connection.row["state_version"] != version
            ):
                self.row = None
            else:
                self.connection.row["observed_state"] = state
                self.connection.row["state_version"] += 1
                self.connection.row["confirmed_at"] = "confirmed"
                if probe_confirmed:
                    self.connection.row["last_real_probe_at"] = "probed"
                    self.connection.row["last_real_probe_request_id"] = probe_request_id
                self.row = self.connection.row
        return self

    def fetchone(self):
        return self.row


class _PublicationConnection:
    def __init__(self):
        self.row = None
        self.queries = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return _PublicationCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


@pytest.mark.parametrize("healthy_slots", [1, 2])
def test_publication_degrades_but_stays_available_below_redundancy_target(
    healthy_slots: int,
) -> None:
    decision = decide_publication(
        "gpt-5.6-sol",
        healthy_slots=healthy_slots,
        minimum_healthy_slots=1,
        redundancy_target=3,
        generation=4,
        bundle_confirmed=True,
    )
    assert decision.desired_state is PublicationState.DEGRADED
    assert decision.reason_code == "below_redundancy_target"


def test_publication_unpublishes_below_configured_publish_floor() -> None:
    decision = decide_publication(
        "gpt-5.6-sol",
        healthy_slots=1,
        minimum_healthy_slots=2,
        redundancy_target=3,
        generation=4,
        bundle_confirmed=True,
    )

    assert decision.desired_state is PublicationState.UNPUBLISHED
    assert decision.reason_code == "below_publish_floor"


def test_publication_unpublishes_when_no_healthy_slot_exists() -> None:
    decision = decide_publication(
        "gpt-5.6-sol",
        healthy_slots=0,
        generation=4,
        bundle_confirmed=True,
    )
    assert decision.desired_state is PublicationState.UNPUBLISHED


def test_publication_reconciler_is_idempotent() -> None:
    store = InMemoryPublicationStore()
    reconciler = PublicationReconciler(store)
    decision = decide_publication(
        "gpt-5.6-sol",
        healthy_slots=2,
        generation=4,
        bundle_confirmed=True,
    )
    first = reconciler.reconcile(decision)
    second = reconciler.reconcile(decision)
    assert first == second
    assert first.observed_state is PublicationState.UNPUBLISHED


def test_publication_reconciler_persists_minimum_slot_config_change() -> None:
    store = InMemoryPublicationStore()
    reconciler = PublicationReconciler(store)
    first = decide_publication(
        "gpt-5.6-sol",
        healthy_slots=0,
        minimum_healthy_slots=1,
        generation=4,
        bundle_confirmed=True,
    )
    changed = decide_publication(
        "gpt-5.6-sol",
        healthy_slots=0,
        minimum_healthy_slots=3,
        generation=4,
        bundle_confirmed=True,
    )

    before = reconciler.reconcile(first)
    after = reconciler.reconcile(changed)

    assert after.minimum_healthy_slots == 3
    assert after.state_version == before.state_version + 1


def test_publication_reconciler_persists_redundancy_target_change() -> None:
    store = InMemoryPublicationStore()
    reconciler = PublicationReconciler(store)
    first = decide_publication(
        "gpt-5.6-sol",
        healthy_slots=2,
        minimum_healthy_slots=1,
        redundancy_target=2,
        generation=4,
        bundle_confirmed=True,
    )
    changed = decide_publication(
        "gpt-5.6-sol",
        healthy_slots=2,
        minimum_healthy_slots=1,
        redundancy_target=3,
        generation=4,
        bundle_confirmed=True,
    )

    before = reconciler.reconcile(first)
    after = reconciler.reconcile(changed)

    assert before.desired_state is PublicationState.PUBLISHED
    assert after.desired_state is PublicationState.DEGRADED
    assert after.redundancy_target == 3
    assert after.state_version == before.state_version + 1


def test_new_desired_state_preserves_last_observed_confirmation_and_resets_action() -> None:
    store = InMemoryPublicationStore()
    planned = store.write(_decision())
    started = store.mark_action_started(planned.model, expected_state_version=planned.state_version)
    confirmed = store.confirm(
        planned.model,
        PublicationState.DEGRADED,
        expected_state_version=started.state_version,
        real_probe_confirmed=True,
        probe_request_id="resp_previous",
    )

    changed = store.write(_decision(healthy_slots=0, generation=10))

    assert changed.observed_state is PublicationState.DEGRADED
    assert changed.confirmed_at == confirmed.confirmed_at
    assert changed.action_started_at is None
    assert changed.last_real_probe_at is None
    assert changed.last_real_probe_request_id is None


def test_publication_rejects_nonpositive_generation() -> None:
    with pytest.raises(PublicationError, match="publication_generation_invalid"):
        decide_publication(
            "gpt-5.6-sol",
            healthy_slots=1,
            generation=0,
            bundle_confirmed=True,
        )


def test_publication_rejects_redundancy_target_below_publish_floor() -> None:
    with pytest.raises(PublicationError, match="publication_redundancy_target_invalid"):
        decide_publication(
            "gpt-5.6-sol",
            healthy_slots=2,
            minimum_healthy_slots=2,
            redundancy_target=1,
            generation=4,
            bundle_confirmed=True,
        )


def test_postgres_publication_store_round_trips_desired_and_observed_state() -> None:
    connection = _PublicationConnection()
    store = PostgresPublicationStore(lambda: connection)
    decision = decide_publication(
        "gpt-5.6-sol",
        healthy_slots=1,
        minimum_healthy_slots=1,
        redundancy_target=3,
        generation=7,
        bundle_confirmed=True,
    )

    planned = store.write(decision)
    assert planned.desired_state is PublicationState.DEGRADED
    assert planned.observed_state is PublicationState.UNPUBLISHED

    started = store.mark_action_started("gpt-5.6-sol", expected_state_version=planned.state_version)
    assert started.action_started_at == "started"
    observed = store.observe("gpt-5.6-sol", PublicationState.DEGRADED)
    assert observed.observed_state is PublicationState.DEGRADED
    assert store.read("gpt-5.6-sol") == observed
    assert connection.commits == 3
    assert connection.rollbacks == 0
    assert connection.closed
    assert any("pg_advisory_xact_lock" in query for query, _ in connection.queries)


class _Remote:
    def __init__(
        self,
        state=ListingPublicationState.UNPUBLISHED,
        *,
        probe_error: Exception | None = None,
        publication_scope: str = "fixture:model-listings",
    ) -> None:
        self.publication_scope = publication_scope
        self.state = state
        self.probe_error = probe_error
        self.calls = []

    def read_listing(self, model):
        self.calls.append(("read", model))
        return self.state

    def set_listing_state(self, model, state, *, idempotency_key):
        self.calls.append(("write", model, state, idempotency_key))
        self.state = state

    def probe(self, model):
        self.calls.append(("probe", model))
        if self.probe_error is not None:
            raise self.probe_error
        return PublicationProbeResult(request_id="resp_probe_fixture")


def _decision(*, healthy_slots=1, generation=9, cost_generation=4, bundle_confirmed=True):
    return decide_publication(
        "gpt-5.6-sol",
        healthy_slots=healthy_slots,
        minimum_healthy_slots=1,
        redundancy_target=3,
        generation=generation,
        cost_generation=cost_generation,
        bundle_confirmed=bundle_confirmed,
    )


def test_publication_controller_probes_before_publish_then_confirms_readback() -> None:
    store = InMemoryPublicationStore()
    remote = _Remote()
    controller = PublicationController(store, remote)

    report = controller.reconcile(_decision())

    assert report.record.observed_state is PublicationState.DEGRADED
    assert report.record.last_real_probe_request_id == "resp_probe_fixture"
    assert report.record.last_real_probe_at is not None
    assert report.record.action_started_at is not None
    assert report.record.state_version == 4
    assert report.remote_write_performed is True
    assert [call[0] for call in remote.calls] == ["read", "probe", "write", "read"]
    assert remote.calls[2][2] is ListingPublicationState.PUBLISHED

    second = controller.reconcile(_decision())
    assert second.action_disposition == "noop"
    assert second.remote_write_performed is False
    assert second.record.state_version == report.record.state_version
    assert [call[0] for call in remote.calls][-1] == "read"


def test_new_publication_generation_invalidates_probe_and_probes_again() -> None:
    store = InMemoryPublicationStore()
    remote = _Remote()
    controller = PublicationController(store, remote)
    controller.reconcile(_decision(generation=9))

    report = controller.reconcile(_decision(generation=10))

    assert report.record.generation == 10
    assert [call[0] for call in remote.calls].count("probe") == 2
    assert [call[0] for call in remote.calls].count("write") == 1


def test_cost_only_generation_change_creates_new_action_and_probe() -> None:
    store = InMemoryPublicationStore()
    remote = _Remote()
    ledger = InMemoryPublicationActionLedger()
    controller = PublicationController(store, remote, ledger=ledger)
    controller.reconcile(_decision(generation=9, cost_generation=4))

    report = controller.reconcile(_decision(generation=9, cost_generation=5))

    assert report.record.cost_generation == 5
    assert [call[0] for call in remote.calls].count("probe") == 2
    assert len(ledger.completed) == 2


def test_listing_scope_change_invalidates_old_probe_evidence() -> None:
    store = InMemoryPublicationStore()
    first_remote = _Remote(publication_scope="fixture:listings:v1")
    PublicationController(store, first_remote).reconcile(_decision())
    second_remote = _Remote(
        ListingPublicationState.PUBLISHED,
        publication_scope="fixture:listings:v2",
    )

    report = PublicationController(store, second_remote).reconcile(_decision())

    assert report.record.publication_scope == "fixture:listings:v2"
    assert [call[0] for call in second_remote.calls] == ["read", "probe", "read"]


def test_publication_controller_does_not_publish_when_real_probe_fails() -> None:
    store = InMemoryPublicationStore()
    remote = _Remote(probe_error=PublicationError("probe_fixture_failed"))

    with pytest.raises(PublicationError, match="probe_fixture_failed"):
        PublicationController(store, remote).reconcile(_decision())

    assert [call[0] for call in remote.calls] == ["read", "probe"]
    record = store.read("gpt-5.6-sol")
    assert record is not None
    assert record.observed_state is PublicationState.UNPUBLISHED
    assert record.action_started_at is not None
    assert record.state_version == 3


def test_publication_controller_busy_claim_does_not_mark_action_started() -> None:
    decision = _decision()
    remote = _Remote()
    ledger = InMemoryPublicationActionLedger()
    claimed = ledger.claim(
        decision,
        ListingPublicationState.PUBLISHED,
        publication_scope=remote.publication_scope,
    )
    assert claimed.disposition is PublicationActionDisposition.CLAIMED
    store = InMemoryPublicationStore()

    report = PublicationController(store, remote, ledger=ledger).reconcile(decision)

    assert report.action_disposition == "busy"
    record = store.read(decision.model)
    assert record is not None
    assert record.action_started_at is None
    assert record.state_version == 2


def test_publication_controller_unpublishes_without_spending_probe() -> None:
    store = InMemoryPublicationStore()
    remote = _Remote(ListingPublicationState.PUBLISHED)

    report = PublicationController(store, remote).reconcile(_decision(healthy_slots=0))

    assert report.record.observed_state is PublicationState.UNPUBLISHED
    assert report.observed_listing_state is ListingPublicationState.UNPUBLISHED
    assert [call[0] for call in remote.calls] == ["read", "write", "read"]


def _cost_evidence(
    listing_id: int,
    *,
    price_multiplier: str,
    delta: str = "0",
    price_plan_active: bool = True,
    readback: bool = True,
    binding_verified: bool = True,
    collector_fresh: bool = True,
    session_healthy: bool = True,
    channel_healthy: bool = True,
    external_success: bool = True,
) -> SellerListingCostEvidence:
    from a6api_auto.trade_model_publication import ManagedPublicationListing

    return SellerListingCostEvidence(
        listing=ManagedPublicationListing(
            merchant_id=listing_id,
            a6_user_id=10_000 + listing_id,
            session_ref=f"/sessions/{listing_id}.json",
            managed_uid=f"listing-{listing_id}",
            listing_id=listing_id,
            channel_id=listing_id,
            model="gpt-5.6-sol",
        ),
        price_multiplier=price_multiplier,
        delta=delta,
        conversion_factor="0.833333333333",
        margin="0.20",
        headroom="0.05",
        price_plan_active=price_plan_active,
        remote_price_readback_confirmed=readback,
        seller_binding_verified=binding_verified,
        collector_fresh=collector_fresh,
        session_healthy=session_healthy,
        channel_healthy=channel_healthy,
        recent_external_success_at=(
            datetime(2026, 8, 1, tzinfo=UTC) + timedelta(seconds=listing_id)
            if external_success
            else None
        ),
        recent_external_success_latency_ms=(1000 + listing_id if external_success else None),
    )


def test_seller_listing_cost_ceiling_inverts_a_d_x_contract() -> None:
    ceiling = seller_listing_provider_cost_ceiling(
        price_multiplier="0.10",
        delta="0.44",
        conversion_factor="0.833333333333",
        margin="0.20",
        headroom="0",
    )

    assert ceiling == pytest.approx(Decimal("0.10"))


def test_cost_guard_unpublishes_only_listings_below_current_provider_cost() -> None:
    low = _cost_evidence(1, price_multiplier="0.06")
    high = _cost_evidence(2, price_multiplier="0.12")
    plan = plan_seller_cost_guard(
        "gpt-5.6-sol",
        protected_provider_cost="0.06",
        cost_generation=12,
        cost_generation_active=True,
        listings=(low, high),
    )

    assert [target.desired_state for target in plan.targets] == [
        ListingPublicationState.UNPUBLISHED,
        ListingPublicationState.PUBLISHED,
    ]
    assert plan.targets[0].reason_code == "provider_cost_above_listing_ceiling"


def test_cost_guard_keeps_every_cost_safe_listing_publishable() -> None:
    plan = plan_seller_cost_guard(
        "gpt-5.6-sol",
        protected_provider_cost="0.01",
        cost_generation=12,
        cost_generation_active=True,
        listings=(
            _cost_evidence(1, price_multiplier="0.12"),
            _cost_evidence(3, price_multiplier="0.12"),
            _cost_evidence(2, price_multiplier="0.12"),
        ),
    )

    assert plan.publishable_listing_count == 3
    assert all(target.desired_state is ListingPublicationState.PUBLISHED for target in plan.targets)
    assert all(target.reason_code != "publication_batch_deferred" for target in plan.targets)


def test_cost_guard_keeps_listing_unpublished_without_external_success() -> None:
    evidence = _cost_evidence(
        1,
        price_multiplier="0.12",
        external_success=False,
    )

    plan = plan_seller_cost_guard(
        "gpt-5.6-sol",
        protected_provider_cost="0.01",
        cost_generation=12,
        cost_generation_active=True,
        listings=(evidence,),
    )

    assert plan.publishable_listing_count == 0
    assert plan.targets[0].reason_code == "seller_external_success_missing"


def test_cost_guard_stops_model_above_highest_listing_ceiling() -> None:
    plan = plan_seller_cost_guard(
        "gpt-5.6-sol",
        protected_provider_cost="0.20",
        cost_generation=12,
        cost_generation_active=True,
        listings=(
            _cost_evidence(1, price_multiplier="0.06"),
            _cost_evidence(2, price_multiplier="0.12"),
        ),
    )

    guarded = apply_seller_cost_guard(_decision(cost_generation=12), plan)

    assert guarded.desired_state is PublicationState.UNPUBLISHED
    assert guarded.reason_code == "provider_cost_above_all_listing_ceilings"


@pytest.mark.parametrize(
    ("generation_active", "plan_active", "readback", "reason"),
    [
        (False, True, True, "provider_cost_generation_not_active"),
        (True, False, True, "seller_price_plan_not_active"),
        (True, True, False, "seller_price_readback_unconfirmed"),
    ],
)
def test_cost_guard_requires_active_generation_and_remote_price_confirmation_to_republish(
    generation_active, plan_active, readback, reason
) -> None:
    plan = plan_seller_cost_guard(
        "gpt-5.6-sol",
        protected_provider_cost="0.01",
        cost_generation=12,
        cost_generation_active=generation_active,
        listings=(
            _cost_evidence(
                1,
                price_multiplier="0.12",
                price_plan_active=plan_active,
                readback=readback,
            ),
        ),
    )

    assert plan.targets[0].desired_state is ListingPublicationState.UNPUBLISHED
    assert plan.targets[0].reason_code == reason
    assert (
        apply_seller_cost_guard(_decision(cost_generation=12), plan).reason_code
        == "seller_price_recovery_unconfirmed"
    )


@pytest.mark.parametrize(
    ("evidence_kwargs", "reason"),
    [
        ({"binding_verified": False}, "seller_binding_unverified"),
        ({"collector_fresh": False}, "seller_collector_stale"),
        ({"session_healthy": False}, "seller_session_unhealthy"),
        ({"channel_healthy": False}, "seller_channel_unhealthy"),
        ({"external_success": False}, "seller_external_success_missing"),
    ],
)
def test_cost_guard_canary_fails_closed_without_fresh_operational_evidence(
    evidence_kwargs, reason
) -> None:
    plan = plan_seller_cost_guard(
        "gpt-5.6-sol",
        protected_provider_cost="0.01",
        cost_generation=12,
        cost_generation_active=True,
        listings=(_cost_evidence(1, price_multiplier="0.12", **evidence_kwargs),),
    )

    assert plan.publishable_listing_count == 0
    assert plan.targets[0].reason_code == reason


def test_cost_guard_remote_converges_mixed_listing_targets() -> None:
    low = _cost_evidence(1, price_multiplier="0.06")
    high = _cost_evidence(2, price_multiplier="0.12")
    plan = plan_seller_cost_guard(
        "gpt-5.6-sol",
        protected_provider_cost="0.06",
        cost_generation=12,
        cost_generation_active=True,
        listings=(low, high),
    )
    low_remote = _Remote(ListingPublicationState.PUBLISHED, publication_scope="low")
    high_remote = _Remote(ListingPublicationState.UNPUBLISHED, publication_scope="high")
    remote = SellerCostGuardPublicationRemote(
        {
            (low.listing.a6_user_id, low.listing.listing_id): low_remote,
            (high.listing.a6_user_id, high.listing.listing_id): high_remote,
        },
        plan=plan,
        probe_remote=high_remote,
    )

    remote.set_listing_state(
        "gpt-5.6-sol",
        ListingPublicationState.PUBLISHED,
        idempotency_key="guard-fixture",
    )

    assert remote.read_listing("gpt-5.6-sol") is ListingPublicationState.PUBLISHED
    assert low_remote.state is ListingPublicationState.UNPUBLISHED
    assert high_remote.state is ListingPublicationState.PUBLISHED


def test_composite_publication_converges_mixed_listings_before_confirmation() -> None:
    published = _Remote(
        ListingPublicationState.PUBLISHED,
        publication_scope="fixture:listing:1",
    )
    unpublished = _Remote(publication_scope="fixture:listing:2")
    remote = CompositePublicationRemote(
        (published, unpublished),
        probe_remote=published,
    )

    report = PublicationController(InMemoryPublicationStore(), remote).reconcile(_decision())

    assert report.observed_listing_state is ListingPublicationState.PUBLISHED
    assert report.record.observed_state is PublicationState.DEGRADED
    assert [call[0] for call in published.calls].count("probe") == 1
    assert [call[0] for call in published.calls].count("write") == 0
    assert [call[0] for call in unpublished.calls].count("write") == 1


def test_publication_action_identity_includes_managed_listing_scope() -> None:
    ledger = InMemoryPublicationActionLedger()
    first = ledger.claim(
        _decision(),
        ListingPublicationState.PUBLISHED,
        publication_scope="a6-supplier:11117:listing:1397",
    )
    second = ledger.claim(
        _decision(),
        ListingPublicationState.PUBLISHED,
        publication_scope="a6-supplier:11461:listing:1442",
    )

    assert first.idempotency_key != second.idempotency_key


def test_completed_publish_action_recovers_confirmation_from_remote_readback() -> None:
    decision = _decision()
    ledger = InMemoryPublicationActionLedger()
    store = InMemoryPublicationStore()
    remote = _Remote(ListingPublicationState.PUBLISHED)

    prior = ledger.claim(
        decision,
        ListingPublicationState.PUBLISHED,
        publication_scope=remote.publication_scope,
    )
    assert prior.disposition is PublicationActionDisposition.CLAIMED
    ledger.complete(prior, observed_after={"listing_state": "published"}, elapsed_ms=1)
    report = PublicationController(store, remote, ledger=ledger).reconcile(decision)

    assert report.action_disposition == "completed"
    assert report.remote_write_performed is False
    assert [call[0] for call in remote.calls] == ["read", "probe", "read"]
    assert report.record.observed_state is PublicationState.DEGRADED


def test_publication_confirmation_rejects_stale_state_version() -> None:
    store = InMemoryPublicationStore()
    original = store.write(_decision())
    store.write(_decision(generation=10))

    with pytest.raises(PublicationError, match="publication_confirmation_cas_mismatch"):
        store.confirm(
            "gpt-5.6-sol",
            PublicationState.DEGRADED,
            expected_state_version=original.state_version,
            real_probe_confirmed=True,
        )


def test_old_cost_generation_confirmation_is_rejected_after_cost_replan() -> None:
    store = InMemoryPublicationStore()
    old = store.write(_decision(generation=9, cost_generation=4))
    old = store.bind_scope(
        old.model,
        "fixture:model-listings",
        expected_state_version=old.state_version,
    )
    store.write(_decision(generation=9, cost_generation=5))

    with pytest.raises(PublicationError, match="publication_confirmation_cas_mismatch"):
        store.confirm(
            old.model,
            PublicationState.DEGRADED,
            expected_state_version=old.state_version,
            real_probe_confirmed=True,
        )


class _A6PublicationClient:
    def __init__(self) -> None:
        self.status = "0"
        self.availability = "1"
        self.visibility = "1"
        self.calls = []

    def supplier_listings(self, user_id, *, page, size, status=None):
        self.calls.append(("list", user_id, page, size, status))
        if page == 1:
            return {"items": [{"id": 1, "status": "1"}] * size}
        return {
            "items": [
                {
                    "id": 88,
                    "status": self.status,
                    "availability": self.availability,
                    "visibility": self.visibility,
                    "own_model": "gpt-5.6-sol",
                    "platform_model": "gpt-5.6-sol",
                }
            ]
        }

    def takedown_supplier_listing(self, user_id, listing_id):
        self.calls.append(("takedown", user_id, listing_id))
        self.status = "0"

    def resubmit_supplier_listing(self, user_id, listing_id):
        self.calls.append(("resubmit", user_id, listing_id))
        self.status = "2"


class _DirectProbeClient:
    def __init__(self, *, proxy_url="") -> None:
        self.proxy_url = proxy_url
        self.calls = []

    def provider_responses_probe(self, api_key, model, *, max_output_tokens, timeout):
        self.calls.append(("probe", api_key, model, max_output_tokens, timeout))
        return 200, {
            "probe_event": "response.created",
            "response_id": "resp_a6_fixture",
        }


def test_a6_publication_remote_pages_readback_and_maps_actions_and_probe() -> None:
    listing_client = _A6PublicationClient()
    probe_client = _DirectProbeClient()
    remote = A6SupplierListingPublicationRemote(
        listing_client,
        probe_client=probe_client,
        user_id=11117,
        listing_id=88,
        probe_api_key="sk-probe-fixture",
        page_size=2,
    )

    assert remote.read_listing("gpt-5.6-sol") is ListingPublicationState.UNPUBLISHED
    remote.set_listing_state(
        "gpt-5.6-sol",
        ListingPublicationState.PUBLISHED,
        idempotency_key="publication-fixture",
    )
    assert remote.read_listing("gpt-5.6-sol") is ListingPublicationState.PUBLISHED
    assert remote.probe("gpt-5.6-sol") == PublicationProbeResult("resp_a6_fixture")
    assert [call[0] for call in listing_client.calls].count("list") == 4
    assert any(call[0] == "resubmit" for call in listing_client.calls)
    assert [call[0] for call in probe_client.calls] == ["probe"]
    assert all(call[0] != "probe" for call in listing_client.calls)


def test_a6_publication_remote_accepts_numeric_zero_and_active_wire_states() -> None:
    listing_client = _A6PublicationClient()
    remote = A6SupplierListingPublicationRemote(
        listing_client,
        probe_client=_DirectProbeClient(),
        user_id=11117,
        listing_id=88,
        probe_api_key="sk-probe-fixture",
        page_size=2,
    )
    listing_client.status = 0
    assert remote.read_listing("gpt-5.6-sol") is ListingPublicationState.UNPUBLISHED

    listing_client.status = 2
    listing_client.availability = 1
    listing_client.visibility = 1
    assert remote.read_listing("gpt-5.6-sol") is ListingPublicationState.PUBLISHED


def test_a6_publication_remote_requires_independent_direct_probe_client() -> None:
    listing_client = _A6PublicationClient()
    with pytest.raises(ValueError, match="publication_probe_client_not_independent"):
        A6SupplierListingPublicationRemote(
            listing_client,
            probe_client=listing_client,
            user_id=11117,
            listing_id=88,
            probe_api_key="sk-probe-fixture",
        )
    with pytest.raises(ValueError, match="publication_probe_proxy_forbidden"):
        A6SupplierListingPublicationRemote(
            listing_client,
            probe_client=_DirectProbeClient(proxy_url="http://panel-proxy"),
            user_id=11117,
            listing_id=88,
            probe_api_key="sk-probe-fixture",
        )

    direct = _DirectProbeClient()
    remote = A6SupplierListingPublicationRemote(
        listing_client,
        probe_client=direct,
        user_id=11117,
        listing_id=88,
        probe_api_key="sk-probe-fixture",
    )
    direct.proxy_url = "http://late-panel-proxy"
    with pytest.raises(PublicationError, match="publication_probe_proxy_forbidden"):
        remote.probe("gpt-5.6-sol")


@pytest.mark.parametrize(
    ("status", "availability", "visibility"),
    [
        ("1", "1", "1"),
        ("2", "0", "1"),
        ("2", "1", "0"),
    ],
)
def test_a6_publication_remote_rejects_pending_or_not_publicly_active_listing(
    status, availability, visibility
) -> None:
    listing_client = _A6PublicationClient()
    listing_client.status = status
    listing_client.availability = availability
    listing_client.visibility = visibility
    remote = A6SupplierListingPublicationRemote(
        listing_client,
        probe_client=_DirectProbeClient(),
        user_id=11117,
        listing_id=88,
        probe_api_key="sk-probe-fixture",
        page_size=2,
    )

    with pytest.raises(PublicationError, match="publication_listing_state_unknown"):
        remote.read_listing("gpt-5.6-sol")


class _LeaseCursor:
    def __init__(self, connection):
        self.connection = connection
        self.result = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params):
        self.connection.calls.append((" ".join(query.split()), params))
        self.result = (self.connection.locked,)

    def fetchone(self):
        return self.result


class _LeaseConnection:
    def __init__(self, *, locked=True):
        self.locked = locked
        self.calls = []
        self.commits = 0
        self.closed = False

    def cursor(self):
        return _LeaseCursor(self)

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


def test_postgres_publication_model_lease_holds_across_controller_remote_saga() -> None:
    connection = _LeaseConnection()
    store = InMemoryPublicationStore()
    remote = _Remote()
    controller = PublicationController(
        store,
        remote,
        model_lease=PostgresPublicationModelLease(lambda: connection),
    )

    controller.reconcile(_decision())

    assert "pg_try_advisory_lock" in connection.calls[0][0]
    assert "pg_advisory_unlock" in connection.calls[-1][0]
    assert connection.commits == 2
    assert connection.closed is True


def test_postgres_publication_model_lease_rejects_concurrent_reconciler() -> None:
    connection = _LeaseConnection(locked=False)
    controller = PublicationController(
        InMemoryPublicationStore(),
        _Remote(),
        model_lease=PostgresPublicationModelLease(lambda: connection),
    )

    with pytest.raises(PublicationError, match="publication_model_lease_busy"):
        controller.reconcile(_decision())


def test_controller_recovers_remote_write_after_process_stopped_before_confirmation() -> None:
    decision = _decision()
    remote = _Remote(ListingPublicationState.PUBLISHED)
    ledger = InMemoryPublicationActionLedger()
    abandoned = ledger.claim(
        decision,
        ListingPublicationState.PUBLISHED,
        publication_scope=remote.publication_scope,
    )
    assert abandoned.disposition is PublicationActionDisposition.CLAIMED
    controller = PublicationController(
        InMemoryPublicationStore(),
        remote,
        ledger=ledger,
        model_lease=PostgresPublicationModelLease(lambda: _LeaseConnection()),
    )

    report = controller.reconcile(decision)

    assert report.action_disposition == "claimed"
    assert report.remote_write_performed is False
    assert report.record.observed_state is PublicationState.DEGRADED
    assert [call[0] for call in remote.calls] == ["read", "probe", "read"]


class _ActionCursor:
    def __init__(self, database):
        self.database = database
        self.result = None
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=None):
        sql = " ".join(query.split())
        self.database.queries.append((sql, params))
        self.result = None
        self.rowcount = 0
        if sql.startswith("INSERT INTO a6_trade.trade_control_actions"):
            if self.database.action is None:
                self.database.action = {
                    "id": 41,
                    "status": "started",
                    "desired_state": json.loads(params[3]),
                }
                self.result = dict(self.database.action)
                self.rowcount = 1
        elif sql.startswith("SELECT id, status, desired_state"):
            self.result = dict(self.database.action)
            self.result["claimable"] = bool(params[0]) or self.result["status"] not in {
                "started",
                "applying",
            }
        elif sql.startswith("UPDATE a6_trade.trade_control_actions SET status"):
            self.database.action["status"] = "started"
            self.rowcount = 1
        elif sql.startswith("SELECT coalesce(max(attempt_no)"):
            self.result = {"attempt_no": len(self.database.attempts) + 1}
        elif sql.startswith("INSERT INTO a6_trade.trade_control_action_attempts"):
            self.database.attempts.append(
                {"action_id": params[0], "attempt_no": params[1], "status": "started"}
            )
            self.rowcount = 1
        elif sql.startswith("UPDATE a6_trade.trade_control_actions SET observed_after"):
            if self.database.action["status"] == "started":
                self.database.action["status"] = params[1]
                self.rowcount = 1
        elif sql.startswith("UPDATE a6_trade.trade_control_action_attempts SET observed_after"):
            for attempt in self.database.attempts:
                if (
                    attempt["action_id"] == params[5]
                    and attempt["attempt_no"] == params[6]
                    and attempt["status"] == "started"
                ):
                    attempt["status"] = params[1]
                    self.rowcount = 1
                    break

    def fetchone(self):
        return self.result


class _ActionConnection:
    def __init__(self, database):
        self.database = database

    def cursor(self):
        return _ActionCursor(self.database)

    def commit(self):
        self.database.commits += 1

    def rollback(self):
        self.database.rollbacks += 1

    def close(self):
        self.database.closes += 1


class _ActionDatabase:
    def __init__(self):
        self.action = None
        self.attempts = []
        self.queries = []
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0

    def connect(self):
        return _ActionConnection(self)


def test_postgres_publication_action_ledger_completes_and_reopens_remote_drift() -> None:
    database = _ActionDatabase()
    ledger = PostgresPublicationActionLedger(
        database.connect,
        config_version="publication-v1",
    )
    decision = _decision()

    first = ledger.claim(decision, ListingPublicationState.PUBLISHED)
    ledger.complete(first, observed_after={"listing_state": "published"}, elapsed_ms=7)
    replay = ledger.claim(decision, ListingPublicationState.PUBLISHED)
    repair = ledger.claim(
        decision,
        ListingPublicationState.PUBLISHED,
        reopen_completed=True,
    )

    assert first.disposition is PublicationActionDisposition.CLAIMED
    assert replay.disposition is PublicationActionDisposition.COMPLETED
    assert repair.disposition is PublicationActionDisposition.CLAIMED
    assert repair.attempt_no == 2
    assert len(database.attempts) == 2
    assert database.rollbacks == 0
    rendered = "\n".join(query for query, _ in database.queries)
    assert "publish_model" not in rendered  # action type is a bound value, not SQL text
    assert any(params and "publish_model" in params for _, params in database.queries)


def test_serialized_publication_action_reclaims_started_attempt_after_process_restart() -> None:
    database = _ActionDatabase()
    ledger = PostgresPublicationActionLedger(
        database.connect,
        config_version="publication-v1",
        stale_claim_seconds=300,
    )
    decision = _decision()

    abandoned = ledger.claim(decision, ListingPublicationState.PUBLISHED)
    recovered = ledger.claim(
        decision,
        ListingPublicationState.PUBLISHED,
        recover_in_progress=True,
    )

    assert abandoned.disposition is PublicationActionDisposition.CLAIMED
    assert recovered.disposition is PublicationActionDisposition.CLAIMED
    assert recovered.attempt_no == 2
    assert len(database.attempts) == 2


def test_cost_guard_unpublishes_everything_when_no_source_is_schedulable() -> None:
    plan = plan_seller_cost_guard(
        "gpt-5.6-sol",
        protected_provider_cost="0.01",
        cost_generation=12,
        cost_generation_active=True,
        listings=(
            _cost_evidence(1, price_multiplier="0.12"),
            _cost_evidence(2, price_multiplier="0.12"),
        ),
        schedulable_sources=False,
    )

    assert plan.publishable_listing_count == 0
    assert plan.any_publishable is False
    assert all(target.reason_code == "no_callable_source" for target in plan.targets)


def test_cost_guard_relists_when_sources_become_schedulable_again() -> None:
    low = _cost_evidence(1, price_multiplier="0.06")
    high = _cost_evidence(2, price_multiplier="0.12")
    plan = plan_seller_cost_guard(
        "gpt-5.6-sol",
        protected_provider_cost="0.06",
        cost_generation=12,
        cost_generation_active=True,
        listings=(low, high),
        schedulable_sources=True,
    )

    assert [target.desired_state for target in plan.targets] == [
        ListingPublicationState.UNPUBLISHED,
        ListingPublicationState.PUBLISHED,
    ]
    assert plan.schedulable_sources is True

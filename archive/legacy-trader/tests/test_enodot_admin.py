from __future__ import annotations

import hashlib
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from a6api_auto.enodot_admin import EnodotAdminClient, EnodotAdminError
from a6api_auto.storage import read_private_json, write_private_json


class Response:
    def __init__(self, status_code: int, document) -> None:
        self.status_code = status_code
        self.document = document

    def json(self):
        return self.document


class PlainTextResponse(Response):
    def json(self):
        raise ValueError("not json")


class Session:
    def __init__(self, responses) -> None:
        self.responses = iter(responses)
        self.calls: list[dict] = []

    def request(self, **kwargs):
        self.calls.append(kwargs)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def success(data) -> Response:
    return Response(200, {"code": 0, "message": "success", "data": data})


def page(items, *, page_number=1, pages=1, total=None) -> dict:
    return {
        "items": items,
        "page": page_number,
        "page_size": 2,
        "pages": pages,
        "total": len(items) if total is None else total,
    }


def account(
    account_id: int = 41,
    *,
    binding_uid: str = "a6:merchant-1:token-1",
    priority: int = 1,
    schedulable: bool = False,
    fingerprint: str = "",
) -> dict:
    extra = {
        "managed_by": "a6-trader",
        "a6_provider": True,
        "a6_binding_uid": binding_uid,
    }
    if fingerprint:
        extra["a6_api_key_fingerprint"] = fingerprint
    return {
        "id": account_id,
        "name": "a6-provider-merchant-1-token-1",
        "platform": "openai",
        "type": "apikey",
        "status": "active",
        "schedulable": schedulable,
        "credentials": {
            "api_key": "sk-upstream-secret",
            "base_url": "https://a6.a6api.com/v1",
            "model_mapping": {},
            "pool_mode": True,
            "pool_mode_retry_count": 0,
            "pool_mode_retry_status_codes": [],
        },
        "extra": extra,
        "proxy_id": 9,
        "concurrency": 1,
        "priority": priority,
        "rate_multiplier": 1.0,
        "group_ids": [],
    }


def provider_kwargs(api_key: str = "sk-a6-provider-secret") -> dict:
    return {
        "binding_uid": "a6:merchant-1:token-1",
        "name": "a6-provider-merchant-1-token-1",
        "api_key": api_key,
        "proxy_id": 9,
        "concurrency": 1,
        "priority": 1,
        "schedulable": False,
    }


def managed_seller(
    *,
    delta: float = 0.25,
    api_key: str | None = None,
    initial_working_credit_usd=999_999_999_999,
) -> dict:
    result = {
        "managed_uid": "a6trader:seller:merchant-5",
        "user_id": 51,
        "api_key_id": 61,
        "group_id": 26,
        "delta": delta,
        "initial_working_credit_usd": initial_working_credit_usd,
        "status": "active",
    }
    if api_key is not None:
        result["api_key"] = api_key
    return result


def managed_seller_capability(**changes) -> dict:
    result = {
        "capability": "a6_trader_managed_sellers",
        "contract_version": "1.0",
        "create_method": "PUT",
        "create_secret_delivery": "first_response_only",
        "replay_returns_api_key": False,
        "read_returns_api_key": False,
        "delta_min": 0,
        "delta_max": 0.5,
        "rate_multiplier_base": 1,
    }
    result.update(changes)
    return result


def test_constructor_is_read_free_and_list_accounts_redacts_credentials() -> None:
    admin_key = "admin-fixture-secret"
    session = Session((success(page([account()])),))
    client = EnodotAdminClient(
        "https://ai.enodot.shop",
        admin_key,
        session=session,
    )

    assert session.calls == []
    assert admin_key not in repr(client)

    result = client.list_accounts(page=1, page_size=2, filters={"status": "active"})

    assert result["items"][0]["credentials"]["api_key"] == "[REDACTED]"
    assert session.calls[0]["method"] == "GET"
    assert session.calls[0]["url"].endswith("/api/v1/admin/accounts")
    assert session.calls[0]["headers"]["x-api-key"] == admin_key
    assert session.calls[0]["params"] == {
        "status": "active",
        "page": 1,
        "page_size": 2,
    }


def test_request_timeout_override_only_narrows_current_context() -> None:
    session = Session(
        (
            success(account(schedulable=True)),
            success(account(schedulable=True)),
        )
    )
    client = EnodotAdminClient(
        "https://ai.enodot.shop",
        "admin-key",
        session=session,
        timeout=8,
    )

    with client.request_timeout(0.75):
        client.get_account(41)
    client.get_account(41)

    assert 0 < session.calls[0]["timeout"] <= 0.75
    assert session.calls[1]["timeout"] == 8
    assert client.timeout == 8


@pytest.mark.parametrize("timeout", (0, -1, True, float("inf"), float("nan")))
def test_request_timeout_override_rejects_invalid_budget(timeout) -> None:
    client = EnodotAdminClient(
        "https://ai.enodot.shop",
        "admin-key",
        session=Session(()),
        timeout=8,
    )

    with (
        pytest.raises(EnodotAdminError, match="enodot_admin_timeout_invalid"),
        client.request_timeout(timeout),
    ):
        pass


def test_managed_seller_create_uses_encoded_uid_idempotency_and_one_time_secret() -> None:
    secret = "sk-managed-seller-one-time-secret"
    session = Session((success(managed_seller(delta=0.5, api_key=secret)),))
    client = EnodotAdminClient("https://ai.enodot.shop", "admin-key", session=session)

    result = client.create_managed_seller(
        managed_uid="a6trader:seller:merchant-5",
        group_id=26,
        delta="0.500000",
        idempotency_key="a6trader-seller-idempotency-5",
    )

    assert result["api_key"] == secret
    assert result["delta"] == 0.5
    assert result["initial_working_credit_usd"] == 999_999_999_999.0
    call = session.calls[0]
    assert call["method"] == "PUT"
    assert call["url"].endswith("/api/v1/admin/managed-sellers/a6trader%3Aseller%3Amerchant-5")
    assert call["headers"]["Idempotency-Key"] == "a6trader-seller-idempotency-5"
    assert call["json"] == {"group_id": 26, "delta": 0.5}


def test_managed_seller_capability_uses_fixed_route_and_requires_exact_contract() -> None:
    session = Session((success(managed_seller_capability()),))
    client = EnodotAdminClient("https://ai.enodot.shop", "admin-key", session=session)

    assert client.get_managed_seller_capability()["contract_version"] == "1.0"
    assert session.calls[0]["method"] == "GET"
    assert session.calls[0]["url"].endswith("/api/v1/admin/capabilities/a6-trader-managed-sellers")

    mismatch = EnodotAdminClient(
        "https://ai.enodot.shop",
        "admin-key",
        session=Session((success(managed_seller_capability(contract_version="2.0")),)),
    )
    with pytest.raises(EnodotAdminError, match="enodot_managed_seller_capability_invalid"):
        mismatch.get_managed_seller_capability()


def test_managed_seller_get_missing_and_delta_zero_update_contract() -> None:
    session = Session(
        (
            Response(404, {"code": 404, "message": "not found"}),
            success(managed_seller(delta=0.0)),
        )
    )
    client = EnodotAdminClient("https://ai.enodot.shop", "admin-key", session=session)

    assert client.get_managed_seller("a6trader:seller:merchant-5") is None
    result = client.set_managed_seller_delta(
        "a6trader:seller:merchant-5",
        0,
        idempotency_key="a6trader-seller-delta-5",
    )

    assert result["delta"] == 0.0
    call = session.calls[1]
    assert call["method"] == "PATCH"
    assert call["url"].endswith(
        "/api/v1/admin/managed-sellers/a6trader%3Aseller%3Amerchant-5/api-key/delta"
    )
    assert call["headers"]["Idempotency-Key"] == "a6trader-seller-delta-5"
    assert call["json"] == {"delta": 0.0}


def test_managed_seller_get_does_not_treat_plain_text_route_404_as_missing() -> None:
    client = EnodotAdminClient(
        "https://ai.enodot.shop",
        "admin-key",
        session=Session((PlainTextResponse(404, "404 page not found"),)),
    )

    with pytest.raises(EnodotAdminError, match="enodot_admin_response_invalid") as raised:
        client.get_managed_seller("a6trader:seller:merchant-5")

    assert raised.value.status == 404


def test_user_balance_read_and_add_use_live_admin_contract() -> None:
    session = Session(
        (
            success({"id": 17, "balance": -5.37927575, "status": "active"}),
            success({"id": 17, "balance": 1, "status": "active"}),
        )
    )
    client = EnodotAdminClient("https://ai.enodot.shop", "admin-key", session=session)

    before = client.get_user(17)
    after = client.add_user_balance(
        17,
        Decimal("6.37927575"),
        idempotency_key="a6trader-balance:17:fixture",
        notes="fixture recovery",
    )

    assert before["balance"] == -5.37927575
    assert after["balance"] == 1.0
    assert session.calls[0]["method"] == "GET"
    assert session.calls[0]["url"].endswith("/api/v1/admin/users/17")
    call = session.calls[1]
    assert call["method"] == "POST"
    assert call["url"].endswith("/api/v1/admin/users/17/balance")
    assert call["headers"]["Idempotency-Key"] == "a6trader-balance:17:fixture"
    assert call["json"] == {
        "balance": 6.37927575,
        "operation": "add",
        "notes": "fixture recovery",
    }


def test_user_balance_add_confirms_receipt_with_same_user_readback() -> None:
    session = Session(
        (
            success({"operation_id": "balance-op-1", "applied": True}),
            success({"id": 17, "balance": 1, "status": "active"}),
        )
    )
    client = EnodotAdminClient("https://ai.enodot.shop", "admin-key", session=session)

    after = client.add_user_balance(
        17,
        Decimal("1.94874300"),
        idempotency_key="channel-growth-balance:8",
        notes="fixture recovery",
    )

    assert after["id"] == 17
    assert after["balance"] == 1.0
    assert [call["method"] for call in session.calls] == ["POST", "GET"]
    assert session.calls[1]["url"].endswith("/api/v1/admin/users/17")


@pytest.mark.parametrize(
    "amount",
    (0, -1, "0.000000001", float("nan"), True),
)
def test_user_balance_add_rejects_invalid_amount_without_network(amount) -> None:
    session = Session(())
    client = EnodotAdminClient("https://ai.enodot.shop", "admin-key", session=session)

    with pytest.raises(EnodotAdminError, match="enodot_user_balance_amount_invalid"):
        client.add_user_balance(
            17,
            amount,
            idempotency_key="a6trader-balance:17:fixture",
        )

    assert session.calls == []


def test_user_balance_response_requires_matching_identity_and_decimal_precision() -> None:
    wrong_id = EnodotAdminClient(
        "https://ai.enodot.shop",
        "admin-key",
        session=Session((success({"id": 18, "balance": 1}),)),
    )
    with pytest.raises(EnodotAdminError, match="enodot_user_response_invalid"):
        wrong_id.get_user(17)

    excessive_precision = EnodotAdminClient(
        "https://ai.enodot.shop",
        "admin-key",
        session=Session((success({"id": 17, "balance": "1.000000001"}),)),
    )
    with pytest.raises(EnodotAdminError, match="enodot_user_response_invalid"):
        excessive_precision.get_user(17)


@pytest.mark.parametrize("delta", (-0.000001, 0.500001, float("nan"), True))
def test_managed_seller_rejects_invalid_delta_without_network(delta) -> None:
    session = Session(())
    client = EnodotAdminClient("https://ai.enodot.shop", "admin-key", session=session)

    with pytest.raises(EnodotAdminError, match="enodot_managed_seller_delta_invalid"):
        client.create_managed_seller(
            managed_uid="a6trader:seller:merchant-5",
            group_id=26,
            delta=delta,
            idempotency_key="a6trader-seller-idempotency-5",
        )
    assert session.calls == []


@pytest.mark.parametrize("method", ("get", "patch"))
def test_managed_seller_read_paths_reject_returned_secret(method: str) -> None:
    secret = "sk-secret-that-must-not-appear-on-read"
    session = Session((success(managed_seller(api_key=secret)),))
    client = EnodotAdminClient("https://ai.enodot.shop", "admin-key", session=session)

    with pytest.raises(EnodotAdminError, match="enodot_managed_seller_response_invalid") as raised:
        if method == "get":
            client.get_managed_seller("a6trader:seller:merchant-5")
        else:
            client.set_managed_seller_delta(
                "a6trader:seller:merchant-5",
                0.25,
                idempotency_key="a6trader-seller-delta-5",
            )
    assert secret not in repr(raised.value)


def test_managed_seller_response_identity_mismatch_is_rejected() -> None:
    wrong = managed_seller()
    wrong["managed_uid"] = "a6trader:seller:merchant-19"
    session = Session((success(wrong),))
    client = EnodotAdminClient("https://ai.enodot.shop", "admin-key", session=session)

    with pytest.raises(EnodotAdminError, match="enodot_managed_seller_response_invalid"):
        client.get_managed_seller("a6trader:seller:merchant-5")


@pytest.mark.parametrize(
    "amount",
    (None, 0, 1, "999999999998.99999999", True, float("nan"), float("inf")),
)
def test_managed_seller_response_requires_fixed_initial_working_credit(amount) -> None:
    document = managed_seller(initial_working_credit_usd=amount)
    if amount is None:
        document.pop("initial_working_credit_usd")
    client = EnodotAdminClient(
        "https://ai.enodot.shop",
        "admin-key",
        session=Session((success(document),)),
    )

    with pytest.raises(EnodotAdminError, match="enodot_managed_seller_response_invalid"):
        client.get_managed_seller("a6trader:seller:merchant-5")


@pytest.mark.parametrize(
    "amount",
    (999_999_999_999, 999_999_999_999.0, "999999999999.00000000"),
)
def test_managed_seller_response_accepts_exact_initial_working_credit(amount) -> None:
    client = EnodotAdminClient(
        "https://ai.enodot.shop",
        "admin-key",
        session=Session((success(managed_seller(initial_working_credit_usd=amount)),)),
    )

    result = client.get_managed_seller("a6trader:seller:merchant-5")

    assert result is not None
    assert result["initial_working_credit_usd"] == 999_999_999_999.0


def test_find_account_by_binding_uid_walks_all_pages() -> None:
    session = Session(
        (
            success(page([account(10, binding_uid="a6:other:1")], pages=2, total=2)),
            success(page([account(11)], page_number=2, pages=2, total=2)),
        )
    )
    client = EnodotAdminClient("https://ai.enodot.shop", "admin-key", session=session)

    found = client.find_account_by_binding_uid("a6:merchant-1:token-1", page_size=2)

    assert found is not None
    assert found["id"] == 11
    assert len(session.calls) == 2
    assert session.calls[0]["params"]["platform"] == "openai"
    assert session.calls[0]["params"]["type"] == "apikey"


def test_get_account_uses_id_route_and_redacts_nested_secret() -> None:
    session = Session((success(account(77)),))
    client = EnodotAdminClient("https://ai.enodot.shop", "admin-key", session=session)

    found = client.get_account(77)

    assert found["id"] == 77
    assert found["credentials"]["api_key"] == "[REDACTED]"
    assert session.calls[0]["url"].endswith("/api/v1/admin/accounts/77")


def test_get_account_usage_uses_active_read_without_forcing_probe() -> None:
    payload = {
        "updated_at": "2026-08-04T11:21:36+08:00",
        "seven_day": {
            "utilization": 3,
            "resets_at": "2026-08-11T11:15:04+08:00",
            "remaining_seconds": 604407,
            "window_stats": {"requests": 71, "tokens": 3544593, "cost": 14.1771335},
        },
    }
    session = Session((success(payload),))
    client = EnodotAdminClient("https://ai.enodot.shop", "admin-key", session=session)

    result = client.get_account_usage(45920)

    assert result == payload
    assert session.calls[0]["url"].endswith("/api/v1/admin/accounts/45920/usage")
    assert session.calls[0]["params"] == {"source": "active"}


def test_get_account_usage_force_is_explicit_and_response_must_be_object() -> None:
    session = Session((success({"seven_day": None}), success([])))
    client = EnodotAdminClient("https://ai.enodot.shop", "admin-key", session=session)

    assert client.get_account_usage(45920, force=True) == {"seven_day": None}
    assert session.calls[0]["params"] == {"source": "active", "force": "true"}
    with pytest.raises(EnodotAdminError, match="enodot_account_usage_response_invalid"):
        client.get_account_usage(45920)


def test_duplicate_binding_is_rejected_without_exposing_records() -> None:
    secret = "sk-secret-in-duplicate"
    first = account(10)
    second = account(11)
    first["credentials"]["api_key"] = secret
    second["credentials"]["api_key"] = secret
    client = EnodotAdminClient(
        "https://ai.enodot.shop",
        "admin-key",
        session=Session((success(page([first, second])),)),
    )

    with pytest.raises(EnodotAdminError) as raised:
        client.find_account_by_binding_uid("a6:merchant-1:token-1")

    assert raised.value.code == "enodot_account_binding_not_unique"
    assert secret not in str(raised.value)
    assert secret not in repr(raised.value)


def test_create_a6_provider_sends_secret_only_in_request_body() -> None:
    api_key = "sk-a6-provider-secret"
    created = account()
    created["credentials"]["api_key"] = api_key
    session = Session((success(created),))
    client = EnodotAdminClient("https://ai.enodot.shop", "admin-key", session=session)

    result = client.create_a6_provider_account(**provider_kwargs(api_key))

    payload = session.calls[0]["json"]
    assert payload["platform"] == "openai"
    assert payload["type"] == "apikey"
    assert payload["schedulable"] is False
    assert payload["credentials"] == {
        "api_key": api_key,
        "base_url": "https://a6.a6api.com/v1",
        "model_mapping": {},
        "pool_mode": True,
        "pool_mode_retry_count": 0,
        "pool_mode_retry_status_codes": [],
    }
    assert payload["extra"]["a6_binding_uid"] == "a6:merchant-1:token-1"
    assert (
        payload["extra"]["a6_api_key_fingerprint"] == hashlib.sha256(api_key.encode()).hexdigest()
    )
    assert result["credentials"]["api_key"] == "[REDACTED]"
    assert api_key not in repr(result)


def test_reconcile_is_dry_run_by_default_and_updates_only_differences() -> None:
    api_key = "sk-a6-provider-secret"
    fingerprint = hashlib.sha256(api_key.encode()).hexdigest()
    current = account(priority=2, fingerprint=fingerprint)
    dry_session = Session((success(page([current])),))
    dry_client = EnodotAdminClient(
        "https://ai.enodot.shop",
        "admin-key",
        session=dry_session,
    )

    planned = dry_client.reconcile_a6_provider_account(**provider_kwargs(api_key))

    assert planned.action == "update"
    assert planned.applied is False
    assert planned.account_id == 41
    assert planned.changed_fields == ("priority",)
    assert len(dry_session.calls) == 1
    assert api_key not in repr(planned)

    updated = {**current, "priority": 1}
    apply_session = Session((success(page([current])), success(updated)))
    apply_client = EnodotAdminClient(
        "https://ai.enodot.shop",
        "admin-key",
        session=apply_session,
    )

    applied = apply_client.reconcile_a6_provider_account(
        apply=True,
        **provider_kwargs(api_key),
    )

    assert applied.action == "update"
    assert applied.applied is True
    assert applied.changed_fields == ("priority",)
    assert apply_session.calls[1]["method"] == "PUT"
    assert apply_session.calls[1]["json"] == {"priority": 1}


def test_reconcile_uses_zero_to_clear_existing_proxy() -> None:
    api_key = "sk-a6-provider-secret"
    fingerprint = hashlib.sha256(api_key.encode()).hexdigest()
    current = account(fingerprint=fingerprint)
    updated = {**current, "proxy_id": None}
    session = Session((success(page([current])), success(updated)))
    client = EnodotAdminClient("https://ai.enodot.shop", "admin-key", session=session)
    kwargs = {**provider_kwargs(api_key), "proxy_id": None}

    result = client.reconcile_a6_provider_account(apply=True, **kwargs)

    assert result.action == "update"
    assert result.changed_fields == ("proxy_id",)
    assert session.calls[1]["json"] == {"proxy_id": 0}


def test_reconcile_create_requires_explicit_apply() -> None:
    api_key = "sk-a6-provider-secret"
    dry_session = Session((success(page([])),))
    client = EnodotAdminClient(
        "https://ai.enodot.shop",
        "admin-key",
        session=dry_session,
    )

    planned = client.reconcile_a6_provider_account(**provider_kwargs(api_key))

    assert planned.action == "create"
    assert planned.applied is False
    assert len(dry_session.calls) == 1

    created = account()
    create_session = Session((success(page([])), success(created)))
    create_client = EnodotAdminClient(
        "https://ai.enodot.shop",
        "admin-key",
        session=create_session,
    )

    applied = create_client.reconcile_a6_provider_account(
        apply=True,
        **provider_kwargs(api_key),
    )

    assert applied.action == "create"
    assert applied.applied is True
    assert applied.account_id == 41
    assert create_session.calls[1]["method"] == "POST"


def test_reconcile_matching_account_is_noop_and_non_boolean_apply_is_rejected() -> None:
    api_key = "sk-a6-provider-secret"
    fingerprint = hashlib.sha256(api_key.encode()).hexdigest()
    session = Session((success(page([account(fingerprint=fingerprint)])),))
    client = EnodotAdminClient("https://ai.enodot.shop", "admin-key", session=session)

    result = client.reconcile_a6_provider_account(**provider_kwargs(api_key))

    assert result.action == "noop"
    assert result.changed_fields == ()
    assert len(session.calls) == 1
    with pytest.raises(EnodotAdminError, match="enodot_reconcile_apply_invalid"):
        client.reconcile_a6_provider_account(apply="yes", **provider_kwargs(api_key))


def test_usage_pagination_and_summary() -> None:
    usage_one = {"id": 1, "api_key": "customer-secret", "total_cost": 12.5}
    usage_two = {"id": 2, "api_key": "customer-secret-2", "total_cost": 3.5}
    session = Session(
        (
            success(page([usage_one], pages=2, total=2)),
            success(page([usage_two], page_number=2, pages=2, total=2)),
            success({"total_requests": 2, "total_cost": 16.0}),
        )
    )
    client = EnodotAdminClient("https://ai.enodot.shop", "admin-key", session=session)

    rows = list(client.iter_usage(page_size=2, filters={"account_id": 41}))
    summary = client.get_usage_summary({"account_id": 41})

    assert [row["id"] for row in rows] == [1, 2]
    assert all(row["api_key"] == "[REDACTED]" for row in rows)
    assert summary == {"total_requests": 2, "total_cost": 16.0}
    assert session.calls[2]["url"].endswith("/api/v1/admin/usage/stats")


def test_short_cooldown_and_scheduling_updates_are_explicit() -> None:
    disabled = account(schedulable=False)
    enabled = account(schedulable=True)
    adjusted = {**account(), "priority": 2, "concurrency": 4}
    session = Session((success(disabled), success(enabled), success(adjusted)))
    client = EnodotAdminClient("https://ai.enodot.shop", "admin-key", session=session)
    started_at = datetime(2026, 7, 24, 13, 0, tzinfo=UTC)

    cooldown = client.start_short_cooldown(41, seconds=90, now=started_at)
    restored = client.end_short_cooldown(41)
    result = client.update_scheduling(41, priority=2, concurrency=4)

    assert cooldown.resume_at == datetime(2026, 7, 24, 13, 1, 30, tzinfo=UTC)
    assert session.calls[0]["json"] == {"schedulable": False}
    assert session.calls[1]["json"] == {"schedulable": True}
    assert session.calls[2]["method"] == "PUT"
    assert session.calls[2]["json"] == {"priority": 2, "concurrency": 4}
    assert restored["schedulable"] is True
    assert result["concurrency"] == 4


def test_update_scheduling_accepts_pool_priority_twelve() -> None:
    session = Session((success({**account(), "priority": 12}),))
    client = EnodotAdminClient("https://ai.enodot.shop", "admin-key", session=session)

    result = client.update_scheduling(41, priority=12)

    assert result["priority"] == 12
    assert session.calls[0]["json"] == {"priority": 12}


def test_group_rate_multiplier_get_merge_put_readback_preserves_other_users() -> None:
    before = [
        {
            "user_id": 8,
            "user_name": "existing",
            "rate_multiplier": 1.1,
            "visible_to_user": True,
        },
        {
            "user_id": 9,
            "user_name": "target",
            "rate_multiplier": 1.2,
            "visible_to_user": False,
        },
    ]
    after = [
        {"user_id": 8, "rate_multiplier": 1.1, "visible_to_user": True},
        {"user_id": 9, "rate_multiplier": 1.35, "visible_to_user": False},
    ]
    session = Session((success(before), success({"message": "updated"}), success(after)))
    client = EnodotAdminClient("https://ai.enodot.shop", "admin-key", session=session)
    locks: list[int] = []

    @contextmanager
    def lock(group_id: int):
        locks.append(group_id)
        yield

    observed = client.merge_group_rate_multiplier(
        26,
        9,
        1.35,
        lock=lock,
    )

    assert locks == [26]
    assert observed == after
    assert [call["method"] for call in session.calls] == ["GET", "PUT", "GET"]
    assert session.calls[1]["json"] == {
        "entries": [
            {"user_id": 8, "rate_multiplier": 1.1, "visible_to_user": True},
            {"user_id": 9, "rate_multiplier": 1.35, "visible_to_user": False},
        ]
    }


def test_group_rate_multiplier_requires_lock_and_checks_readback() -> None:
    client = EnodotAdminClient(
        "https://ai.enodot.shop",
        "admin-key",
        session=Session(()),
    )
    with pytest.raises(EnodotAdminError, match="enodot_rate_multiplier_lock_required"):
        client.merge_group_rate_multiplier(26, 9, 1.3, lock=None)

    @contextmanager
    def lock(_group_id: int):
        yield

    mismatch_session = Session(
        (
            success([]),
            success({"message": "updated"}),
            success([{"user_id": 9, "rate_multiplier": 1.2, "visible_to_user": False}]),
        )
    )
    mismatch = EnodotAdminClient(
        "https://ai.enodot.shop",
        "admin-key",
        session=mismatch_session,
    )
    with pytest.raises(EnodotAdminError, match="enodot_rate_multiplier_readback_mismatch"):
        mismatch.merge_group_rate_multiplier(26, 9, 1.3, lock=lock)


def test_group_rate_multiplier_ignores_rpm_only_rows_and_postgres_lock_is_scoped() -> None:
    session = Session(
        (
            success(
                [
                    {"user_id": 7, "rpm_override": 10},
                    {"user_id": 8, "rate_multiplier": 1.1, "visible_to_user": True},
                ]
            ),
        )
    )
    client = EnodotAdminClient("https://ai.enodot.shop", "admin-key", session=session)
    assert client.get_group_rate_multipliers(26) == [
        {"user_id": 8, "rate_multiplier": 1.1, "visible_to_user": True}
    ]

    class Cursor:
        def __init__(self, connection):
            self.connection = connection

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params):
            self.connection.calls.append((query, params))

    class Connection:
        def __init__(self):
            self.calls = []
            self.closed = False

        def cursor(self):
            return Cursor(self)

        def close(self):
            self.closed = True

    connection = Connection()
    lock = client.postgres_group_rate_lock(lambda: connection)
    with lock(26):
        assert connection.calls[-1][0] == "SELECT pg_advisory_lock(%s, %s)"
    assert [call[0] for call in connection.calls] == [
        "SELECT pg_advisory_lock(%s, %s)",
        "SELECT pg_advisory_unlock(%s, %s)",
    ]
    assert connection.calls[0][1] == connection.calls[1][1] == (0x41365452, 26)
    assert connection.closed is True


def test_token_file_auth_refreshes_once_and_persists_rotated_tokens(tmp_path) -> None:
    token_file = tmp_path / "admin-tokens.json"
    write_private_json(
        token_file,
        {
            "schema_version": 1,
            "access_token": "old-access-secret",
            "refresh_token": "old-refresh-secret",
        },
    )
    session = Session(
        (
            Response(401, {"code": 401, "message": "expired"}),
            success(
                {
                    "access_token": "new-access-secret",
                    "refresh_token": "new-refresh-secret",
                    "expires_at": 1234567890,
                }
            ),
            success(page([account()])),
        )
    )
    client = EnodotAdminClient(
        "https://ai.enodot.shop",
        token_file=token_file,
        session=session,
    )

    result = client.list_accounts()

    assert result["total"] == 1
    assert session.calls[0]["headers"]["Authorization"] == "Bearer old-access-secret"
    assert session.calls[1]["url"].endswith("/api/v1/auth/refresh")
    assert session.calls[2]["headers"]["Authorization"] == "Bearer new-access-secret"
    stored = read_private_json(token_file)
    assert stored["access_token"] == "new-access-secret"
    assert stored["refresh_token"] == "new-refresh-secret"


def test_rejected_response_and_transport_errors_do_not_retain_secrets() -> None:
    secret = "admin-fixture-secret"
    response_secret = "sk-secret-from-provider-response"
    rejected = Session(
        (
            Response(
                403,
                {"code": 403, "message": f"rejected {secret} {response_secret}"},
            ),
        )
    )
    client = EnodotAdminClient("https://ai.enodot.shop", secret, session=rejected)

    with pytest.raises(EnodotAdminError) as raised:
        client.list_accounts()

    rendered = f"{raised.value!s} {raised.value!r}"
    assert raised.value.status == 403
    assert secret not in rendered
    assert response_secret not in rendered

    transport = Session((RuntimeError(f"transport leaked {secret}"),))
    transport_client = EnodotAdminClient(
        "https://ai.enodot.shop",
        secret,
        session=transport,
    )
    with pytest.raises(EnodotAdminError) as transport_error:
        transport_client.list_accounts()
    assert secret not in repr(transport_error.value)
    assert transport_error.value.__cause__ is None


def test_provider_quota_and_channel_errors_use_shared_structured_classifier() -> None:
    quota = EnodotAdminClient.classify_upstream_error(
        403,
        {"error": {"message": "账户额度不足，请充值或调整请求后重试。"}},
    )
    no_channel = EnodotAdminClient.classify_upstream_error(
        503,
        {"error": {"code": "no_available_channel", "message": "internal detail"}},
    )

    assert quota.error_class == "account_quota_insufficient"
    assert quota.short_circuit is True
    assert quota.suppress_upstream_detail is True
    assert no_channel.error_class == "no_available_channel"
    assert no_channel.retry_next_provider is True
    assert no_channel.cooldown_seconds == 60


def test_reconcile_account_profit_gate_dry_run_and_apply_with_readback() -> None:
    current = account()
    dry_session = Session((success(current),))
    dry_client = EnodotAdminClient(
        "https://ai.enodot.shop",
        "admin-key",
        session=dry_session,
    )

    planned = dry_client.reconcile_account_profit_gate(
        41,
        profit_required=0.92,
        oauth_auto_cost=True,
    )

    assert planned.action == "update"
    assert planned.applied is False
    assert planned.account_id == 41
    assert planned.changed_fields == ("profit_required", "oauth_auto_cost")
    assert len(dry_session.calls) == 1

    updated = {**current, "extra": {**current["extra"], "profit_required": 0.92, "oauth_auto_cost": True}}
    apply_session = Session((success(current), success(updated), success(updated)))
    apply_client = EnodotAdminClient(
        "https://ai.enodot.shop",
        "admin-key",
        session=apply_session,
    )

    applied = apply_client.reconcile_account_profit_gate(
        41,
        profit_required=0.92,
        oauth_auto_cost=True,
        apply=True,
    )

    assert applied.action == "update"
    assert applied.applied is True
    assert applied.changed_fields == ("profit_required", "oauth_auto_cost")
    assert apply_session.calls[1]["method"] == "PUT"
    assert apply_session.calls[1]["json"] == {
        "extra": {**current["extra"], "profit_required": 0.92, "oauth_auto_cost": True}
    }
    assert apply_session.calls[2]["method"] == "GET"


def test_reconcile_account_profit_gate_noop_when_matching() -> None:
    current = account()
    current["extra"]["profit_required"] = 0.44
    current["extra"]["oauth_auto_cost"] = False
    session = Session((success(current),))
    client = EnodotAdminClient("https://ai.enodot.shop", "admin-key", session=session)

    result = client.reconcile_account_profit_gate(
        41,
        profit_required=0.44,
        oauth_auto_cost=False,
        apply=True,
    )

    assert result.action == "noop"
    assert result.applied is False
    assert len(session.calls) == 1


def test_reconcile_account_profit_gate_readback_mismatch_fails() -> None:
    current = account()
    updated = {**current, "extra": {**current["extra"], "profit_required": 0.44}}
    # 写回后 readback 未生效（oauth_auto_cost 缺失）
    drifted = {**current, "extra": {**current["extra"], "profit_required": 0.44}}
    session = Session((success(current), success(updated), success(drifted)))
    client = EnodotAdminClient("https://ai.enodot.shop", "admin-key", session=session)

    with pytest.raises(EnodotAdminError, match="enodot_account_profit_gate_readback_mismatch"):
        client.reconcile_account_profit_gate(
            41,
            profit_required=0.44,
            oauth_auto_cost=True,
            apply=True,
        )


def test_reconcile_account_profit_gate_rejects_invalid_values_without_network() -> None:
    client = EnodotAdminClient("https://ai.enodot.shop", "admin-key", session=Session(()))

    with pytest.raises(EnodotAdminError, match="enodot_profit_required_invalid"):
        client.reconcile_account_profit_gate(41, profit_required=-0.1, oauth_auto_cost=True)
    with pytest.raises(EnodotAdminError, match="enodot_profit_required_invalid"):
        client.reconcile_account_profit_gate(41, profit_required=True, oauth_auto_cost=True)
    with pytest.raises(EnodotAdminError, match="enodot_profit_required_invalid"):
        client.reconcile_account_profit_gate(41, profit_required="x", oauth_auto_cost=True)
    with pytest.raises(EnodotAdminError, match="enodot_oauth_auto_cost_invalid"):
        client.reconcile_account_profit_gate(41, profit_required=0.92, oauth_auto_cost="true")
    with pytest.raises(EnodotAdminError, match="enodot_account_id_invalid"):
        client.reconcile_account_profit_gate(0, profit_required=0.92, oauth_auto_cost=True)

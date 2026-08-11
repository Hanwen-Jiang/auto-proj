from __future__ import annotations

import hashlib

import pytest

from a6api_auto.errors import A6AutoError
from scripts.reconcile_a6_provider_pool import (
    _managed_provider_accounts,
    _provider_update,
    _verify,
)


def provider_account() -> dict:
    return {
        "id": 45896,
        "name": "a6-provider-merchant-19-token-14093",
        "credentials": {
            "api_key": "sk-private-fixture",
            "base_url": "https://a6.a6api.com/v1",
            "model_mapping": {"gpt-5.6-sol": "gpt-5.6-sol"},
        },
        "extra": {
            "managed_by": "a6-trader",
            "a6_binding_uid": "a6:merchant-19:token-14093",
            "a6_merchant_id": 19,
            "a6_user_id": 9812,
            "external_a6_token_id": 14093,
        },
        "proxy_id": 18,
    }


def test_provider_pool_update_preserves_key_and_routes_through_per_user_relay() -> None:
    update, base_url = _provider_update(
        provider_account(),
        api_key="sk-private-fixture",
        relay_origin="http://127.0.0.1:8767/",
    )

    assert base_url == "http://127.0.0.1:8767/a6-user/9812/v1"
    assert update["credentials"] == {
        "api_key": "sk-private-fixture",
        "base_url": base_url,
        "model_mapping": {"gpt-5.6-sol": "gpt-5.6-sol"},
        "pool_mode": True,
        "pool_mode_retry_count": 0,
        "pool_mode_retry_status_codes": [],
    }
    assert update["extra"] == {
        "managed_by": "a6-trader",
        "a6_binding_uid": "a6:merchant-19:token-14093",
        "a6_merchant_id": 19,
        "a6_user_id": 9812,
        "external_a6_token_id": 14093,
        "a6_provider": True,
        "a6_api_key_fingerprint": hashlib.sha256(b"sk-private-fixture").hexdigest(),
    }
    assert update["proxy_id"] == 0


def test_provider_pool_readback_contract() -> None:
    account = provider_account()
    account["credentials"].update(
        {
            "base_url": "http://127.0.0.1:8767/a6-user/9812/v1",
            "pool_mode": True,
            "pool_mode_retry_count": 0,
            "pool_mode_retry_status_codes": [],
        }
    )
    account["extra"].update(
        {
            "a6_provider": True,
            "a6_api_key_fingerprint": hashlib.sha256(b"sk-private-fixture").hexdigest(),
        }
    )
    account["proxy_id"] = None

    _verify(
        account,
        desired_base_url="http://127.0.0.1:8767/a6-user/9812/v1",
    )

    update, _ = _provider_update(
        account,
        api_key="sk-private-fixture",
        relay_origin="http://127.0.0.1:8767",
    )
    assert update == {}


def test_provider_pool_rejects_a_remote_or_noncanonical_relay() -> None:
    with pytest.raises(A6AutoError, match="provider_pool_relay_origin_invalid"):
        _provider_update(
            provider_account(),
            api_key="sk-private-fixture",
            relay_origin="http://hk_cloud:8767",
        )


def test_managed_provider_selection_excludes_synthetic_shadow_accounts() -> None:
    managed = provider_account()
    synthetic = {
        "id": 45910,
        "name": "a6-provider-hk-shadow-e2e-fixture-a",
        "extra": {"a6_provider": True},
    }

    selected = _managed_provider_accounts([synthetic, managed])

    assert selected == [managed]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda account: account["extra"].update(managed_by="other-controller"),
        lambda account: account["extra"].update(a6_merchant_id=7),
        lambda account: account["extra"].update(external_a6_token_id=12465),
        lambda account: account["extra"].update(a6_binding_uid="a6:merchant-7:token-12465"),
    ],
)
def test_managed_provider_selection_rejects_identity_drift(mutate) -> None:
    account = provider_account()
    mutate(account)

    with pytest.raises(A6AutoError, match="provider_pool_account_identity_invalid"):
        _managed_provider_accounts([account])


def test_managed_provider_selection_rejects_duplicate_identity() -> None:
    first = provider_account()
    duplicate = {**provider_account(), "id": 45996}

    with pytest.raises(A6AutoError, match="provider_pool_account_identity_invalid"):
        _managed_provider_accounts([first, duplicate])

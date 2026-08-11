from __future__ import annotations

import importlib
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
adoption = importlib.import_module("scripts.adopt_merchant1_channel_1120")
AdoptionError = adoption.AdoptionError
PilotTarget = adoption.PilotTarget
PinEvidence = adoption.PinEvidence
_fingerprint = adoption._fingerprint
_pricing = adoption._pricing
_stable_post_bundle = adoption._stable_post_bundle
_verify_post_bundle = adoption._verify_post_bundle


def source_bundle() -> dict:
    return {
        "channel": {
            "id": 1120,
            "binding_id": 1111,
            "supplier_id": 308,
            "name": "legacy",
            "nickname": "legacy",
            "channel_type": 1,
            "status": 1,
            "availability": 1,
            "models": "legacy-models",
            "description": "legacy description",
        },
        "approved_request": {
            "id": 1101,
            "native_channel_id": 1120,
            "supplier_id": 308,
            "name": "legacy",
            "nickname": "legacy",
            "channel_type": 1,
            "status": 1,
            "base_url": "https://legacy.invalid",
            "models": "legacy-models",
            "test_model": "",
            "description": "legacy description",
            "api_key_last4": "old1",
        },
        "pending_request": {
            "id": 1199,
            "native_channel_id": 0,
            "supplier_id": 308,
            "name": "managed-new",
            "status": 0,
        },
    }


def adopted_bundle(ownership: dict) -> dict:
    return {
        "channel": {**source_bundle()["channel"], **ownership["channel"]},
        "approved_request": {
            **source_bundle()["approved_request"],
            **ownership["request"],
        },
        "pending_request": {
            **source_bundle()["pending_request"],
            **ownership["request_guards"][0],
        },
    }


def test_adoption_post_readback_requires_all_three_fingerprints() -> None:
    ownership = _stable_post_bundle(
        source_bundle(),
        target=PilotTarget(),
        description="a6c-fixture",
        key_last4="new1",
    )
    bundle = adopted_bundle(ownership)

    _verify_post_bundle(bundle, ownership)
    bundle["approved_request"]["api_key_last4"] = "xxxx"
    with pytest.raises(AdoptionError, match="adoption_remote_readback_mismatch"):
        _verify_post_bundle(bundle, ownership)


def test_adoption_fingerprint_is_order_independent() -> None:
    left = {"channel": {"id": 1120, "status": 1}, "request": {"id": 1101}}
    right = {"request": {"id": 1101}, "channel": {"status": 1, "id": 1120}}
    assert _fingerprint(left) == _fingerprint(right)


def test_current_pin_cost_drives_listing_prices() -> None:
    now = datetime.now(timezone.utc)
    pin = PinEvidence(
        pin_binding_id=10,
        token_ref_id=4,
        provider_merchant_id=5,
        external_a6_token_id=14020,
        decision_id=6,
        channel_id=150,
        cost_multiplier=Decimal("0.0108"),
        confirmed_at=now,
        quote_id=420,
        quote_scan_run_id=42,
        quote_observed_at=now,
    )

    pricing = _pricing(pin)

    assert pricing.prices == (52_877, 317_262, 5_288, 66_096)
    assert pricing.landed_cost_multiplier.quantize(Decimal("0.00000001")) == Decimal(
        "0.01296000"
    )
    assert pricing.price_multiplier.quantize(Decimal("0.00000001")) == Decimal(
        "0.01057536"
    )
    assert pricing.final_multiplier.quantize(Decimal("0.00000001")) == Decimal(
        "0.01586304"
    )
    assert pricing.final_multiplier >= (
        Decimal("1.2") * pricing.landed_cost_multiplier * Decimal("1.02")
    )

"""Payhip webhooks: the (weak) signature, freshness, per-item signals, privacy. No database. Names PHW*."""
import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from adapters.lemon_squeezy.webhook import WebhookRejected
from adapters.payhip.webhook import PayhipWebhook
from shared.models import Marketplace

API_KEY = "payhip-key-0123456789"
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
SIG = hashlib.sha256(API_KEY.encode()).hexdigest()


def paid(**over):
    base = {"id": "tx-100", "type": "paid", "email": "jane.doe@example.test", "currency": "USD", "price": 1900, "ip_address": "203.0.113.9",
            "date": int((NOW - timedelta(minutes=5)).timestamp()), "signature": SIG,
            "items": [{"product_id": "p1", "product_name": "Guide", "product_key": "RGsF", "quantity": 1, "product_permalink": "https://payhip.com/b/RGsF"}]}
    base.update(over)
    return {k: v for k, v in base.items() if v is not ...}


def handle(p, **kw):
    return PayhipWebhook(API_KEY, clock=lambda: NOW, **kw).handle({}, json.dumps(p).encode() if not isinstance(p, bytes) else p)


def test_PHW1_a_signed_paid_event_becomes_one_sale_signal_per_item_without_personal_data():
    p = paid(items=[{"product_key": "RGsF"}, {"product_key": "Xy9z", "quantity": 3}])
    a, b = handle(p)
    assert (a.signal.signal_ref, a.signal.product_external_id, a.signal.kind, a.signal.marketplace) == ("ph_tx_tx-100_0", "RGsF", "SALE", Marketplace.PAYHIP)
    assert (b.signal.signal_ref, b.signal.product_external_id) == ("ph_tx_tx-100_1", "Xy9z") and a.attributed_lead is None
    assert a.signal.occurred_at == datetime.fromtimestamp(p["date"], tz=timezone.utc)
    blob = repr((a, b))
    assert "jane" not in blob and "example.test" not in blob and "203.0.113" not in blob


@pytest.mark.parametrize("sig", [..., "", "0" * 64, SIG[:-1], SIG + "0", 5, None, hashlib.sha256(b"other-key-12345").hexdigest()])
def test_PHW2_a_wrong_or_missing_signature_is_rejected_even_for_events_that_would_be_ignored(sig):
    for p in (paid(signature=sig), paid(signature=sig, type="refunded")):
        with pytest.raises(WebhookRejected) as e:
            handle(p)
        assert e.value.code == "BAD_SIGNATURE"
    assert len(handle(paid(signature=SIG.upper()))) == 1                                  # hex case does not matter


def test_PHW3_refunds_and_subscription_events_are_ignored_not_rejected():
    for t in ("refunded", "subscription.created", "subscription.deleted", "something.new"):
        assert handle(paid(type=t)) == []


def test_PHW4_an_event_older_than_the_window_or_dated_in_the_future_is_ignored_as_a_probable_replay():
    at = lambda delta: int((NOW + delta).timestamp())  # noqa: E731
    assert len(handle(paid(date=at(-timedelta(hours=24))))) == 1                           # exactly at the limit: accepted
    assert handle(paid(date=at(-timedelta(hours=24, seconds=1)))) == []
    assert len(handle(paid(date=at(timedelta(days=1))))) == 1 and handle(paid(date=at(timedelta(days=1, seconds=1)))) == []
    assert handle(paid(date=at(-timedelta(hours=2))), ) and handle(paid(date=at(-timedelta(hours=30))), ) == []
    assert len(PayhipWebhook(API_KEY, max_age=timedelta(hours=48), clock=lambda: NOW).handle({}, json.dumps(paid(date=at(-timedelta(hours=30)))).encode())) == 1


@pytest.mark.parametrize("p", [paid(id=...), paid(id="has space"), paid(id=5), paid(date=...), paid(date="1700000000"), paid(date=True), paid(items=...), paid(items="x"),
                               paid(items=[{"product_key": "bad key!"}]), paid(items=[{"no_key": 1}]), paid(items=["x"])])
def test_PHW5_a_signed_but_malformed_paid_event_is_a_fixed_code(p):
    with pytest.raises(WebhookRejected) as e:
        handle(p)
    assert e.value.code == "INVALID_PAYLOAD"


@pytest.mark.parametrize("body", [b"not json", b"[1,2]", b'"text"', b"", b"\xff\xfe"])
def test_PHW6_a_body_that_is_not_a_json_object_is_invalid(body):
    with pytest.raises(WebhookRejected) as e:
        handle(body)
    assert e.value.code == "INVALID_PAYLOAD"


def test_PHW7_the_signature_is_a_constant_so_it_proves_only_that_the_sender_knew_the_key():
    # This documents Payhip's design: the same signature validates ANY body. The receiver's secret path token is what stops a forgery.
    first, second = paid(id="tx-1"), paid(id="tx-2", items=[{"product_key": "OTHER"}])
    assert first["signature"] == second["signature"] == hashlib.sha256(API_KEY.encode()).hexdigest()
    assert handle(first) and handle(second)


def test_PHW8_the_key_must_be_usable():
    for bad in ("", "short", None, 5):
        with pytest.raises(ValueError):
            PayhipWebhook(bad)

"""Lemon Squeezy webhooks: the signature over the raw body, event handling, attribution, privacy. No database. Names LW*."""
import json
import uuid
from datetime import datetime, timezone

import pytest

from adapters.lemon_squeezy.webhook import LemonSqueezyWebhook, WebhookRejected, sign
from shared.models import Marketplace

SECRET = "whsec-0123456789"
LEAD = uuid.uuid4()


def payload(event="order_created", store=1, order="123", product=456, created="2026-11-02T10:00:00.000000Z", test_mode=False, custom=None, **extra):
    meta = {"event_name": event, "test_mode": test_mode}
    if custom is not None:
        meta["custom_data"] = custom
    data = {"type": "orders", "id": order, "attributes": {"store_id": store, "created_at": created, "status": "paid", "user_email": "jane.doe@example.test",
                                                          "user_name": "Jane Doe", "first_order_item": {"product_id": product, "product_name": "Guide"}}}
    data["attributes"].update(extra)
    return {"meta": meta, "data": data}


def request(p, secret=SECRET, event=None):
    body = json.dumps(p).encode()
    return {"x-signature": sign(secret, body), "x-event-name": event or p["meta"]["event_name"]}, body


def handler(**kw):
    return LemonSqueezyWebhook(SECRET, 1, **kw)


def test_LW1_a_signed_order_becomes_one_sale_signal_without_any_personal_data():
    (parsed,) = handler().handle(*request(payload()))
    s = parsed.signal
    assert (s.signal_ref, s.kind, s.marketplace, s.product_external_id) == ("ls_order_123", "SALE", Marketplace.LEMON_SQUEEZY, "456")
    assert s.occurred_at == datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc) and parsed.attributed_lead is None
    assert "jane" not in repr(parsed).lower() and "example.test" not in repr(parsed)


def test_LW2_the_lead_id_the_checkout_link_carried_comes_back_as_the_attribution():
    assert handler().handle(*request(payload(custom={"ref": str(LEAD)})))[0].attributed_lead == LEAD
    for custom in ({"ref": "not-a-uuid"}, {"ref": 5}, {"other": "x"}, "text", None):
        assert handler().handle(*request(payload(custom=custom)))[0].attributed_lead is None


def test_LW3_the_signature_must_match_the_raw_body_exactly():
    h = handler()
    headers, body = request(payload())
    for bad in ({}, {"x-signature": ""}, {"x-signature": "0" * 64}, {"x-signature": headers["x-signature"][:-1]}, {"x-signature": sign("other-secret-123", body)}):
        with pytest.raises(WebhookRejected) as e:
            h.handle({**headers, **bad} if bad else {"x-event-name": "order_created"}, body)
        assert e.value.code == "BAD_SIGNATURE"
    with pytest.raises(WebhookRejected):
        h.handle(headers, body.replace(b"123", b"124"))                                  # tampered after signing
    with pytest.raises(WebhookRejected):
        h.handle(headers, json.dumps(payload(), indent=2).encode())                      # same content, different bytes: a different signature
    assert h.handle({**headers, "x-signature": headers["x-signature"].upper()}, body)    # hex case does not matter


def test_LW4_events_that_are_not_new_orders_and_test_mode_orders_are_ignored_not_rejected():
    for event in ("order_refunded", "subscription_created", "license_key_created"):
        assert handler().handle(*request(payload(event=event))) == []
    assert handler().handle(*request(payload(test_mode=True))) == []
    assert len(handler(accept_test_mode=True).handle(*request(payload(test_mode=True)))) == 1


def test_LW5_an_order_from_another_store_is_ignored():
    assert handler().handle(*request(payload(store=2))) == []
    assert len(handler().handle(*request(payload(store="1")))) == 1                      # the store id may arrive as text


@pytest.mark.parametrize("mutate", [lambda p: p.pop("meta"), lambda p: p.pop("data"), lambda p: p["data"].pop("id"), lambda p: p["data"]["attributes"].pop("created_at"),
                                    lambda p: p["data"]["attributes"].update(created_at="yesterday"), lambda p: p["data"]["attributes"].pop("first_order_item"),
                                    lambda p: p["data"].update(id="has space"), lambda p: p["data"]["attributes"]["first_order_item"].update(product_id="bad product!")])
def test_LW6_a_signed_but_malformed_payload_is_a_fixed_code_not_a_crash(mutate):
    p = payload()
    mutate(p)
    headers, body = request(p, event="order_created")
    with pytest.raises(WebhookRejected) as e:
        handler().handle(headers, body)
    assert e.value.code == "INVALID_PAYLOAD"


def test_LW6b_not_json_and_a_mismatched_event_name_are_invalid_payloads():
    body = b"not json"
    with pytest.raises(WebhookRejected) as e:
        handler().handle({"x-signature": sign(SECRET, body)}, body)
    assert e.value.code == "INVALID_PAYLOAD"
    headers, body = request(payload(event="order_created"), event="order_refunded")
    with pytest.raises(WebhookRejected):
        handler().handle(headers, body)
    headers, body = request(payload())
    del headers["x-event-name"]
    assert len(handler().handle(headers, body)) == 1


def test_LW7_the_secret_must_be_usable():
    for bad in ("", "short", None, 5):
        with pytest.raises(ValueError):
            LemonSqueezyWebhook(bad, 1)

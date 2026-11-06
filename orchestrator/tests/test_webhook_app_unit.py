"""The webhook receiver with a fake inbox: routing, the secret path token, the order of the checks, answers. No database. Names WA*."""
import io
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from adapters.lemon_squeezy.webhook import LemonSqueezyWebhook, sign
from adapters.payhip.webhook import PayhipWebhook
from orchestrator.webhook_app import MAX_BODY, WebhookApp

LS_SECRET, PH_KEY = "whsec-0123456789", "payhip-key-0123456789"
LS_TOKEN, PH_TOKEN = "ls-path-token-0123456789", "ph-path-token-0123456789"
LEAD = uuid.uuid4()


class Inbox:
    def __init__(self, fail=False):
        self.rows, self.fail = {}, fail

    def write(self, signal, attributed_lead=None):
        if self.fail:
            raise RuntimeError("db down for jane.doe@example.test")
        key = (signal.marketplace.value, signal.signal_ref)
        if key in self.rows:
            return False
        self.rows[key] = (signal, attributed_lead)
        return True


def app(inbox=None):
    inbox = inbox if inbox is not None else Inbox()
    return WebhookApp(inbox, {"lemonsqueezy": (LemonSqueezyWebhook(LS_SECRET, 1), LS_TOKEN), "payhip": (PayhipWebhook(PH_KEY), PH_TOKEN)}), inbox


def ls_body(order="123", custom=None):
    meta = {"event_name": "order_created", "test_mode": False}
    if custom:
        meta["custom_data"] = custom
    return json.dumps({"meta": meta, "data": {"id": order, "attributes": {"store_id": 1, "created_at": "2026-11-02T10:00:00.000000Z",
                                                                           "user_email": "jane.doe@example.test", "first_order_item": {"product_id": 456}}}}).encode()


def ph_body(tx="tx-1", signature=None):
    import hashlib
    return json.dumps({"id": tx, "type": "paid", "email": "jane.doe@example.test", "date": int(datetime.now(timezone.utc).timestamp()),
                       "signature": signature or hashlib.sha256(PH_KEY.encode()).hexdigest(), "items": [{"product_key": "RGsF"}]}).encode()


def call(a, path, body=b"{}", method="POST", headers=None, length=None):
    env = {"REQUEST_METHOD": method, "PATH_INFO": path, "wsgi.input": io.BytesIO(body), "CONTENT_LENGTH": str(len(body) if length is None else length)}
    env.update({f"HTTP_{k.upper().replace('-', '_')}": v for k, v in (headers or {}).items()})
    out = {}
    chunks = a(env, lambda status, h: out.update(status=int(status.split()[0]), headers=dict(h)))
    return out["status"], json.loads(b"".join(chunks)), out["headers"]


def ls_call(a, body=None, token=LS_TOKEN, signature=None, **kw):
    body = body if body is not None else ls_body()
    return call(a, f"/webhooks/lemonsqueezy/{token}", body, headers={"X-Signature": signature or sign(LS_SECRET, body), "X-Event-Name": "order_created"}, **kw)


def test_WA1_the_receiver_will_not_start_misconfigured():
    h = LemonSqueezyWebhook(LS_SECRET, 1)
    for handlers in ({}, {"lemonsqueezy": (h, "short")}, {"lemonsqueezy": (h, "bad token with spaces!!")}, {"Wrong": (h, LS_TOKEN)}, {"payhip": (h, LS_TOKEN)}):
        with pytest.raises(ValueError):
            WebhookApp(Inbox(), handlers)


def test_WA2_a_signed_order_is_accepted_stored_and_a_repeat_delivery_adds_nothing():
    a, inbox = app()
    status, body, headers = ls_call(a, ls_body(custom={"ref": str(LEAD)}))
    assert (status, body) == (200, {"ok": True, "accepted": 1, "duplicates": 0}) and headers["Cache-Control"] == "no-store"
    ((signal, lead),) = inbox.rows.values()
    assert signal.signal_ref == "ls_order_123" and lead == LEAD
    assert ls_call(a, ls_body(custom={"ref": str(LEAD)}))[:2] == (200, {"ok": True, "accepted": 0, "duplicates": 1}) and len(inbox.rows) == 1


def test_WA3_an_unknown_name_or_a_wrong_token_looks_exactly_like_a_missing_page():
    a, inbox = app()
    answers = {json.dumps(call(a, p)[:2]) for p in ("/webhooks/lemonsqueezy/" + "x" * 24, "/webhooks/nosuch/" + LS_TOKEN, "/webhooks/lemonsqueezy/short", "/other",
                                                  "/webhooks/lemonsqueezy/" + LS_TOKEN + "x", "/webhooks/payhip/" + LS_TOKEN, "/webhooks/lemonsqueezy/" + PH_TOKEN)}
    assert answers == {json.dumps((404, {"error": "NOT_FOUND"}))} and inbox.rows == {}


def test_WA4_the_token_is_checked_before_the_signature_so_nothing_is_learned_without_it():
    a, inbox = app()
    status, body, _ = ls_call(a, token="wrong-token-0123456789", signature="0" * 64)
    assert (status, body) == (404, {"error": "NOT_FOUND"})
    assert ls_call(a, signature="0" * 64)[:2] == (401, {"error": "BAD_SIGNATURE"}) and inbox.rows == {}


def test_WA5_a_forged_payhip_event_with_the_right_constant_signature_still_needs_the_secret_url():
    a, inbox = app()
    forged = ph_body("forged-1")                                          # the signature is the constant anyone who saw one payload knows
    assert call(a, "/webhooks/payhip/" + "f" * 24, forged)[0] == 404 and inbox.rows == {}
    status, body, _ = call(a, "/webhooks/payhip/" + PH_TOKEN, forged)
    assert (status, body["accepted"]) == (200, 1)
    assert call(a, "/webhooks/payhip/" + PH_TOKEN, ph_body("x", signature="0" * 64))[:2] == (401, {"error": "BAD_SIGNATURE"})


def test_WA6_ignored_events_are_acknowledged_so_the_provider_stops_retrying_and_bad_payloads_are_400():
    a, inbox = app()
    refund = json.dumps({"meta": {"event_name": "order_refunded"}, "data": {"id": "1"}}).encode()
    assert call(a, f"/webhooks/lemonsqueezy/{LS_TOKEN}", refund, headers={"X-Signature": sign(LS_SECRET, refund), "X-Event-Name": "order_refunded"})[:2] == (
        200, {"ok": True, "accepted": 0, "duplicates": 0})
    junk = b"not json"
    assert call(a, f"/webhooks/lemonsqueezy/{LS_TOKEN}", junk, headers={"X-Signature": sign(LS_SECRET, junk)})[:2] == (400, {"error": "INVALID_PAYLOAD"})
    assert inbox.rows == {}


def test_WA7_method_and_size_are_checked():
    a, _ = app()
    path = f"/webhooks/lemonsqueezy/{LS_TOKEN}"
    assert call(a, path, method="GET")[:2] == (405, {"error": "METHOD_NOT_ALLOWED"})
    assert call(a, path, b"", length=0)[:2] == (400, {"error": "INVALID_PAYLOAD"}) and call(a, path, b"{}", length="x")[0] == 400
    assert call(a, path, b"{}", length=MAX_BODY + 1)[:2] == (413, {"error": "PAYLOAD_TOO_LARGE"})


def test_WA8_a_failure_inside_is_a_generic_500_the_provider_will_retry_and_leaks_nothing():
    a, _ = app(Inbox(fail=True))
    status, body, _ = ls_call(a)
    assert (status, body) == (500, {"error": "INTERNAL"}) and "jane" not in json.dumps(body)


def test_WA9_nothing_personal_is_stored_or_returned():
    a, inbox = app()
    status, body, _ = ls_call(a)
    assert "jane" not in json.dumps(body) and "example.test" not in repr(inbox.rows)
    assert call(a, "/webhooks/payhip/" + PH_TOKEN, ph_body("t2"))[0] == 200 and "example.test" not in repr(inbox.rows) and len(inbox.rows) == 2

"""Webhook to inbox to adapter to Scout to lead, on Postgres, with the real receiver. Needs TEST_ADMIN_DSN. Names WH*."""
import hashlib
import io
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from psycopg import errors

from adapters.lemon_squeezy.adapter import DEFAULT_BUCKET, LemonSqueezyAdapter
from adapters.lemon_squeezy.fake_transport import FakeLemonSqueezyTransport
from adapters.lemon_squeezy.webhook import LemonSqueezyWebhook, sign
from adapters.payhip.adapter import PayhipAdapter, PayhipProduct
from adapters.payhip.fake_transport import FakePayhipTransport
from adapters.payhip.transport import CONSERVATIVE_BUCKET
from adapters.payhip.webhook import PayhipWebhook
from orchestrator.lead_store import PgLeadRepo, PgLeadStore
from orchestrator.qualifier import Qualifier
from orchestrator.scoring import IcpDefinition, RuleScorer
from orchestrator.scout import Scout
from orchestrator.webhook_app import WebhookApp
from shared.bus.store import PgOutboxStore
from shared.bus.worker import Worker
from shared.governor import BucketConfig, InProcessGovernor, lane
from shared.signal_inbox import PgSignalInbox

LS_SECRET, PH_KEY = "whsec-0123456789", "payhip-key-0123456789"
LS_TOKEN, PH_TOKEN = "ls-path-token-0123456789", "ph-path-token-0123456789"
LEAD = uuid.uuid4()


def receiver(db):
    return WebhookApp(PgSignalInbox(db.conn), {"lemonsqueezy": (LemonSqueezyWebhook(LS_SECRET, 1), LS_TOKEN), "payhip": (PayhipWebhook(PH_KEY), PH_TOKEN)})


def ls_body(order="123", product=456, custom=None):
    created = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000000Z")
    meta = {"event_name": "order_created", "test_mode": False, **({"custom_data": custom} if custom else {})}
    return json.dumps({"meta": meta, "data": {"id": order, "attributes": {"store_id": 1, "created_at": created, "user_email": "jane.doe@example.test",
                                                                            "user_name": "Jane Doe", "first_order_item": {"product_id": product}}}}).encode()


def ph_body(tx="tx-1", key="RGsF"):
    return json.dumps({"id": tx, "type": "paid", "email": "jane.doe@example.test", "ip_address": "203.0.113.9", "signature": hashlib.sha256(PH_KEY.encode()).hexdigest(),
                       "date": int((datetime.now(timezone.utc) - timedelta(minutes=5)).timestamp()), "items": [{"product_key": key}]}).encode()


def post(app, path, body, headers=None):
    env = {"REQUEST_METHOD": "POST", "PATH_INFO": path, "wsgi.input": io.BytesIO(body), "CONTENT_LENGTH": str(len(body))}
    env.update({f"HTTP_{k.upper().replace('-', '_')}": v for k, v in (headers or {}).items()})
    out = {}
    chunks = app(env, lambda s, h: out.update(status=int(s.split()[0])))
    return out["status"], json.loads(b"".join(chunks))


def send_ls(app, body=None, token=LS_TOKEN, signature=None):
    body = body or ls_body()
    return post(app, f"/webhooks/lemonsqueezy/{token}", body, {"X-Signature": signature or sign(LS_SECRET, body), "X-Event-Name": "order_created"})


def send_ph(app, body=None, token=PH_TOKEN):
    return post(app, f"/webhooks/payhip/{token}", body or ph_body())


def one(db, q, *a):
    return db.conn.execute(q, a).fetchone()[0]


def scout_for(db, adapter, marketplace, account, bucket):
    gov = InProcessGovernor({lane(marketplace, account): BucketConfig(**bucket)})
    return Scout(PgLeadStore(db.conn), gov, {(marketplace, account): adapter})


def ls_adapter(db):
    return LemonSqueezyAdapter(FakeLemonSqueezyTransport(), PgSignalInbox(db.conn))


def ph_adapter(db):
    return PayhipAdapter(FakePayhipTransport(), [PayhipProduct("RGsF", "Budget Planner", 1900)], PgSignalInbox(db.conn))


def test_WH1_a_signed_webhook_lands_in_the_inbox_and_the_adapter_hands_it_to_the_scout_as_a_signal(db):
    assert send_ls(receiver(db)) == (200, {"ok": True, "accepted": 1, "duplicates": 0})
    got = ls_adapter(db).fetch_signals(datetime.now(timezone.utc) - timedelta(hours=2))
    assert [(s.signal_ref, s.kind, s.product_external_id) for s in got] == [("ls_order_123", "SALE", "456")]


def test_WH2_lemon_squeezy_to_a_lead_end_to_end_and_a_repeat_creates_nothing(db):
    app, adapter = receiver(db), ls_adapter(db)
    send_ls(app)
    scout = scout_for(db, adapter, "LEMON_SQUEEZY", "store-1", DEFAULT_BUCKET)
    (r,) = scout.run_once()
    assert (r.status, r.new_leads) == ("ok", 1)
    lead = db.conn.execute("SELECT source_type, source_marketplace::text, signal_ref, status::text, contact_id FROM core.lead").fetchone()
    assert lead == ("MARKETPLACE_SIGNAL", "LEMON_SQUEEZY", "ls_order_123", "SIGNAL_CAPTURED", None)
    payload = db.conn.execute("SELECT payload FROM core.outbox_event WHERE event_type='signal.captured'").fetchone()[0]
    assert payload["kind"] == "SALE" and payload["product_external_id"] == "456" and payload["has_contact"] is False
    assert send_ls(app)[1] == {"ok": True, "accepted": 0, "duplicates": 1}
    assert scout.run_once()[0].new_leads == 0 and one(db, "SELECT count(*) FROM core.lead") == 1


def test_WH3_payhip_to_a_lead_end_to_end(db):
    app = receiver(db)
    assert send_ph(app)[1]["accepted"] == 1
    (r,) = scout_for(db, ph_adapter(db), "PAYHIP", "shop-1", CONSERVATIVE_BUCKET).run_once()
    assert r.new_leads == 1
    assert db.conn.execute("SELECT source_marketplace::text, signal_ref FROM core.lead").fetchone() == ("PAYHIP", "ph_tx_tx-1_0")
    assert db.conn.execute("SELECT payload->>'product_external_id' FROM core.outbox_event WHERE event_type='signal.captured'").fetchone()[0] == "RGsF"


def test_WH4_the_lead_id_a_lemon_squeezy_checkout_carried_is_kept_and_payhip_never_has_one(db):
    app = receiver(db)
    send_ls(app, ls_body(order="777", custom={"ref": str(LEAD)}))
    send_ph(app)
    rows = dict(db.conn.execute("SELECT marketplace::text, attributed_lead_id FROM core.signal_inbox").fetchall())
    assert rows == {"LEMON_SQUEEZY": LEAD, "PAYHIP": None}


def test_WH5_a_request_that_fails_any_check_stores_nothing(db):
    app = receiver(db)
    assert send_ls(app, signature="0" * 64)[0] == 401 and send_ls(app, token="wrong-token-0123456789")[0] == 404
    assert post(app, f"/webhooks/lemonsqueezy/{LS_TOKEN}", b"not json", {"X-Signature": sign(LS_SECRET, b"not json")})[0] == 400
    forged = ph_body("forged")
    assert send_ph(app, forged, token="f" * 24)[0] == 404 and one(db, "SELECT count(*) FROM core.signal_inbox") == 0


def test_WH6_the_receiver_runs_with_the_application_role_only(db):
    db.conn.execute("SET ROLE app_rw")
    try:
        app = receiver(db)
        assert send_ls(app)[1]["accepted"] == 1 and send_ph(app)[1]["accepted"] == 1
    finally:
        db.conn.execute("RESET ROLE")
    assert one(db, "SELECT count(*) FROM core.signal_inbox") == 2


def test_WH7_a_webhook_sale_of_an_ideal_product_is_scored_through_the_bus_with_the_consent_risk_flag(db):
    send_ls(receiver(db), ls_body(product=456))
    scout_for(db, ls_adapter(db), "LEMON_SQUEEZY", "store-1", DEFAULT_BUCKET).run_once()
    worker = Worker(PgOutboxStore(db.conn), "w1")
    Qualifier(RuleScorer(IcpDefinition(frozenset({"456"}))), PgLeadRepo()).register(worker)
    assert worker.drain().retried == 0
    assert db.conn.execute("SELECT status::text, composite_score, risk_flags FROM core.lead").fetchone() == ("QUALIFIED", 75, ["CONSENT_RISK"])


def test_WH8_no_buyer_detail_reaches_the_inbox_a_lead_or_an_event(db):
    app = receiver(db)
    send_ls(app)
    send_ph(app)
    scout_for(db, ls_adapter(db), "LEMON_SQUEEZY", "store-1", DEFAULT_BUCKET).run_once()
    scout_for(db, ph_adapter(db), "PAYHIP", "shop-1", CONSERVATIVE_BUCKET).run_once()
    blob = "".join(json.dumps(db.conn.execute(q).fetchall(), default=str) for q in (
        "SELECT row_to_json(i) FROM core.signal_inbox i", "SELECT row_to_json(l) FROM core.lead l", "SELECT row_to_json(e) FROM core.outbox_event e"))
    assert "example.test" not in blob and "Jane" not in blob and "203.0.113" not in blob

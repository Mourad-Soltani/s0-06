"""Scout -> bus -> Qualifier against Postgres, with the real Gumroad adapter over a fake transport. Needs
TEST_ADMIN_DSN. Names PL*."""
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from adapters.gumroad.adapter import GumroadAdapter
from adapters.gumroad.fake_transport import FakeGumroadTransport
from orchestrator.lead_store import PgLeadRepo, PgLeadStore
from orchestrator.qualifier import Qualifier
from orchestrator.scoring import IcpDefinition, RuleScorer
from orchestrator.scout import Scout, ScoutConfig
from shared.bus.events import EventType
from shared.bus.publisher import publish
from shared.bus.store import PgOutboxStore
from shared.bus.worker import Worker
from shared.governor import BucketConfig, InProcessGovernor, lane
from shared.models import Marketplace, Signal

EMAIL, NAME = "jane.doe@example.test", "Jane Doe"
ICP = IcpDefinition(frozenset({"gr-1"}))


def iso(minutes_ago):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def sale(sid, minutes_ago=30, product="gr-1"):
    return {"id": sid, "created_at": iso(minutes_ago), "product_id": product, "email": EMAIL, "full_name": NAME}


def build(db, sales, **cfg):
    transport = FakeGumroadTransport()
    transport.sales.extend(sales)
    gov = InProcessGovernor({lane(Marketplace.GUMROAD, "acct"): BucketConfig(capacity=100, refill_per_second=100)})
    store = PgLeadStore(db.conn)
    scout = Scout(store, gov, {("GUMROAD", "acct"): GumroadAdapter(transport)}, ScoutConfig(**cfg))
    return scout, store, transport


def worker(db, **kw):
    w = Worker(PgOutboxStore(db.conn), "w1", **kw)
    Qualifier(RuleScorer(ICP), PgLeadRepo()).register(w)
    return w


def one(db, q, *a):
    return db.conn.execute(q, a).fetchone()[0]


def events(db, etype):
    return db.conn.execute("SELECT payload, lead_id, trace_id, status FROM core.outbox_event WHERE event_type=%s", (etype,)).fetchall()


def test_PL1_a_sale_becomes_a_qualified_lead_end_to_end(db):
    scout, _, _ = build(db, [sale("s1")])
    (r,) = scout.run_once()
    assert (r.status, r.new_leads, r.seen) == ("ok", 1, 1)
    lead, status, contact = db.conn.execute("SELECT id, status::text, contact_id FROM core.lead").fetchone()
    assert status == "SIGNAL_CAPTURED" and contact is None
    ((payload, lead_id, trace_id, _),) = events(db, "signal.captured")
    assert lead_id == lead and trace_id == lead and payload["kind"] == "SALE" and payload["product_external_id"] == "gr-1"
    assert worker(db).drain().retried == 0
    row = db.conn.execute("SELECT status::text, composite_score, confidence, risk_flags FROM core.lead").fetchone()
    assert row == ("QUALIFIED", 75, 0.80, ["CONSENT_RISK"]) or (row[0], row[1], float(row[2]), row[3]) == ("QUALIFIED", 75, 0.8, ["CONSENT_RISK"])
    assert [p["composite"] for p, *_ in events(db, "lead.scored")] == [75]
    assert len(events(db, "lead.qualified")) == 1
    assert one(db, "SELECT count(*) FROM core.outbox_event WHERE status <> 'DONE'") == 0


def test_PL2_an_off_catalog_sale_goes_to_review_and_is_not_announced_as_qualified(db):
    scout, _, _ = build(db, [sale("s1", product="zz")])
    scout.run_once()
    worker(db).drain()
    assert db.conn.execute("SELECT status::text, composite_score FROM core.lead").fetchone() == ("NEEDS_REVIEW", 54)
    assert len(events(db, "lead.scored")) == 1 and events(db, "lead.qualified") == []


def test_PL3_running_the_scout_twice_creates_one_lead_and_one_event_per_sale(db):
    scout, store, _ = build(db, [sale("a", 50), sale("b", 20)])
    scout.run_once()
    (r,) = scout.run_once()
    assert r.new_leads == 0 and one(db, "SELECT count(*) FROM core.lead") == 2
    assert len(events(db, "signal.captured")) == 2
    assert store.get_cursor("GUMROAD", "acct") is not None


def test_PL4_redelivering_signal_captured_does_not_score_twice(db):
    scout, _, _ = build(db, [sale("s1")])
    scout.run_once()
    w = worker(db)
    w.drain()
    before = db.conn.execute("SELECT composite_score, updated_at FROM core.lead").fetchone()
    db.conn.execute("UPDATE core.outbox_event SET status='PENDING', claimed_by=NULL, claimed_until=NULL "
                    "WHERE event_type='signal.captured'")                       # as if the ack were lost
    w.drain()
    assert db.conn.execute("SELECT composite_score, updated_at FROM core.lead").fetchone() == before
    assert len(events(db, "lead.scored")) == 1


def test_PL5_a_deep_review_queue_pauses_intake_without_calling_the_marketplace(db):
    for _ in range(3):
        db.conn.execute("INSERT INTO core.lead(source_type,signal_ref,signal_id,status) "
                        "VALUES ('INBOUND',%s,%s,'NEEDS_REVIEW')", (f"q_{uuid.uuid4().hex[:8]}", uuid.uuid4().hex * 2))
    scout, store, transport = build(db, [sale("s1")], max_review_depth=3)
    assert store.queue_depth() == 3
    (r,) = scout.run_once()
    assert r.status == "paused_backpressure" and "get_sales" not in transport.calls
    assert one(db, "SELECT count(*) FROM core.lead") == 3


def test_PL6_the_daily_cap_stops_mid_batch_and_holds_the_rest_for_later(db):
    scout, store, _ = build(db, [sale("a", 50), sale("b", 40), sale("c", 30)], max_new_leads_per_day=2)
    (r,) = scout.run_once()
    assert (r.status, r.new_leads) == ("daily_cap", 2) and one(db, "SELECT count(*) FROM core.lead") == 2
    assert scout.run_once()[0].status == "daily_cap" and one(db, "SELECT count(*) FROM core.lead") == 2
    assert store.new_leads_today() == 2


def test_PL7_the_cursor_never_moves_backwards(db):
    store = PgLeadStore(db.conn)
    later, earlier = datetime(2026, 10, 19, 12, tzinfo=timezone.utc), datetime(2026, 10, 19, 6, tzinfo=timezone.utc)
    store.set_cursor("GUMROAD", "acct", later)
    store.set_cursor("GUMROAD", "acct", earlier)
    assert store.get_cursor("GUMROAD", "acct") == later and store.get_cursor("GUMROAD", "other") is None


def test_PL8_the_same_signal_on_two_marketplaces_is_two_leads(db):
    store = PgLeadStore(db.conn)
    s = Signal(signal_ref="same_ref", kind="SALE", marketplace=Marketplace.GUMROAD, occurred_at=datetime.now(timezone.utc))
    assert store.ingest(s, "GUMROAD") and store.ingest(s, "ETSY")
    assert not store.ingest(s, "GUMROAD") and not store.ingest(s, "ETSY")
    assert one(db, "SELECT count(*) FROM core.lead") == 2


def test_PL9_queue_depth_counts_only_the_review_statuses(db):
    for status in ("NEEDS_REVIEW", "OUTREACH_DRAFTED", "APPROVAL_PENDING", "QUALIFIED", "DISQUALIFIED", "SIGNAL_CAPTURED"):
        db.conn.execute("INSERT INTO core.lead(source_type,signal_ref,signal_id,status) VALUES ('INBOUND',%s,%s,%s::core.lead_status)",
                        (f"d_{uuid.uuid4().hex[:8]}", uuid.uuid4().hex * 2, status))
    assert PgLeadStore(db.conn).queue_depth() == 3


def test_PL10_no_buyer_detail_reaches_a_lead_or_an_event(db):
    scout, _, _ = build(db, [sale("s1")])
    scout.run_once()
    worker(db).drain()
    blob = json.dumps(db.conn.execute("SELECT row_to_json(l) FROM core.lead l").fetchall(), default=str) + \
        json.dumps(db.conn.execute("SELECT row_to_json(e) FROM core.outbox_event e").fetchall(), default=str)
    assert EMAIL not in blob and NAME not in blob and "jane" not in blob.lower()


def test_PL11_the_whole_pipeline_runs_as_the_application_role(db):
    scout, _, _ = build(db, [sale("s1")])
    db.conn.execute("SET ROLE app_rw")
    try:
        assert scout.run_once()[0].new_leads == 1
        worker(db).drain()
    finally:
        db.conn.execute("RESET ROLE")
    assert one(db, "SELECT status::text FROM core.lead") == "QUALIFIED"


def test_PL12_a_malformed_signal_event_is_dead_lettered_and_never_scored(db):
    lead = one(db, "INSERT INTO core.lead(source_type,signal_ref,signal_id,status) VALUES ('INBOUND','bad',%s,'SIGNAL_CAPTURED') RETURNING id",
               uuid.uuid4().hex * 2)
    publish(db.conn, EventType.SIGNAL_CAPTURED, marketplace="GUMROAD", lead_id=lead, payload={})
    res = worker(db, max_attempts=2, backoff_seconds=0).drain()
    assert res.dead == 1
    assert db.conn.execute("SELECT status, last_error_code FROM core.outbox_event WHERE event_type='signal.captured'").fetchone() \
        == ("DEAD", "VALUE_ERROR")
    assert one(db, "SELECT status::text FROM core.lead WHERE id=%s", lead) == "SIGNAL_CAPTURED"

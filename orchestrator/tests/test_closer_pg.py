"""The Closer on Postgres: the real send gate, kill switch, consent and the audit trail, with the real email and Gumroad adapters over
fake transports. Needs TEST_ADMIN_DSN. Names CLP*."""
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from psycopg import errors
from psycopg.types.json import Json

from adapters.email_channel.adapter import EmailChannelAdapter
from adapters.email_channel.fake_transport import FakeEmailTransport
from adapters.email_channel.message import EmailConfig
from adapters.errors import RateLimited, TransportError
from adapters.gumroad.adapter import GumroadAdapter
from adapters.gumroad.fake_transport import FakeGumroadTransport
from orchestrator.closer import KILLED, NOT_APPROVED, SENT, SHADOW, Closer
from orchestrator.guard import ComplianceGuard
from orchestrator.guard_rules import GuardConfig
from shared.consent import ConsentService

EMAIL_CFG = EmailConfig("1 Example Street, Paris 75001, France", "https://example.test/unsubscribe")
GUARD_CFG = GuardConfig(allowed_link_hosts=frozenset({"shop.example.test"}), max_discount_pct=Decimal("20"))
PLAIN = "https://shop.example.test/l/gr-1"
BODY = f"Subject: Your guide is ready\n\nHi,\n\nHere it is: {PLAIN}\n\nThanks!"


def one(db, q, *a):
    return db.conn.execute(q, a).fetchone()[0]


def build(db):
    gumroad = FakeGumroadTransport()
    gumroad.add_product("gr-1", short_url=PLAIN)
    email = EmailChannelAdapter(FakeEmailTransport(), EMAIL_CFG)

    @contextmanager
    def connect():
        yield db.conn

    return Closer(connect, GumroadAdapter(gumroad), {"EMAIL": email}, ComplianceGuard({"EMAIL": email}, GUARD_CFG)), email


def approved(db, *, tier=1, status="APPROVED", body=BODY, email_addr=None):
    perm = ConsentService(db.conn).capture(email_addr or f"c{uuid.uuid4().hex[:8]}@example.test", "EMAIL", "https://example.test/optin")
    contact = one(db, "SELECT contact_id FROM pii.contact_permission WHERE id=%s", perm)
    lead = one(db, "INSERT INTO core.lead(source_type,signal_ref,signal_id,contact_id,product_external_id) VALUES ('INBOUND',%s,%s,%s,'gr-1') RETURNING id",
               f"sig_{uuid.uuid4().hex[:10]}", uuid.uuid4().hex * 2, contact)
    payload = {"context": "consented_outreach", "consent_ref": str(perm), "body": body, "product_external_id": "gr-1", "discount_pct": None}
    ref = one(db, "INSERT INTO pii.payload_store(lead_id, body) VALUES (%s,%s) RETURNING payload_id", lead, Json(payload))
    pid = one(db, "INSERT INTO core.action_proposal(lead_id,role,marketplace,channel,action_type,payload_ref,autonomy_tier,confidence,compliance_verdict,"
                  "status,contact_permission_id,idempotency_key,audit_trace_id) VALUES (%s,'COPYWRITER','EMAIL','EMAIL','SEND_MESSAGE',%s,%s,0.8,'PASS',"
                  "%s::core.proposal_status,%s,gen_random_uuid(),gen_random_uuid()) RETURNING id", lead, ref, tier, status, perm)
    return pid, lead, contact, perm


def status(db, pid):
    return one(db, "SELECT status::text FROM core.action_proposal WHERE id=%s", pid)


def audit(db, action):
    return db.conn.execute("SELECT actor, marketplace::text, lead_id, autonomy_tier, outcome FROM audit.event WHERE action_type=%s ORDER BY seq", (action,)).fetchall()


def test_CLP1_an_approved_tier_1_draft_is_sent_once_with_the_attributed_link_and_recorded(db):
    closer, email = build(db)
    pid, lead, contact, perm = approved(db)
    assert closer.process(pid).outcome == SENT and status(db, pid) == "SENT"
    (sent,) = email.transport.sent
    assert f"{PLAIN}?ref={lead}" in sent.text and "To stop receiving these emails" in sent.text and sent.thread_id == str(contact)
    assert email.transport.sent_keys == [str(one(db, "SELECT idempotency_key FROM core.action_proposal WHERE id=%s", pid))]
    assert audit(db, "CLOSER_SENT") == [("closer", "EMAIL", lead, 1, "OK")]
    payload, ev_lead, trace = db.conn.execute("SELECT payload, lead_id, trace_id FROM core.outbox_event WHERE event_type='action.sent'").fetchone()
    assert payload["proposal_id"] == str(pid) and payload["link"] == "ATTRIBUTED" and len(payload["provider_ref"]) == 32 and ev_lead == trace == lead


def test_CLP2_a_replay_sends_nothing_more(db):
    closer, email = build(db)
    pid, *_ = approved(db)
    closer.process(pid)
    assert closer.process(pid).outcome == NOT_APPROVED and len(email.transport.sent) == 1
    assert one(db, "SELECT count(*) FROM core.outbox_event WHERE event_type='action.sent'") == 1


def test_CLP3_a_tier_0_draft_is_approved_for_the_gate_but_never_sent(db):
    closer, email = build(db)
    pid, lead, *_ = approved(db, tier=0)
    assert closer.process(pid).outcome == SHADOW and status(db, pid) == "APPROVED" and email.transport.sent == []
    assert audit(db, "CLOSER_SHADOW") == [("closer", "EMAIL", lead, 0, SHADOW)] and closer.sweep() == []              # the sweep ignores tier 0


def test_CLP4_a_kill_switch_holds_the_send_and_the_sweep_sends_it_after_the_release(db):
    closer, email = build(db)
    pid, *_ = approved(db)
    db.conn.execute("SELECT core.set_kill_switch('GLOBAL', true, 'ops-1', 'TEST_REASON')")
    assert closer.process(pid).outcome == KILLED and status(db, pid) == "APPROVED" and email.transport.sent == []
    db.conn.execute("SELECT core.set_kill_switch('GLOBAL', false, 'ops-1')")
    assert [r.outcome for r in closer.sweep()] == [SENT] and len(email.transport.sent) == 1 and status(db, pid) == "SENT"


def test_CLP5_consent_withdrawn_after_approval_stops_the_send(db):
    closer, email = build(db)
    pid, lead, contact, perm = approved(db)
    ConsentService(db.conn).withdraw(contact, "EMAIL")
    assert closer.process(pid).outcome == "FINAL_CHECK_NO_CURRENT_PERMISSION" and status(db, pid) == "FAILED" and email.transport.sent == []
    other, _, c2, _ = approved(db)
    ConsentService(db.conn).quarantine(c2)
    assert closer.process(other).outcome == "FINAL_CHECK_NO_CURRENT_PERMISSION" and email.transport.sent == []


def test_CLP6_a_final_text_that_breaks_the_rules_is_not_sent(db):
    closer, email = build(db)
    pid, *_ = approved(db, body=BODY + "\nAlso https://evil.test/x")
    assert closer.process(pid).outcome == "FINAL_CHECK_LINK_HOST_NOT_ALLOWED" and status(db, pid) == "FAILED" and email.transport.sent == []


def test_CLP7_a_rate_limited_send_goes_back_in_line_and_the_sweep_retries_it_once(db):
    closer, email = build(db)
    pid, *_ = approved(db)
    email.transport.inject("send", RateLimited(5.0))
    assert closer.process(pid).outcome == "RETRY_LATER_RATE_LIMITED" and status(db, pid) == "APPROVED" and email.transport.sent == []
    assert [r.outcome for r in closer.sweep()] == [SENT] and len(email.transport.sent) == 1
    assert closer.sweep() == []


def test_CLP8_a_send_whose_outcome_is_unknown_is_failed_and_never_retried_blindly(db):
    closer, email = build(db)
    pid, *_ = approved(db)
    email.transport.inject("send", TransportError())
    assert closer.process(pid).outcome == "SEND_FAILED_TRANSPORT_ERROR" and status(db, pid) == "FAILED"
    assert closer.sweep() == [] and email.transport.sent == [] and audit(db, "CLOSER_FAILED")[0][4] == "SEND_FAILED_TRANSPORT_ERROR"


def test_CLP9_the_sweep_takes_only_approved_tier_1_proposals_oldest_first(db):
    closer, email = build(db)
    first, *_ = approved(db)
    approved(db, tier=0)
    approved(db, status="PROPOSED")
    second, *_ = approved(db)
    db.conn.execute("UPDATE core.action_proposal SET updated_at = now() - interval '1 hour' WHERE id=%s", (second,))
    assert [r.proposal_id for r in closer.sweep()] == [second, first] and len(email.transport.sent) == 2


def test_CLP10_a_proposal_that_was_never_approved_is_not_sent(db):
    closer, email = build(db)
    pid, *_ = approved(db, status="PROPOSED")
    assert closer.process(pid).outcome == NOT_APPROVED and email.transport.sent == [] and status(db, pid) == "PROPOSED"


def test_CLP11_the_review_role_can_send_and_the_app_role_cannot_read_bodies_so_nothing_changes(db):
    closer, email = build(db)
    a, *_ = approved(db)
    db.conn.execute("SET ROLE review_svc")
    try:
        assert closer.process(a).outcome == SENT
    finally:
        db.conn.execute("RESET ROLE")
    b, *_ = approved(db)
    db.conn.execute("SET ROLE app_rw")
    try:
        with pytest.raises(errors.InsufficientPrivilege):
            closer.process(b)
    finally:
        db.conn.execute("RESET ROLE")
    assert status(db, b) == "APPROVED" and len(email.transport.sent) == 1

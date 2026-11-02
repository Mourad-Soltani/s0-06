"""The reconciliation job on Postgres, with the real email adapter and the Closer: a crash is simulated by claiming a proposal and
never recording it. Needs TEST_ADMIN_DSN. Names RNP*."""
import hashlib
import uuid
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal

import pytest
from psycopg.types.json import Json

from adapters.email_channel.adapter import EmailChannelAdapter
from adapters.email_channel.fake_transport import FakeEmailTransport
from adapters.email_channel.message import EmailConfig, OutboundEmail
from adapters.gumroad.adapter import GumroadAdapter
from adapters.gumroad.fake_transport import FakeGumroadTransport
from orchestrator.closer import SENT, Closer
from orchestrator.guard import ComplianceGuard
from orchestrator.guard_rules import GuardConfig
from orchestrator.reconciler import RECOVERED_SENT, REQUEUED, RESOLVED_ELSEWHERE, Reconciler
from shared.consent import ConsentService

PLAIN = "https://shop.example.test/l/gr-1"
BODY = f"Subject: Your guide is ready\n\nHi,\n\nHere it is: {PLAIN}\n\nThanks!"
PROVIDER_REF = lambda pid: hashlib.sha256(pid.encode()).hexdigest()[:32]  # noqa: E731


def one(db, q, *a):
    return db.conn.execute(q, a).fetchone()[0]


class World:
    def __init__(self, db, alerts=None):
        self.db = db
        gumroad = FakeGumroadTransport()
        gumroad.add_product("gr-1", short_url=PLAIN)
        self.email = EmailChannelAdapter(FakeEmailTransport(), EmailConfig("1 Example Street, Paris 75001, France", "https://example.test/unsubscribe"))

        @contextmanager
        def connect():
            yield db.conn

        guard = ComplianceGuard({"EMAIL": self.email}, GuardConfig(allowed_link_hosts=frozenset({"shop.example.test"}), max_discount_pct=Decimal("20")))
        self.closer = Closer(connect, GumroadAdapter(gumroad), {"EMAIL": self.email}, guard)
        self.alerts = alerts if alerts is not None else []
        self.reconciler = Reconciler(connect, {"EMAIL": self.email}, stale_after=timedelta(minutes=15), alert=lambda p, o: self.alerts.append((p, o)))

    def approved(self):
        db = self.db
        perm = ConsentService(db.conn).capture(f"c{uuid.uuid4().hex[:8]}@example.test", "EMAIL", "https://example.test/optin")
        contact = one(db, "SELECT contact_id FROM pii.contact_permission WHERE id=%s", perm)
        lead = one(db, "INSERT INTO core.lead(source_type,signal_ref,signal_id,contact_id,product_external_id) VALUES ('INBOUND',%s,%s,%s,'gr-1') RETURNING id",
                   f"sig_{uuid.uuid4().hex[:10]}", uuid.uuid4().hex * 2, contact)
        payload = {"context": "consented_outreach", "consent_ref": str(perm), "body": BODY, "product_external_id": "gr-1", "discount_pct": None}
        ref = one(db, "INSERT INTO pii.payload_store(lead_id, body) VALUES (%s,%s) RETURNING payload_id", lead, Json(payload))
        return one(db, "INSERT INTO core.action_proposal(lead_id,role,marketplace,channel,action_type,payload_ref,autonomy_tier,confidence,compliance_verdict,"
                       "status,contact_permission_id,idempotency_key,audit_trace_id) VALUES (%s,'COPYWRITER','EMAIL','EMAIL','SEND_MESSAGE',%s,1,0.8,'PASS',"
                       "'APPROVED',%s,gen_random_uuid(),gen_random_uuid()) RETURNING id", lead, ref, perm)

    def crash_after_claim(self, minutes_ago=30, provider_accepted=True):
        """The Closer claimed the proposal (SENDING, committed), maybe the provider took the message, then the process died."""
        pid = self.approved()
        key = one(self.db, "SELECT idempotency_key FROM core.action_proposal WHERE id=%s", pid)
        self.db.conn.execute("UPDATE core.action_proposal SET status='SENDING' WHERE id=%s", (pid,))
        if provider_accepted:
            self.email.transport.send(OutboundEmail("thread", "Hello", "text"), str(key))
        self.age(pid, minutes_ago)
        return pid, key

    def age(self, pid, minutes):
        self.db.conn.execute("UPDATE core.action_proposal SET updated_at = now() - make_interval(mins => %s) WHERE id=%s", (minutes, pid))


def row(db, pid):
    return db.conn.execute("SELECT status::text, provider_ref FROM core.action_proposal WHERE id=%s", (pid,)).fetchone()


def audit(db, action):
    return db.conn.execute("SELECT actor, marketplace::text, outcome FROM audit.event WHERE action_type=%s ORDER BY seq", (action,)).fetchall()


def test_RNP1_a_send_the_provider_confirms_is_recorded_as_sent_without_sending_again(db):
    w = World(db)
    pid, key = w.crash_after_claim()
    sent_before = len(w.email.transport.sent)
    (res,) = w.reconciler.run()
    assert (res.proposal_id, res.outcome) == (pid, RECOVERED_SENT) and len(w.email.transport.sent) == sent_before == 1
    status, ref = row(db, pid)
    assert status == "SENT" and ref == PROVIDER_REF(w.email.transport.provider_ids[str(key)])
    payload, lead, trace = db.conn.execute("SELECT payload, lead_id, trace_id FROM core.outbox_event WHERE event_type='action.sent'").fetchone()
    assert payload["recovered"] is True and payload["provider_ref"] == ref and lead == trace
    assert audit(db, "RECONCILER_SENT") == [("reconciler", "EMAIL", "OK")]


def test_RNP2_a_send_the_provider_never_saw_is_requeued_and_the_closer_sends_it_exactly_once(db):
    w = World(db)
    pid, key = w.crash_after_claim(provider_accepted=False)
    assert [r.outcome for r in w.reconciler.run()] == [REQUEUED] and row(db, pid)[0] == "APPROVED" and w.email.transport.sent == []
    assert [r.outcome for r in w.closer.sweep()] == [SENT] and len(w.email.transport.sent) == 1
    assert w.email.transport.sent_keys == [str(key)]                                    # the same idempotency key as the lost attempt
    status, ref = row(db, pid)
    assert status == "SENT" and ref == PROVIDER_REF(w.email.transport.provider_ids[str(key)])
    assert w.reconciler.run() == [] and w.closer.sweep() == []


def test_RNP3_a_provider_that_cannot_answer_leaves_it_sending_alerts_and_looks_again_only_after_the_threshold(db):
    w = World(db)
    w.email.transport.supports_lookup = False
    pid, _ = w.crash_after_claim()
    (res,) = w.reconciler.run()
    assert res.outcome == "UNRESOLVED_LOOKUP_UNSUPPORTED" and res.alerted and w.alerts == [(pid, "UNRESOLVED_LOOKUP_UNSUPPORTED")]
    assert row(db, pid)[0] == "SENDING" and audit(db, "RECONCILER_UNRESOLVED") == [("reconciler", "EMAIL", "LOOKUP_UNSUPPORTED")]
    assert w.reconciler.run() == []                                                      # its clock was reset: not looked at on every run
    w.age(pid, 30)
    assert [r.outcome for r in w.reconciler.run()] == ["UNRESOLVED_LOOKUP_UNSUPPORTED"] and len(audit(db, "RECONCILER_UNRESOLVED")) == 2


def test_RNP4_a_failed_lookup_is_never_read_as_not_sent(db):
    from adapters.errors import RateLimited
    w = World(db)
    pid, _ = w.crash_after_claim(provider_accepted=False)
    w.email.transport.inject("lookup", RateLimited(5.0))
    assert [r.outcome for r in w.reconciler.run()] == ["UNRESOLVED_RATE_LIMITED"] and row(db, pid)[0] == "SENDING" and w.closer.sweep() == []


def test_RNP5_a_send_still_within_the_threshold_is_not_touched(db):
    w = World(db)
    pid, _ = w.crash_after_claim(minutes_ago=5)
    assert w.reconciler.run() == [] and row(db, pid)[0] == "SENDING" and w.alerts == []


def test_RNP6_if_the_closer_finishes_while_the_provider_is_being_asked_the_closer_wins(db):
    w = World(db)
    pid, _ = w.crash_after_claim(provider_accepted=False)
    original = w.email.find_send

    def closer_finishes_first(key):
        db.conn.execute("UPDATE core.action_proposal SET status='SENT', updated_at=now() WHERE id=%s", (pid,))
        return original(key)

    w.email.find_send = closer_finishes_first
    (res,) = w.reconciler.run()
    assert res.outcome == RESOLVED_ELSEWHERE and row(db, pid)[0] == "SENT" and audit(db, "RECONCILER_REQUEUED") == [] and w.closer.sweep() == []


def test_RNP7_the_closer_records_the_provider_reference_on_a_normal_send(db):
    w = World(db)
    pid = w.approved()
    assert w.closer.process(pid).outcome == SENT
    key = one(db, "SELECT idempotency_key FROM core.action_proposal WHERE id=%s", pid)
    assert row(db, pid) == ("SENT", PROVIDER_REF(w.email.transport.provider_ids[str(key)]))


def test_RNP8_the_job_needs_no_access_to_message_bodies_so_the_application_role_is_enough(db):
    w = World(db)
    pid, _ = w.crash_after_claim()
    db.conn.execute("SET ROLE app_rw")
    try:
        assert [r.outcome for r in w.reconciler.run()] == [RECOVERED_SENT]
    finally:
        db.conn.execute("RESET ROLE")
    assert row(db, pid)[0] == "SENT"


def test_RNP9_several_stuck_sends_are_each_settled_correctly_in_one_run(db):
    w = World(db)
    sent, _ = w.crash_after_claim(minutes_ago=60)
    lost, _ = w.crash_after_claim(minutes_ago=45, provider_accepted=False)
    fresh, _ = w.crash_after_claim(minutes_ago=2)
    assert [(r.proposal_id, r.outcome) for r in w.reconciler.run()] == [(sent, RECOVERED_SENT), (lost, REQUEUED)]
    assert (row(db, sent)[0], row(db, lost)[0], row(db, fresh)[0]) == ("SENT", "APPROVED", "SENDING")


def test_RNP10_nothing_personal_reaches_the_audit_trail_or_the_event(db):
    w = World(db)
    w.crash_after_claim()
    w.reconciler.run()
    import json
    blob = "".join(json.dumps(db.conn.execute(q).fetchall(), default=str) for q in (
        "SELECT row_to_json(e) FROM core.outbox_event e", "SELECT row_to_json(a) FROM audit.event a"))
    assert "example.test" not in blob and "esp-" not in blob                              # neither an address nor the raw provider id

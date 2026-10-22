"""Kill switch against Postgres, end to end with the bus worker. Needs TEST_ADMIN_DSN. Names are KS*."""
import threading
import time
import uuid

import psycopg
import pytest
from psycopg import errors

from shared.bus.events import EventType
from shared.bus.publisher import publish
from shared.bus.runner import run_loop
from shared.bus.store import PgOutboxStore
from shared.bus.worker import Worker
from shared.killswitch import GLOBAL, KillSwitch, PgKillStore


class Revoker:
    def __init__(self, fail=()):
        self.calls, self.fail = [], set(fail)

    def revoke(self, marketplace):
        self.calls.append(marketplace)
        if marketplace in self.fail:
            raise RuntimeError("no")


def ks(db, revoker=None, audit=None):
    return KillSwitch(PgKillStore(db.conn), revoker or Revoker(), audit)


def one(db, q, *a):
    return db.conn.execute(q, a).fetchone()[0]


def wait_until(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


def approved_proposal(db, marketplace="GUMROAD"):
    c = db.conn
    contact = c.execute("INSERT INTO pii.contact(email,name,quarantined) VALUES (%s,'T',false) RETURNING contact_id",
                        (f"u{uuid.uuid4().hex[:8]}@example.test",)).fetchone()[0]
    perm = c.execute("INSERT INTO pii.contact_permission(contact_id,channel,basis,captured_at,expires_at,source_url) "
                     "VALUES (%s,'EMAIL','consent', now() - interval '2 days', now() + interval '30 days', "
                     "'https://example.test/o') RETURNING id", (contact,)).fetchone()[0]
    lead = c.execute("INSERT INTO core.lead(source_type,signal_ref,signal_id,contact_id) "
                     "VALUES ('INBOUND',%s,%s,%s) RETURNING id", (f"sig_{uuid.uuid4().hex[:10]}", uuid.uuid4().hex * 2, contact)
                     ).fetchone()[0]
    return c.execute(
        "INSERT INTO core.action_proposal(lead_id,role,marketplace,channel,action_type,autonomy_tier,"
        "compliance_verdict,status,contact_permission_id,idempotency_key,audit_trace_id) "
        "VALUES (%s,'COPYWRITER',%s,'EMAIL','SEND_MESSAGE',1,'PASS','APPROVED',%s,gen_random_uuid(),gen_random_uuid()) "
        "RETURNING id", (lead, marketplace, perm)).fetchone()[0]


def send(db, pid):
    db.conn.execute("UPDATE core.action_proposal SET status='SENDING' WHERE id=%s", (pid,))


def test_KS1_kill_blocks_sends_emits_one_event_and_revive_restores(db):
    rev = Revoker()
    s = ks(db, rev)
    pid = approved_proposal(db)
    res = s.kill("GUMROAD", "ops-1", "INCIDENT")
    assert res.changed and res.fully_applied and rev.calls == ["GUMROAD"]
    with pytest.raises(errors.Error) as e:
        send(db, pid)
    assert e.value.sqlstate == "CP003"
    assert one(db, "SELECT count(*) FROM core.outbox_event WHERE event_type='adapter.killed' "
                   "AND payload->>'scope'='GUMROAD'") == 1
    s.revive("GUMROAD", "ops-1")
    send(db, pid)
    assert one(db, "SELECT status FROM core.action_proposal WHERE id=%s", pid) == "SENDING"


def test_KS2_queues_halt_and_resume_within_five_seconds(db):
    done, killed_events, lock = [], [], threading.Lock()
    stop = threading.Event()
    errs = []

    def loop():
        try:
            with psycopg.connect(db.dsn, autocommit=True) as conn:
                w = Worker(PgOutboxStore(conn), "w1")
                w.subscribe(EventType.LEAD_SCORED, "scorer", lambda c, e: (lock.acquire(), done.append(e.event_id), lock.release()))
                w.subscribe(EventType.ADAPTER_KILLED, "ops", lambda c, e: (lock.acquire(), killed_events.append(e.event_id), lock.release()))
                run_loop(w, stop, poll_interval=0.1)
        except Exception as e:  # noqa: BLE001
            errs.append(e)

    t = threading.Thread(target=loop)
    t.start()
    try:
        for _ in range(3):
            publish(db.conn, EventType.LEAD_SCORED, marketplace="GUMROAD")
        assert wait_until(lambda: len(done) == 3), "baseline events were not processed"
        s = ks(db)
        s.kill(GLOBAL, "ops-1", "INCIDENT")                       # the kill has committed from here on
        for _ in range(5):
            publish(db.conn, EventType.LEAD_SCORED, marketplace="GUMROAD")
        assert wait_until(lambda: len(killed_events) == 1), "the adapter.killed event must still flow"
        time.sleep(1.5)                                           # many polls
        assert len(done) == 3                                     # nothing new was processed while killed
        assert one(db, "SELECT count(*) FROM core.outbox_event WHERE event_type='lead.scored' "
                       "AND status='PENDING' AND attempts=0") == 5   # held, not failed, no attempt consumed
        s.revive(GLOBAL, "ops-1")
        assert wait_until(lambda: len(done) == 8), "queues must resume within 5 seconds of revive"
    finally:
        stop.set()
        t.join(10)
    assert not errs, errs


def test_KS3_global_kill_revokes_every_marketplace_in_the_database(db):
    rev = Revoker()
    ks(db, rev).kill(GLOBAL, "ops-1", "INCIDENT")
    expected = PgKillStore(db.conn).marketplaces()
    assert len(expected) >= 10 and sorted(rev.calls) == sorted(expected)


def test_KS4_failed_revocation_keeps_the_kill_and_a_retry_adds_no_second_event(db):
    rev = Revoker(fail={"GUMROAD"})
    s = ks(db, rev)
    res = s.kill("GUMROAD", "ops-1", "INCIDENT")
    assert res.failed_revocations == (("GUMROAD", "RUNTIME_ERROR"),)
    assert one(db, "SELECT core.is_killed('GUMROAD')") is True
    rev.fail.clear()
    again = s.kill("GUMROAD", "ops-2", "INCIDENT")
    assert again.changed is False and again.fully_applied and rev.calls == ["GUMROAD", "GUMROAD"]
    assert one(db, "SELECT count(*) FROM core.outbox_event WHERE event_type='adapter.killed'") == 1
    assert one(db, "SELECT killed_by FROM core.kill_switch WHERE marketplace='GUMROAD'") == "ops-1"


def test_KS5_audit_hook_gets_ids_and_codes_only(db):
    seen = []
    ks(db, audit=lambda a, d: seen.append((a, d))).kill("ETSY", "ops-1", "INCIDENT")
    (action, details), = seen
    assert action == "KILL" and set(details) == {"scope", "actor", "reason_code", "changed", "failed_revocations"}
    assert "@" not in repr(details)


def test_KS6_status_lists_global_and_every_marketplace(db):
    s = ks(db)
    s.kill("PAYHIP", "ops-1", "INCIDENT")
    states = dict(s.status())
    assert states[GLOBAL] is False and states["PAYHIP"] is True
    assert len(states) == 1 + len(PgKillStore(db.conn).marketplaces())


def test_KS7_unknown_scope_changes_nothing(db):
    with pytest.raises(ValueError):
        ks(db).kill("NOPE", "ops-1", "INCIDENT")
    assert one(db, "SELECT count(*) FROM core.kill_switch WHERE killed") == 0
    assert one(db, "SELECT count(*) FROM core.outbox_event") == 0

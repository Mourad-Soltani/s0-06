"""Postgres tests for the outbox bus: PgOutboxStore, publish, and Worker against the real schema (0001+0002).
Names are B* so they cannot collide with the CI gate's required prefixes or the mutation step's -k T10."""
import threading
import uuid

import psycopg
import pytest
from psycopg import errors

from shared.bus.events import EventType, event_id_for
from shared.bus.publisher import publish
from shared.bus.store import PgOutboxStore
from shared.bus.worker import RunResult, Worker


@pytest.fixture()
def bdb(db):
    db.conn.execute("CREATE TABLE public.bus_test_effects (consumer text NOT NULL, event_id uuid NOT NULL, "
                    "UNIQUE (consumer, event_id))")
    return db


def recorder(name):
    return lambda conn, ev: conn.execute(
        "INSERT INTO public.bus_test_effects (consumer, event_id) VALUES (%s, %s)", (name, ev.event_id))


def effects(db, name=None):
    q, a = "SELECT count(*) FROM public.bus_test_effects", ()
    if name:
        q, a = q + " WHERE consumer = %s", (name,)
    return db.conn.execute(q, a).fetchone()[0]


def mk(db, worker_id="w1", **kw):
    return Worker(PgOutboxStore(db.conn), worker_id, **kw)


def row(db, q, *a):
    return db.conn.execute(q, a).fetchone()


def test_B1_publish_is_transactional(bdb):
    conn = bdb.conn
    with pytest.raises(RuntimeError):
        with conn.transaction():
            publish(conn, EventType.SIGNAL_CAPTURED)
            raise RuntimeError("producer failed after publishing")
    assert row(bdb, "SELECT count(*) FROM core.outbox_event")[0] == 0      # rolled back with the producer
    with conn.transaction():
        publish(conn, EventType.SIGNAL_CAPTURED)
    assert row(bdb, "SELECT count(*) FROM core.outbox_event")[0] == 1


def test_B2_publish_is_idempotent_by_event_id(bdb):
    eid = event_id_for(EventType.SIGNAL_CAPTURED, "sig_1")
    assert publish(bdb.conn, EventType.SIGNAL_CAPTURED, event_id=eid, payload={"v": 1}) == eid
    assert publish(bdb.conn, EventType.SIGNAL_CAPTURED, event_id=eid, payload={"v": 2}) == eid
    assert row(bdb, "SELECT count(*), max(payload->>'v') FROM core.outbox_event") == (1, "1")  # first row wins


def test_B3_publish_validation(bdb):
    with pytest.raises(ValueError):
        publish(bdb.conn, "not.an_event")
    with pytest.raises(errors.CheckViolation):
        publish(bdb.conn, EventType.SIGNAL_CAPTURED, payload={"contact": "a@b.co"})  # DB blocks email-shaped values
    assert row(bdb, "SELECT count(*) FROM core.outbox_event")[0] == 0


def test_B4_delivers_with_all_fields_and_acks(bdb):
    lead = bdb.conn.execute(
        "INSERT INTO core.lead(source_type,signal_ref,signal_id) VALUES ('INBOUND','sig_b4',%s) RETURNING id",
        ("a" * 64,)).fetchone()[0]
    trace = uuid.uuid4()
    eid = publish(bdb.conn, EventType.LEAD_SCORED, marketplace="GUMROAD", lead_id=lead,
                  payload={"score": 71}, trace_id=trace)
    seen = []
    w = mk(bdb)
    w.subscribe(EventType.LEAD_SCORED, "qualifier", lambda c, e: seen.append(e))
    assert w.run_once() == RunResult(claimed=1, done=1)
    (ev,) = seen
    assert (ev.event_id, ev.event_type, ev.marketplace, ev.lead_id, ev.trace_id, ev.attempts) == \
           (eid, "lead.scored", "GUMROAD", lead, trace, 1)
    assert dict(ev.payload) == {"score": 71} and ev.payload_ref is None
    assert row(bdb, "SELECT status FROM core.outbox_event WHERE event_id=%s", eid)[0] == "DONE"
    assert row(bdb, "SELECT count(*) FROM core.consumer_receipt WHERE consumer='qualifier' AND event_id=%s", eid)[0] == 1


def test_B5_redelivery_after_a_lost_ack_has_one_effect(bdb):
    eid = publish(bdb.conn, EventType.SIGNAL_CAPTURED)
    w = mk(bdb)
    w.subscribe(EventType.SIGNAL_CAPTURED, "a", recorder("a"))
    w.run_once()
    assert effects(bdb) == 1
    bdb.conn.execute("UPDATE core.outbox_event SET status='PENDING', claimed_by=NULL, claimed_until=NULL "
                     "WHERE event_id=%s", (eid,))                       # as if the ack never landed
    assert w.run_once() == RunResult(claimed=1, done=1)
    assert effects(bdb) == 1                                           # receipt blocked the second run
    assert row(bdb, "SELECT status FROM core.outbox_event WHERE event_id=%s", eid)[0] == "DONE"


def test_B6_partial_failure_reruns_only_the_failed_consumer(bdb):
    publish(bdb.conn, EventType.SIGNAL_CAPTURED)
    broken = {"b": True}

    def b(conn, ev):
        if broken["b"]:
            raise RuntimeError("boom")
        recorder("b")(conn, ev)

    w = mk(bdb, backoff_seconds=0)
    w.subscribe(EventType.SIGNAL_CAPTURED, "a", recorder("a"))
    w.subscribe(EventType.SIGNAL_CAPTURED, "b", b)
    assert w.run_once() == RunResult(claimed=1, retried=1)
    assert (effects(bdb, "a"), effects(bdb, "b")) == (1, 0)
    broken["b"] = False
    assert w.run_once() == RunResult(claimed=1, done=1)
    assert (effects(bdb, "a"), effects(bdb, "b")) == (1, 1)           # a ran once in total


def test_B7_handler_failure_rolls_back_and_stores_only_a_code(bdb):
    eid = publish(bdb.conn, EventType.SIGNAL_CAPTURED)

    def bad(conn, ev):
        recorder("a")(conn, ev)
        raise RuntimeError("leak@example.test")

    w = mk(bdb)                                                        # default 30s backoff
    w.subscribe(EventType.SIGNAL_CAPTURED, "a", bad)
    assert w.run_once() == RunResult(claimed=1, retried=1)
    assert effects(bdb) == 0                                           # handler write rolled back
    assert row(bdb, "SELECT count(*) FROM core.consumer_receipt")[0] == 0   # and so was the receipt
    st, code, att, later = row(bdb, "SELECT status, last_error_code, attempts, available_at > now() "
                                    "FROM core.outbox_event WHERE event_id=%s", eid)
    assert (st, code, att, later) == ("PENDING", "RUNTIME_ERROR", 1, True)


def test_B8_dead_letters_after_max_attempts(bdb):
    eid = publish(bdb.conn, EventType.SIGNAL_CAPTURED)
    w = mk(bdb, max_attempts=2, backoff_seconds=0)
    w.subscribe(EventType.SIGNAL_CAPTURED, "a", lambda c, e: 1 / 0)
    assert w.run_once() == RunResult(claimed=1, retried=1)
    assert w.run_once() == RunResult(claimed=1, dead=1)
    assert w.run_once() == RunResult()                                 # nothing left to claim
    assert row(bdb, "SELECT status, last_error_code FROM core.outbox_event WHERE event_id=%s", eid) == \
           ("DEAD", "ZERO_DIVISION_ERROR")


def test_B9_unrouted_events_are_acked(bdb):
    eid = publish(bdb.conn, EventType.LEAD_SCORED)
    assert mk(bdb).run_once() == RunResult(claimed=1, done=1, unrouted=1)
    assert row(bdb, "SELECT status FROM core.outbox_event WHERE event_id=%s", eid)[0] == "DONE"


def test_B10_two_concurrent_workers_process_each_event_exactly_once(bdb):
    n = 24
    for i in range(n):
        publish(bdb.conn, EventType.SIGNAL_CAPTURED, event_id=event_id_for(EventType.SIGNAL_CAPTURED, i))
    errs = []

    def run(name):
        try:
            with psycopg.connect(bdb.dsn, autocommit=True) as conn:
                w = Worker(PgOutboxStore(conn), name, limit=3)
                w.subscribe(EventType.SIGNAL_CAPTURED, "a", recorder("a"))
                w.drain()
        except Exception as e:  # noqa: BLE001
            errs.append(e)

    threads = [threading.Thread(target=run, args=(f"w{i}",)) for i in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errs, errs                                              # receipts + UNIQUE both guard duplicates
    assert effects(bdb) == n
    assert row(bdb, "SELECT count(*) FROM core.outbox_event WHERE status='DONE'")[0] == n


def test_B11_a_crashed_claimants_events_are_recovered(bdb):
    eid = publish(bdb.conn, EventType.SIGNAL_CAPTURED)
    bdb.conn.execute("SELECT * FROM core.claim_outbox_events('crashed', 10, interval '-1 second')")  # never acked
    w = mk(bdb)
    w.subscribe(EventType.SIGNAL_CAPTURED, "a", recorder("a"))
    assert w.run_once() == RunResult(claimed=1, done=1)
    assert effects(bdb) == 1
    assert row(bdb, "SELECT attempts FROM core.outbox_event WHERE event_id=%s", eid)[0] == 2


def test_B12_reap_dead_letters_exhausted_crashed_claims(bdb):
    eid = publish(bdb.conn, EventType.SIGNAL_CAPTURED)
    bdb.conn.execute("SELECT * FROM core.claim_outbox_events('crashed', 10, interval '-1 second', 1)")
    assert mk(bdb, max_attempts=1).reap() == 1
    assert row(bdb, "SELECT status, last_error_code FROM core.outbox_event WHERE event_id=%s", eid) == \
           ("DEAD", "LEASE_EXPIRED")

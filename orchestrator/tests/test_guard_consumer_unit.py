"""The Compliance Guard as a bus consumer, with a fake connection. No database. Names GC*."""
import uuid
from decimal import Decimal

import pytest

from orchestrator.guard import ComplianceGuard, GuardError, GuardResult
from orchestrator.guard_consumer import CONSUMER, GuardConsumer
from orchestrator.guard_rules import PASS, GuardConfig
from shared.bus.events import Event, EventType

PID, LEAD = uuid.uuid4(), uuid.uuid4()
GOOD = "Subject: Hello\n\nHi https://shop.example.test/l/1"


class Cur:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class Tx:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        pass

    def __exit__(self, t, v, tb):
        self.conn.events.append("rollback" if t else "commit")
        return False


class Conn:
    def __init__(self, *cursors):
        self.queue, self.calls, self.events = list(cursors), [], []

    def execute(self, sql, params=()):
        self.calls.append((" ".join(sql.split()), params))
        return self.queue.pop(0) if self.queue else Cur()

    def transaction(self):
        return Tx(self)


class Guard:
    def evaluate(self, facts, action):
        return GuardResult(PASS, ())


def row(status="PROPOSED"):
    return (LEAD, "EMAIL", "SEND_MESSAGE", 1, 0.9, status, None, {"context": "buyer_initiated", "body": GOOD}, [])


def event(payload):
    return Event(id=1, event_id=uuid.uuid4(), event_type=EventType.ACTION_PROPOSED, marketplace="EMAIL", lead_id=LEAD,
                 payload_ref=None, payload=payload, trace_id=LEAD, attempts=1)


def consumer(audit_factory=None, pubs=None):
    return GuardConsumer(Guard(), audit_factory=audit_factory, publish=lambda c, et, **kw: (pubs if pubs is not None else []).append(et))


def test_GC1_an_action_proposed_event_triggers_one_evaluation():
    conn, pubs, built = Conn(Cur(row())), [], []
    consumer(lambda c: built.append(c) or None, pubs).handle(conn, event({"proposal_id": str(PID), "tier": 0}))
    sqls = [s for s, _ in conn.calls]
    assert any("FOR UPDATE OF p" in s for s in sqls) and any("compliance_verdict = %s::core.verdict" in s for s in sqls)
    assert built == [conn] and pubs == [EventType.COMPLIANCE_PASSED]                    # the audit factory got the worker's connection


def test_GC2_a_proposal_that_is_no_longer_pending_is_acknowledged_quietly():
    conn, pubs = Conn(Cur(row("APPROVED"))), []
    assert consumer(None, pubs).handle(conn, event({"proposal_id": str(PID)})) is None
    assert pubs == [] and not any("UPDATE core.action_proposal" in s for s, _ in conn.calls)


def test_GC3_a_missing_proposal_raises_so_the_event_is_retried_then_dead_lettered():
    with pytest.raises(GuardError) as e:
        consumer(None).handle(Conn(Cur(None)), event({"proposal_id": str(PID)}))
    assert e.value.code == "NOT_FOUND"


@pytest.mark.parametrize("payload", [{}, {"proposal_id": "garbage"}, {"proposal_id": None}, None, {"proposal_id": 5}])
def test_GC4_a_malformed_event_raises_and_touches_nothing(payload):
    conn = Conn()
    with pytest.raises((ValueError, AttributeError)):
        consumer(None).handle(conn, event(payload))
    assert conn.calls == []


def test_GC5_the_consumer_registers_for_action_proposed():
    class W:
        subs = []

        def subscribe(self, etype, name, handler):
            self.subs.append((etype, name, handler))

    c, w = consumer(None), W()
    c.register(w)
    assert w.subs == [(EventType.ACTION_PROPOSED, CONSUMER, c.handle)] and CONSUMER == "compliance_guard"

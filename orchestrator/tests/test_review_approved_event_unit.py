"""Approving publishes action.approved (the Closer's trigger) inside the decision's transaction. No database. Names RE*."""
import uuid

import pytest

from orchestrator.review_service import ReviewError, ReviewQueue
from shared.bus.events import KNOWN_EVENT_TYPES, EventType, event_id_for

PID, LEAD = uuid.uuid4(), uuid.uuid4()


class Cur:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class Tx:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        self.conn.log.append("begin")

    def __exit__(self, t, v, tb):
        self.conn.log.append("rollback" if t else "commit")
        return False


class Conn:
    def __init__(self, log, *cursors):
        self.queue, self.log = list(cursors), log

    def execute(self, sql, params=()):
        self.log.append("sql")
        return self.queue.pop(0) if self.queue else Cur()

    def transaction(self):
        return Tx(self)


def make(*cursors, publish=True, audit=True):
    log, pubs = [], []
    q = ReviewQueue(Conn(log, *cursors), audit=(lambda a, d: log.append("audit")) if audit else None,
                    publish=(lambda c, et, **kw: (log.append("publish"), pubs.append((et, kw)))) if publish else None)
    return q, log, pubs


def test_RE1_the_event_type_is_known_and_valid_for_the_database():
    import re
    assert EventType.ACTION_APPROVED == "action.approved" and "action.approved" in KNOWN_EVENT_TYPES
    assert re.match(r"^[a-z][a-z_]*\.[a-z][a-z_]*$", "action.approved")


def test_RE2_approving_publishes_the_event_after_the_audit_and_before_the_commit():
    q, log, pubs = make(Cur((LEAD, "EMAIL", 1, "PASS")), Cur())
    q.approve(PID, "alice")
    assert log == ["begin", "sql", "sql", "audit", "publish", "commit"]
    (etype, kw), = pubs
    assert etype == EventType.ACTION_APPROVED and kw["event_id"] == event_id_for(EventType.ACTION_APPROVED, PID)
    assert kw["payload"] == {"proposal_id": str(PID), "tier": 1} and kw["marketplace"] == "EMAIL" and kw["lead_id"] == LEAD and kw["trace_id"] == LEAD


def test_RE3_without_a_publisher_nothing_is_published_so_existing_callers_are_unchanged():
    q, log, pubs = make(Cur((LEAD, "EMAIL", 1, "PASS")), Cur(), publish=False)
    q.approve(PID, "alice")
    assert pubs == [] and "publish" not in log


def test_RE4_a_failing_publish_undoes_the_approval():
    def broken(conn, etype, **kw):
        raise RuntimeError("bus down")

    log = []
    q = ReviewQueue(Conn(log, Cur((LEAD, "EMAIL", 1, "PASS")), Cur()), publish=broken)
    with pytest.raises(RuntimeError):
        q.approve(PID, "alice")
    assert log[-1] == "rollback"


def test_RE5_only_an_approval_publishes_and_a_refused_one_publishes_nothing():
    q, log, pubs = make(Cur(None), Cur(("PROPOSED", "BLOCK")))
    with pytest.raises(ReviewError):
        q.approve(PID, "alice")
    assert pubs == []
    q2, log2, pubs2 = make(Cur((LEAD, "EMAIL", 0, "BLOCK")), Cur())
    q2.reject(PID, "alice", "TONE_OFF")
    assert pubs2 == []


def test_RE6_the_event_carries_ids_and_a_tier_only():
    q, log, pubs = make(Cur((LEAD, "EMAIL", 0, "PASS")), Cur())
    q.approve(PID, "alice")
    assert set(pubs[0][1]["payload"]) == {"proposal_id", "tier"} and "@" not in str(pubs[0][1])

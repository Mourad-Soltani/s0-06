"""ReviewQueue logic with a fake connection and a spy audit hook: no database. Names RS*."""
import sys
import types
import uuid
from datetime import datetime, timedelta, timezone

import pytest

try:
    import psycopg  # noqa: F401
except ImportError:                                        # the sandbox has no driver; CI does
    mod = types.ModuleType("psycopg.types.json")
    mod.Json = lambda v: ("JSON", v)
    for name in ("psycopg", "psycopg.types"):
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules["psycopg.types.json"] = mod

from orchestrator.review_service import (EDIT_UNAVAILABLE, NOT_APPROVABLE, NOT_FOUND, NOT_PENDING, MAX_PAYLOAD_BYTES,
                                         ReviewError, ReviewQueue, ShadowStats, review_audit_hook)

PID, LEAD, REF, NEWREF = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


class Cur:
    def __init__(self, row=None, rows=None):
        self.row, self.rows = row, rows or []

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class Tx:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        self.conn.events.append("begin")

    def __exit__(self, t, v, tb):
        self.conn.events.append("rollback" if t else "commit")
        return False


class Conn:
    def __init__(self, *cursors):
        self.queue, self.calls, self.events = list(cursors), [], []

    def execute(self, sql, params=()):
        self.calls.append((" ".join(sql.split()), params))
        self.events.append("sql")
        return self.queue.pop(0) if self.queue else Cur()

    def transaction(self):
        return Tx(self)


def queue(conn, audit=None, hasher=None):
    return ReviewQueue(conn, audit=audit, hasher=hasher)


def spy():
    seen = []
    return seen, lambda action, d: seen.append((action, d))


# ------------------------------------------------------------------ validation happens before the database
@pytest.mark.parametrize("reviewer", ["", "a@b.co", "two words", None, "x" * 129])
def test_RS1_reviewers_must_be_opaque_ids(reviewer):
    for call in (lambda: queue(None).approve(PID, reviewer), lambda: queue(None).reject(PID, reviewer, "TONE"),
                 lambda: queue(None).edit(PID, reviewer, {"body": "x"})):
        with pytest.raises(ValueError):
            call()


@pytest.mark.parametrize("reason", ["", "tone", "has space", "1TONE", None, "X" * 65])
def test_RS1b_reasons_must_be_codes(reason):
    with pytest.raises(ValueError):
        queue(None).reject(PID, "alice", reason)


@pytest.mark.parametrize("payload", [{}, [], None, "text", 5, {"k": "x" * (MAX_PAYLOAD_BYTES + 1)}])
def test_RS1c_edits_need_a_reasonable_object(payload):
    with pytest.raises(ValueError):
        queue(None, hasher=lambda lead, p: b"x").edit(PID, "alice", payload)


@pytest.mark.parametrize("kw", [dict(limit=0), dict(limit=201), dict(limit=True), dict(limit="5"), dict(marketplace="etsy"),
                                dict(marketplace="a b")])
def test_RS1d_listing_arguments_are_validated(kw):
    with pytest.raises(ValueError):
        queue(None).list_pending(**kw)


# ------------------------------------------------------------------ listing and reading
def test_RS2_listing_maps_rows_and_filters_in_sql():
    created = NOW - timedelta(hours=1)
    row = (PID, LEAD, "COPYWRITER", "ETSY", "EMAIL", "SEND_MESSAGE", 1, __import__("decimal").Decimal("0.85"), "PASS", created, REF)
    conn = Conn(Cur(rows=[row]))
    (a,) = queue(conn).list_pending(limit=10, marketplace="ETSY")
    assert (a.proposal_id, a.lead_id, a.marketplace, a.autonomy_tier, a.confidence, a.compliance_verdict, a.payload_ref) == \
           (PID, LEAD, "ETSY", 1, 0.85, "PASS", REF)
    sql, params = conn.calls[0]
    assert "status = 'PROPOSED'" in sql and "ORDER BY created_at, id" in sql and params == ("ETSY", "ETSY", 10)
    d = a.as_dict()
    assert d["proposal_id"] == str(PID) and d["created_at"] == created.isoformat() and "payload" not in d


def test_RS2b_get_returns_a_json_ready_dict_or_none():
    created = NOW
    row = (PID, LEAD, "COPYWRITER", "ETSY", "EMAIL", "SEND_MESSAGE", 1, None, "PASS", "PROPOSED", created, REF, {"body": "hi"})
    got = queue(Conn(Cur(row))).get(PID)
    assert got["proposal_id"] == str(PID) and got["payload"] == {"body": "hi"} and got["status"] == "PROPOSED"
    assert queue(Conn(Cur(None))).get(PID) is None


# ------------------------------------------------------------------ approving
def test_RS3_approve_updates_records_the_decision_and_audits_inside_one_transaction():
    conn = Conn(Cur((LEAD, "ETSY", 1, "PASS")), Cur())
    seen, hook = spy()
    queue(conn, hook).approve(PID, "alice")
    (sql1, p1), (sql2, p2) = conn.calls
    assert "status = 'APPROVED'" in sql1 and "status = 'PROPOSED'" in sql1 and "compliance_verdict = 'PASS'" in sql1
    assert p1 == (PID,) and "INSERT INTO core.review_decision" in sql2 and "'APPROVE'" in sql2 and p2 == (PID, "alice")
    assert seen == [("REVIEW_APPROVED", {"actor": "alice", "lead_id": LEAD, "marketplace": "ETSY", "tier": 1,
                                         "verdict": "PASS", "outcome": "OK"})]
    assert conn.events == ["begin", "sql", "sql", "commit"]


@pytest.mark.parametrize("state,code", [(None, NOT_FOUND), (("APPROVED", "PASS"), NOT_PENDING), (("REJECTED", "BLOCK"), NOT_PENDING),
                                        (("PROPOSED", "BLOCK"), NOT_APPROVABLE), (("PROPOSED", "ESCALATE"), NOT_APPROVABLE),
                                        (("PROPOSED", None), NOT_APPROVABLE)])
def test_RS4_a_proposal_that_cannot_be_approved_says_why_and_changes_nothing(state, code):
    conn = Conn(Cur(None), Cur(state))
    seen, hook = spy()
    with pytest.raises(ReviewError) as e:
        queue(conn, hook).approve(PID, "alice")
    assert e.value.code == code and seen == [] and conn.events[-1] == "rollback"
    assert not any("review_decision" in sql for sql, _ in conn.calls)


# ------------------------------------------------------------------ rejecting
def test_RS5_reject_records_the_reason_and_audits_it_even_for_a_blocked_proposal():
    conn = Conn(Cur((LEAD, "ETSY", 0, "BLOCK")), Cur())
    seen, hook = spy()
    queue(conn, hook).reject(PID, "bob", "TONE_OFF")
    (sql1, _), (sql2, p2) = conn.calls
    assert "status = 'REJECTED'" in sql1 and "compliance_verdict" in sql1 and "compliance_verdict = 'PASS'" not in sql1
    assert "'REJECT'" in sql2 and p2 == (PID, "TONE_OFF", "bob")
    assert seen == [("REVIEW_REJECTED", {"actor": "bob", "lead_id": LEAD, "marketplace": "ETSY", "tier": 0,
                                         "verdict": "BLOCK", "outcome": "TONE_OFF"})]


@pytest.mark.parametrize("state,code", [(None, NOT_FOUND), (("APPROVED", "PASS"), NOT_PENDING)])
def test_RS6_reject_refusals(state, code):
    conn = Conn(Cur(None), Cur(state))
    with pytest.raises(ReviewError) as e:
        queue(conn).reject(PID, "bob", "TONE_OFF")
    assert e.value.code == code and conn.events[-1] == "rollback"


def test_RS7_a_failed_audit_undoes_the_decision():
    def broken(action, d):
        raise RuntimeError("audit down")

    conn = Conn(Cur((LEAD, "ETSY", 1, "PASS")), Cur())
    with pytest.raises(RuntimeError):
        queue(conn, broken).approve(PID, "alice")
    assert conn.events[-1] == "rollback"                                  # no decision without its audit event


# ------------------------------------------------------------------ editing
def test_RS8_edit_swaps_the_body_rehashes_clears_the_verdict_and_audits():
    hashed = []
    hasher = lambda lead, payload: hashed.append((lead, payload)) or b"\x01" * 32  # noqa: E731
    conn = Conn(Cur((LEAD, REF, "PROPOSED", "ETSY", 1)), Cur((NEWREF,)), Cur(), Cur(), Cur())
    seen, hook = spy()
    patched = {"body": "better text"}
    queue(conn, hook, hasher).edit(PID, "alice", patched)
    sqls = [s for s, _ in conn.calls]
    assert "FOR UPDATE" in sqls[0] and "INSERT INTO pii.payload_store" in sqls[1]
    assert "UPDATE core.action_proposal" in sqls[2] and "compliance_verdict = NULL" in sqls[2] and "status" not in sqls[2].split("SET")[1].split("WHERE")[0]
    assert conn.calls[2][1] == (NEWREF, b"\x01" * 32, PID)
    assert "DELETE FROM pii.payload_store" in sqls[3] and conn.calls[3][1] == (REF,)
    assert "INSERT INTO core.review_edit" in sqls[4] and conn.calls[4][1] == (PID, "alice")
    assert hashed == [(LEAD, patched)]
    assert seen == [("REVIEW_EDITED", {"actor": "alice", "lead_id": LEAD, "marketplace": "ETSY", "tier": 1,
                                       "verdict": None, "outcome": "OK"})]
    assert conn.events[-1] == "commit"


def test_RS8b_an_edit_with_no_previous_body_has_nothing_to_delete():
    conn = Conn(Cur((LEAD, None, "PROPOSED", "ETSY", 1)), Cur((NEWREF,)), Cur(), Cur())
    queue(conn, None, lambda lead, p: b"x" * 32).edit(PID, "alice", {"body": "x"})
    assert not any("DELETE" in s for s, _ in conn.calls) and len(conn.calls) == 4


@pytest.mark.parametrize("row,code", [(None, NOT_FOUND), ((LEAD, REF, "APPROVED", "ETSY", 1), NOT_PENDING),
                                      ((LEAD, REF, "REJECTED", "ETSY", 1), NOT_PENDING)])
def test_RS9_edits_need_a_pending_proposal(row, code):
    conn = Conn(Cur(row))
    with pytest.raises(ReviewError) as e:
        queue(conn, None, lambda lead, p: b"x" * 32).edit(PID, "alice", {"body": "x"})
    assert e.value.code == code and conn.events[-1] == "rollback" and len(conn.calls) == 1


def test_RS9b_editing_needs_a_hasher_and_refuses_before_touching_the_database():
    conn = Conn()
    with pytest.raises(ReviewError) as e:
        queue(conn).edit(PID, "alice", {"body": "x"})
    assert e.value.code == EDIT_UNAVAILABLE and conn.calls == [] and conn.events == []


# ------------------------------------------------------------------ shadow-mode statistics (spec 9.1)
def stats(approved=85, rejected=15, edited=10, proposals=100, blocked=2, days=15):
    return ShadowStats(approved, rejected, edited, proposals, blocked, NOW - timedelta(days=days))


def test_RS10_rates_and_the_tier_1_gate_boundaries():
    s = stats()
    assert (s.decided, s.approval_rate, s.edit_rate, s.block_rate) == (100, 0.85, 0.10, 0.02) and s.passes_tier1_gate(NOW)
    assert stats(approved=80, rejected=20).passes_tier1_gate(NOW)                        # exactly 80% passes
    assert not stats(approved=79, rejected=21).passes_tier1_gate(NOW)
    assert stats(blocked=4).passes_tier1_gate(NOW) and not stats(blocked=5).passes_tier1_gate(NOW)   # strictly under 5%
    assert not stats(proposals=99).passes_tier1_gate(NOW)                                # fewer than 100 actions
    assert not stats(days=13).passes_tier1_gate(NOW) and stats(days=14).passes_tier1_gate(NOW)
    assert not stats(approved=0, rejected=0).passes_tier1_gate(NOW)                       # nothing decided yet
    empty = ShadowStats(0, 0, 0, 0, 0, None)
    assert (empty.approval_rate, empty.edit_rate, empty.block_rate, empty.days_observed(NOW)) == (None, None, None, 0.0)
    assert not empty.passes_tier1_gate(NOW)


def test_RS11_shadow_stats_query_one_marketplace_role_and_tier():
    first = NOW - timedelta(days=20)
    conn = Conn(Cur((85, 15)), Cur((10,)), Cur((100, 2, first)))
    s = queue(conn).shadow_stats("ETSY", "COPYWRITER")
    assert s == ShadowStats(85, 15, 10, 100, 2, first)
    assert all(p == ("ETSY", "COPYWRITER", 0) for _, p in conn.calls)                    # tier 0 by default
    assert "autonomy_tier" in conn.calls[0][0] and "count(DISTINCT e.proposal_id)" in conn.calls[1][0]
    conn2 = Conn(Cur((0, 0)), Cur((0,)), Cur((0, 0, None)))
    queue(conn2).shadow_stats("ETSY", "COPYWRITER", tier=1)
    assert conn2.calls[0][1] == ("ETSY", "COPYWRITER", 1)


def test_RS12_the_audit_hook_passes_ids_and_codes_to_the_writer():
    calls = []

    class W:
        def record(self, actor, action, **kw):
            calls.append((actor, action, kw))

    review_audit_hook(W())("REVIEW_APPROVED", {"actor": "alice", "lead_id": LEAD, "marketplace": "ETSY", "tier": 1,
                                                "verdict": "PASS", "outcome": "OK"})
    assert calls == [("alice", "REVIEW_APPROVED", {"marketplace": "ETSY", "lead_id": LEAD, "compliance_verdict": "PASS",
                                                    "autonomy_tier": 1, "outcome": "OK"})]

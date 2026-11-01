"""ProposalWriter and tier assignment with a fake connection. No database. Names PW*."""
import sys
import types
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

try:
    import psycopg  # noqa: F401
except ImportError:                                        # the sandbox has no driver; CI does
    class _Json:                                           # same shape as psycopg's: the wrapped value is .obj
        def __init__(self, obj):
            self.obj = obj

    mod = types.ModuleType("psycopg.types.json")
    mod.Json = _Json
    for name in ("psycopg", "psycopg.types"):
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules["psycopg.types.json"] = mod

from orchestrator.copy_schema import DraftBrief, ProductFacts
from orchestrator.copywriter import CopyResult
from orchestrator.proposals import ProposalWriter, assign_tier, copy_audit_hook, idempotency_key
from orchestrator.review_service import ShadowStats
from shared.bus.events import EventType, event_id_for

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
LEAD, PERM, REF, PID = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
PASSING = ShadowStats(85, 15, 10, 100, 2, NOW - timedelta(days=15))
FAILING = ShadowStats(0, 0, 0, 0, 0, None)


def brief(**kw):
    p = ProductFacts("gr-1", "Budget Planner", 1900, "USD", "https://shop.example.test/l/gr-1")
    base = dict(lead_id=LEAD, marketplace="EMAIL", channel="EMAIL", context="consented_outreach", product=p, signal_kind="SALE",
                contact_permission_id=PERM)
    base.update(kw)
    return DraftBrief(**base)


class Cur:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class Tx:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        self.conn.events.append("begin")

    def __exit__(self, t, v, tb):
        self.conn.events.append("rollback" if t else "commit")
        return False


class Conn:
    def __init__(self, log, *cursors):
        self.queue, self.calls, self.events, self.log = list(cursors), [], log, log

    def execute(self, sql, params=()):
        self.calls.append((" ".join(sql.split()), params))
        self.events.append("sql")
        return self.queue.pop(0) if self.queue else Cur()

    def transaction(self):
        return Tx(self)


class Writer:
    def __init__(self, log, result=None, boom=False):
        self.log, self.result, self.boom, self.calls = log, result or CopyResult("Hello", "Hi there", 0.8, "LLM", "m"), boom, []

    def draft(self, b, *, trace_id=None):
        self.log.append("draft")
        self.calls.append((b, trace_id))
        if self.boom:
            raise AssertionError("the model must not be called for an existing proposal")
        return self.result


def make(*cursors, stats=FAILING, result=None, audit=None, boom=False):
    log = []
    conn = Conn(log, *cursors)
    pubs = []
    w = ProposalWriter(conn, Writer(log, result, boom), hasher=lambda lead, payload: b"\x07" * 32, stats_provider=lambda mp: stats,
                       audit=audit, publish=lambda c, et, **kw: pubs.append((et, kw)), clock=lambda: NOW)
    return w, conn, pubs, log


def test_PW1_a_new_proposal_is_written_audited_and_announced_in_one_transaction():
    seen = []
    w, conn, pubs, log = make(Cur(None), Cur((REF,)), Cur((PID,)), audit=lambda a, d: seen.append((a, d)))
    got = w.create(brief(discount_pct=Decimal("20.0"), offer_code="SAVE"))
    assert got == (PID, True)
    assert log == ["sql", "draft", "begin", "sql", "sql", "commit"]               # lookup, then the model, THEN the transaction
    (s1, _), (s2, p2), (s3, p3) = conn.calls
    assert "SELECT id FROM core.action_proposal WHERE idempotency_key" in s1 and "INSERT INTO pii.payload_store" in s2
    payload = p2[1].obj
    assert payload == {"context": "consented_outreach", "body": "Subject: Hello\n\nHi there", "source": "LLM",
                       "product_external_id": "gr-1", "discount_pct": "20", "offer_code": "SAVE", "consent_ref": str(PERM)}
    assert "ON CONFLICT (idempotency_key) DO NOTHING" in s3 and "'COPYWRITER'" in s3 and "'SEND_MESSAGE'" in s3
    assert p3 == (LEAD, "EMAIL", "EMAIL", REF, b"\x07" * 32, 0, 0.8, PERM, idempotency_key(brief(discount_pct=Decimal("20.0"), offer_code="SAVE")), LEAD)
    assert seen == [("COPY_DRAFTED", {"marketplace": "EMAIL", "lead_id": LEAD, "tier": 0, "outcome": "OK"})]
    (etype, kw), = pubs
    assert etype == EventType.ACTION_PROPOSED and kw["payload"] == {"proposal_id": str(PID), "tier": 0}
    assert kw["event_id"] == event_id_for(EventType.ACTION_PROPOSED, PID) and kw["trace_id"] == LEAD and kw["lead_id"] == LEAD


def test_PW2_the_tier_follows_the_shadow_gate_and_the_audit_names_a_fallback():
    seen = []
    w, conn, pubs, _ = make(Cur(None), Cur((REF,)), Cur((PID,)), stats=PASSING, audit=lambda a, d: seen.append(d),
                            result=CopyResult("S", "B", 0.6, "TEMPLATE", None, "BUDGET_EXHAUSTED"))
    w.create(brief())
    assert conn.calls[2][1][5] == 1 and pubs[0][1]["payload"]["tier"] == 1
    assert seen[0]["outcome"] == "FALLBACK_BUDGET_EXHAUSTED" and conn.calls[1][1][1].obj["source"] == "TEMPLATE"


def test_PW3_the_proposal_key_is_stable_and_specific():
    a, b = brief(), brief()
    assert idempotency_key(a) == idempotency_key(b)
    other_product = brief(product=ProductFacts("gr-2", "Other", 100, "USD", "https://shop.example.test/l/gr-2"))
    assert len({idempotency_key(x) for x in (a, other_product, brief(context="buyer_initiated", contact_permission_id=None),
                                              brief(lead_id=uuid.uuid4()))}) == 4


def test_PW4_an_existing_proposal_is_returned_without_calling_the_model():
    w, conn, pubs, log = make(Cur((PID,)), boom=True)
    assert w.create(brief()) == (PID, False)
    assert log == ["sql"] and len(conn.calls) == 1 and pubs == []                  # one lookup, no model call, no transaction


def test_PW5_losing_a_race_keeps_the_other_proposal_and_drops_our_body():
    seen = []
    w, conn, pubs, log = make(Cur(None), Cur((REF,)), Cur(None), Cur(), Cur((PID,)), audit=lambda a, d: seen.append(a))
    assert w.create(brief()) == (PID, False)
    assert "DELETE FROM pii.payload_store" in conn.calls[3][0] and conn.calls[3][1] == (REF,)
    assert seen == [] and pubs == [] and log[-1] == "commit"


def test_PW6_a_failing_audit_rolls_back_the_proposal_and_publishes_nothing():
    def broken(a, d):
        raise RuntimeError("audit down")

    w, conn, pubs, log = make(Cur(None), Cur((REF,)), Cur((PID,)), audit=broken)
    with pytest.raises(RuntimeError):
        w.create(brief())
    assert log[-1] == "rollback" and pubs == []


def test_PW7_buyer_threads_carry_no_consent_reference_and_no_email_is_ever_stored():
    w, conn, _, _ = make(Cur(None), Cur((REF,)), Cur((PID,)))
    w.create(brief(context="buyer_initiated", contact_permission_id=None))
    payload = conn.calls[1][1][1].obj
    assert "consent_ref" not in payload and payload["discount_pct"] is None and "@" not in str(payload)


def test_PW8_tier_assignment_and_the_audit_hook():
    assert assign_tier(PASSING, NOW) == 1 and assign_tier(FAILING, NOW) == 0 and assign_tier(None, NOW) == 0
    calls = []

    class W:
        def record(self, actor, action, **kw):
            calls.append((actor, action, kw))

    copy_audit_hook(W())("COPY_DRAFTED", {"marketplace": "EMAIL", "lead_id": LEAD, "tier": 0, "outcome": "OK"})
    assert calls == [("copywriter", "COPY_DRAFTED", {"marketplace": "EMAIL", "lead_id": LEAD, "autonomy_tier": 0, "outcome": "OK"})]

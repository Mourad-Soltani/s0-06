"""ComplianceGuard (rules + adapter preflight) and GuardService with a fake connection. No database. Names CS*."""
import uuid
from decimal import Decimal

import pytest

from adapters.email_channel.adapter import EmailChannelAdapter
from adapters.email_channel.fake_transport import FakeEmailTransport
from adapters.email_channel.message import EmailConfig
from orchestrator.guard import (NOT_FOUND, NOT_PENDING, ComplianceGuard, GuardError, GuardResult, GuardService, guard_audit_hook)
from orchestrator.guard_rules import BLOCK, ESCALATE, PASS, Finding, GuardConfig
from shared.bus.events import EventType
from shared.models import ActionType, ComplianceVerdict, Marketplace, ProposedAction, Verdict

CFG = GuardConfig(allowed_link_hosts=frozenset({"shop.example.test"}), max_discount_pct=Decimal("20"))
GOOD = "Subject: Your guide is ready\n\nHi,\n\nHere it is: https://shop.example.test/l/gr-1\n\nThanks!"
PID, LEAD, PERM = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


class FakeAdapter:
    def __init__(self, verdict=Verdict.PASS, codes=()):
        self.verdict, self.codes, self.seen = verdict, tuple(codes), []

    def preflight_check(self, action):
        self.seen.append(action)
        return ComplianceVerdict(verdict=self.verdict, reason_codes=self.codes)


def facts_for(guard, body=GOOD, **kw):
    from orchestrator.guard_rules import DraftFacts
    base = dict(context="consented_outreach", has_current_permission=True)
    base.update(kw)
    return DraftFacts(body=body, **base)


def action(payload=None):
    return ProposedAction(action_type=ActionType.SEND_MESSAGE, marketplace=Marketplace.EMAIL,
                          payload=payload if payload is not None else {"context": "consented_outreach"})


# ------------------------------------------------------------------ ComplianceGuard
def test_CS1_a_clean_draft_with_a_passing_adapter_passes():
    adapter = FakeAdapter()
    g = ComplianceGuard({"EMAIL": adapter}, CFG)
    r = g.evaluate(facts_for(g), action())
    assert r == GuardResult(PASS, ()) and r.codes == () and adapter.seen[0].marketplace is Marketplace.EMAIL


def test_CS2_adapter_blocks_and_escalations_become_findings_with_the_right_severity():
    g = ComplianceGuard({"EMAIL": FakeAdapter(Verdict.BLOCK, ["MESSAGING_UNSUPPORTED", "CONTEXT_NOT_ALLOWED"])}, CFG)
    r = g.evaluate(facts_for(g), action())
    assert r.verdict == BLOCK and r.findings == (Finding("CONTEXT_NOT_ALLOWED", BLOCK), Finding("MESSAGING_UNSUPPORTED", BLOCK))
    g2 = ComplianceGuard({"EMAIL": FakeAdapter(Verdict.ESCALATE, ["NEEDS_LOOK"])}, CFG)
    assert g2.evaluate(facts_for(g2), action()) == GuardResult(ESCALATE, (Finding("NEEDS_LOOK", ESCALATE),))


def test_CS3_no_adapter_for_the_marketplace_is_a_block_not_a_pass():
    g = ComplianceGuard({}, CFG)
    r = g.evaluate(facts_for(g), action())
    assert r == GuardResult(BLOCK, (Finding("NO_ADAPTER", BLOCK),))


def test_CS4_rule_findings_and_adapter_findings_are_merged_without_duplicates():
    g = ComplianceGuard({"EMAIL": FakeAdapter(Verdict.BLOCK, ["INVALID_MESSAGE_FORMAT"])}, CFG)
    r = g.evaluate(facts_for(g, body="no subject line"), action())
    assert r.codes.count("INVALID_MESSAGE_FORMAT") == 1 and r.verdict == BLOCK


def test_CS5_the_real_email_adapter_enforces_the_consent_reference_through_the_guard():
    adapter = EmailChannelAdapter(FakeEmailTransport(), EmailConfig("1 Example Street, Paris 75001", "https://example.test/unsub"))
    g = ComplianceGuard({"EMAIL": adapter}, CFG)
    bare = g.evaluate(facts_for(g), action({"context": "consented_outreach", "body": GOOD}))
    assert bare.verdict == BLOCK and "MISSING_CONSENT_REF" in bare.codes
    ok = g.evaluate(facts_for(g), action({"context": "consented_outreach", "consent_ref": str(PERM), "body": GOOD}))
    assert ok == GuardResult(PASS, ())
    cold = g.evaluate(facts_for(g), action({"context": "cold_outreach", "consent_ref": str(PERM), "body": GOOD}))
    assert "CONTEXT_NOT_ALLOWED" in cold.codes and adapter.transport.calls == []        # preflight never reaches the provider


# ------------------------------------------------------------------ GuardService with a fake connection
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
    def __init__(self, *cursors):
        self.queue, self.calls, self.events = list(cursors), [], []

    def execute(self, sql, params=()):
        self.calls.append((" ".join(sql.split()), params))
        self.events.append("sql")
        return self.queue.pop(0) if self.queue else Cur()

    def transaction(self):
        return Tx(self)


def row(payload=None, status="PROPOSED", permission=PERM, confidence=0.9, flags=("CONSENT_RISK",), tier=1):
    payload = {"context": "consented_outreach", "body": GOOD} if payload is None else payload
    return (LEAD, "EMAIL", "SEND_MESSAGE", tier, confidence, status, permission, payload, flags)


class SpyGuard:
    def __init__(self, result=GuardResult(PASS, ())):
        self.result, self.calls = result, []

    def evaluate(self, facts, act):
        self.calls.append((facts, act))
        return self.result


def service(conn, guard=None, audit=None, publish=None):
    pubs = []
    svc = GuardService(conn, guard or SpyGuard(), audit=audit, publish=publish or (lambda c, et, **kw: pubs.append((et, kw))))
    return svc, pubs


def test_CS6_a_pass_updates_the_verdict_audits_and_publishes_in_one_transaction():
    conn = Conn(Cur(row()), Cur((1,)))
    seen = []
    svc, pubs = service(conn, audit=lambda a, d: seen.append((a, d)))
    r = svc.evaluate(PID)
    assert r.verdict == PASS and conn.events == ["begin", "sql", "sql", "sql", "commit"]
    sqls = [s for s, _ in conn.calls]
    assert "FOR UPDATE OF p" in sqls[0] and "cp.withdrawn_at IS NULL" in sqls[1] and "NOT c.quarantined" in sqls[1]
    assert "compliance_verdict = %s::core.verdict" in sqls[2] and conn.calls[2][1] == ("PASS", PID)
    assert seen == [("COMPLIANCE_EVALUATED", {"marketplace": "EMAIL", "lead_id": LEAD, "verdict": "PASS", "tier": 1, "outcome": "OK"})]
    (etype, kw), = pubs
    assert etype == EventType.COMPLIANCE_PASSED and kw["payload"] == {"proposal_id": str(PID), "tier": 1}
    assert kw["marketplace"] == "EMAIL" and kw["lead_id"] == LEAD and kw["trace_id"] == LEAD and isinstance(kw["event_id"], uuid.UUID)


def test_CS7_a_block_records_every_finding_under_one_run_and_publishes_nothing():
    result = GuardResult(BLOCK, (Finding("DISCOUNT_ABOVE_CAP", BLOCK), Finding("URGENCY_PRESSURE", ESCALATE)))
    conn = Conn(Cur(row()), Cur((1,)))
    seen = []
    svc, pubs = service(conn, SpyGuard(result), audit=lambda a, d: seen.append((a, d)))
    assert svc.evaluate(PID) == result and pubs == []
    inserts = [p for s, p in conn.calls if "INSERT INTO core.compliance_finding" in s]
    assert [(i[0], i[2], i[3]) for i in inserts] == [(PID, "DISCOUNT_ABOVE_CAP", "BLOCK"), (PID, "URGENCY_PRESSURE", "ESCALATE")]
    assert inserts[0][1] == inserts[1][1] and isinstance(inserts[0][1], uuid.UUID)         # one run id
    assert seen[0][1]["verdict"] == "BLOCK" and seen[0][1]["outcome"] == "DISCOUNT_ABOVE_CAP"


def test_CS8_an_escalation_is_recorded_and_not_published():
    svc, pubs = service(Conn(Cur(row()), Cur((1,))), SpyGuard(GuardResult(ESCALATE, (Finding("URGENCY_PRESSURE", ESCALATE),))))
    assert svc.evaluate(PID).verdict == ESCALATE and pubs == []


@pytest.mark.parametrize("found,code", [(None, NOT_FOUND), (row(status="APPROVED"), NOT_PENDING), (row(status="REJECTED"), NOT_PENDING)])
def test_CS9_only_a_pending_proposal_is_evaluated(found, code):
    conn = Conn(Cur(found))
    svc, pubs = service(conn)
    with pytest.raises(GuardError) as e:
        svc.evaluate(PID)
    assert e.value.code == code and conn.events == ["begin", "sql", "rollback"] and pubs == []


def test_CS10_a_failing_audit_undoes_the_verdict_and_publishes_nothing():
    def broken(a, d):
        raise RuntimeError("audit down")

    conn = Conn(Cur(row()), Cur((1,)))
    svc, pubs = service(conn, audit=broken)
    with pytest.raises(RuntimeError):
        svc.evaluate(PID)
    assert conn.events[-1] == "rollback" and pubs == []


def test_CS11_the_facts_come_from_the_proposal_the_lead_and_the_live_permission():
    spy = SpyGuard()
    payload = {"context": "consented_outreach", "body": GOOD, "discount_pct": "10", "execution_plan": ["browser_automation"]}
    conn = Conn(Cur(row(payload, confidence=0.4, flags=("TOS_RISK",))), Cur((1,)))
    service(conn, spy)[0].evaluate(PID)
    (facts, act), = spy.calls
    assert facts.context == "consented_outreach" and facts.body == GOOD and facts.has_current_permission is True
    assert facts.discount_pct == Decimal("10") and facts.confidence == 0.4 and facts.risk_flags == ("TOS_RISK",)
    assert act.action_type is ActionType.SEND_MESSAGE and act.marketplace is Marketplace.EMAIL and act.payload == payload
    assert act.execution_plan == ("browser_automation",)


def test_CS12_no_permission_means_no_permission_query_and_no_live_consent():
    spy = SpyGuard()
    conn = Conn(Cur(row(permission=None)))
    service(conn, spy)[0].evaluate(PID)
    assert spy.calls[0][0].has_current_permission is False and not any("FROM pii.contact_permission" in s for s, _ in conn.calls)
    spy2, conn2 = SpyGuard(), Conn(Cur(row()), Cur(None))                       # permission row not live (withdrawn, expired, quarantined)
    service(conn2, spy2)[0].evaluate(PID)
    assert spy2.calls[0][0].has_current_permission is False


@pytest.mark.parametrize("raw,expected", [(None, None), (10, Decimal("10")), ("12.5", Decimal("12.5")), (True, Decimal(-1)),
                                          ("ten", Decimal(-1)), ([1], Decimal(-1))])
def test_CS13_a_malformed_discount_becomes_an_invalid_one_never_a_missing_one(raw, expected):
    spy = SpyGuard()
    payload = {"context": "consented_outreach", "body": GOOD, "discount_pct": raw}
    service(Conn(Cur(row(payload)), Cur((1,))), spy)[0].evaluate(PID)
    assert spy.calls[0][0].discount_pct == expected


def test_CS14_a_proposal_without_a_readable_body_is_evaluated_as_an_empty_draft():
    spy = SpyGuard()
    service(Conn(Cur(row(payload={}))), spy)[0].evaluate(PID)
    f = spy.calls[0][0]
    assert f.body is None and f.context == "" and f.discount_pct is None
    real = ComplianceGuard({"EMAIL": FakeAdapter()}, CFG)
    assert "MISSING_BODY" in real.evaluate(f, action({})).codes


def test_CS15_the_audit_hook_names_the_guard_as_the_actor_and_passes_ids_and_codes_only():
    calls = []

    class W:
        def record(self, actor, action, **kw):
            calls.append((actor, action, kw))

    guard_audit_hook(W())("COMPLIANCE_EVALUATED", {"marketplace": "EMAIL", "lead_id": LEAD, "verdict": "BLOCK", "tier": 1, "outcome": "NO_ADAPTER"})
    assert calls == [("compliance-guard", "COMPLIANCE_EVALUATED", {"marketplace": "EMAIL", "lead_id": LEAD, "compliance_verdict": "BLOCK",
                                                                   "autonomy_tier": 1, "outcome": "NO_ADAPTER"})]

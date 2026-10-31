"""Compliance Guard against Postgres, with the real email adapter, consent, review and the audit trail. Needs
TEST_ADMIN_DSN. Names CV*."""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from psycopg import errors
from psycopg.types.json import Json

from adapters.email_channel.adapter import EmailChannelAdapter
from adapters.email_channel.fake_transport import FakeEmailTransport
from adapters.email_channel.message import EmailConfig
from orchestrator.guard import ComplianceGuard, GuardError, GuardService, guard_audit_hook
from orchestrator.guard_rules import GuardConfig
from orchestrator.review_service import NOT_APPROVABLE, ReviewError, ReviewQueue, review_audit_hook
from shared.audit import AuditWriter
from shared.consent import ConsentService

CFG = GuardConfig(allowed_link_hosts=frozenset({"shop.example.test"}), max_discount_pct=Decimal("20"))
GOOD = "Subject: Your guide is ready\n\nHi,\n\nHere it is: https://shop.example.test/l/gr-1\n\nThanks!"
EMAIL_CFG = EmailConfig("1 Example Street, Paris 75001, France", "https://example.test/unsubscribe")


def one(db, q, *a):
    return db.conn.execute(q, a).fetchone()[0]


def service(db, audit=True, config=CFG):
    adapter = EmailChannelAdapter(FakeEmailTransport(), EMAIL_CFG)
    audit_fn = guard_audit_hook(AuditWriter(db.conn)) if audit is True else audit
    return GuardService(db.conn, ComplianceGuard({"EMAIL": adapter}, config), audit=audit_fn)


def consent(db, email="jane.doe@example.test", **kw):
    perm = ConsentService(db.conn).capture(email, "EMAIL", "https://example.test/optin", **kw)
    return one(db, "SELECT contact_id FROM pii.contact_permission WHERE id=%s", perm), perm


def draft(db, body=GOOD, *, permission="auto", discount=None, confidence=0.9, flags=None, status="PROPOSED", context="consented_outreach"):
    contact, perm = (None, None)
    if permission == "auto":
        contact, perm = consent(db, f"c{uuid.uuid4().hex[:8]}@example.test")
    elif permission is not None:
        contact, perm = permission
    lead = one(db, "INSERT INTO core.lead(source_type,signal_ref,signal_id,contact_id) VALUES ('INBOUND',%s,%s,%s) RETURNING id",
               f"sig_{uuid.uuid4().hex[:10]}", uuid.uuid4().hex * 2, contact)
    if flags:
        db.conn.execute("UPDATE core.lead SET risk_flags=%s::text[] WHERE id=%s", (flags, lead))
    payload = {"context": context, "body": body, "discount_pct": discount}
    if perm:
        payload["consent_ref"] = str(perm)
    ref = one(db, "INSERT INTO pii.payload_store(lead_id, body) VALUES (%s,%s) RETURNING payload_id", lead, Json(payload))
    pid = one(db, "INSERT INTO core.action_proposal(lead_id,role,marketplace,channel,action_type,payload_ref,autonomy_tier,confidence,"
                  "status,contact_permission_id,idempotency_key,audit_trace_id) VALUES (%s,'COPYWRITER','EMAIL','EMAIL','SEND_MESSAGE',%s,1,%s,"
                  "%s::core.proposal_status,%s,gen_random_uuid(),gen_random_uuid()) RETURNING id", lead, ref, confidence, status, perm)
    return pid, lead


def verdict_of(db, pid):
    return one(db, "SELECT compliance_verdict::text FROM core.action_proposal WHERE id=%s", pid)


def findings(db, pid):
    return db.conn.execute("SELECT rule_code, severity, run_id FROM core.compliance_finding WHERE proposal_id=%s ORDER BY id", (pid,)).fetchall()


def passed_events(db):
    return db.conn.execute("SELECT payload, lead_id, trace_id FROM core.outbox_event WHERE event_type='compliance.passed'").fetchall()


def audit_rows(db):
    return db.conn.execute("SELECT action_type, actor, marketplace::text, lead_id, compliance_verdict, autonomy_tier, outcome "
                           "FROM audit.event WHERE action_type='COMPLIANCE_EVALUATED' ORDER BY seq").fetchall()


def test_CV1_a_clean_draft_passes_is_audited_and_announced(db):
    pid, lead = draft(db)
    r = service(db).evaluate(pid)
    assert (r.verdict, r.codes) == ("PASS", ()) and verdict_of(db, pid) == "PASS" and findings(db, pid) == []
    assert audit_rows(db) == [("COMPLIANCE_EVALUATED", "compliance-guard", "EMAIL", lead, "PASS", 1, "OK")]
    assert passed_events(db) == [({"proposal_id": str(pid), "tier": 1}, lead, lead)]


@pytest.mark.parametrize("body,kw,expected", [
    (GOOD, dict(permission=None), {"NO_CURRENT_PERMISSION", "MISSING_CONSENT_REF"}),
    (GOOD.replace("shop.example.test", "evil.test"), {}, {"LINK_HOST_NOT_ALLOWED"}),
    (GOOD, dict(discount=50), {"DISCOUNT_ABOVE_CAP"}),
    (GOOD.replace("Hi,", "Hi {{name}},"), {}, {"UNRESOLVED_PLACEHOLDER"}),
    (GOOD.replace("Your guide is ready", "Re: your order"), {}, {"DECEPTIVE_SUBJECT"}),
    (GOOD + "\nGuaranteed income!", {}, {"PROHIBITED_CLAIM"})])
def test_CV2_blocked_drafts_record_their_findings_and_publish_nothing(db, body, kw, expected):
    pid, _ = draft(db, body, **kw)
    r = service(db).evaluate(pid)
    assert r.verdict == "BLOCK" and expected <= set(r.codes) and verdict_of(db, pid) == "BLOCK"
    stored = findings(db, pid)
    assert {c for c, _, _ in stored} == set(r.codes) and len({run for _, _, run in stored}) == 1
    assert passed_events(db) == []


def test_CV3_an_escalated_draft_waits_for_a_human_and_cannot_be_approved(db):
    pid, _ = draft(db, GOOD + "\nAct now!")
    r = service(db).evaluate(pid)
    assert r.verdict == "ESCALATE" and r.codes == ("URGENCY_PRESSURE",) and passed_events(db) == []
    with pytest.raises(ReviewError) as e:
        ReviewQueue(db.conn).approve(pid, "alice")
    assert e.value.code == NOT_APPROVABLE


def test_CV4_consent_must_be_live_in_the_database_not_just_present(db):
    withdrawn, expired, held = consent(db, "a@example.test"), consent(db, "b@example.test", captured_at=datetime.now(timezone.utc) - timedelta(days=3), ttl_days=1), consent(db, "c@example.test")
    ConsentService(db.conn).withdraw(withdrawn[0], "EMAIL")
    ConsentService(db.conn).quarantine(held[0])
    for permission in (withdrawn, expired, held):
        pid, _ = draft(db, permission=permission)
        assert "NO_CURRENT_PERMISSION" in service(db).evaluate(pid).codes and verdict_of(db, pid) == "BLOCK"


def test_CV5_confidence_and_risk_flags_come_from_the_proposal_and_the_lead(db):
    low, _ = draft(db, confidence=0.3)
    assert service(db).evaluate(low).codes == ("LOW_CONFIDENCE",)
    risky, _ = draft(db, flags=["TOS_RISK"])
    assert service(db).evaluate(risky).codes == ("RISK_FLAG_TOS",)
    ok, _ = draft(db, flags=["CONSENT_RISK"])
    assert service(db).evaluate(ok).verdict == "PASS"


def test_CV6_re_evaluating_after_an_edit_keeps_the_history_and_announces_the_new_pass(db):
    pid, lead = draft(db, GOOD.replace("shop.example.test", "evil.test"))
    assert service(db).evaluate(pid).verdict == "BLOCK"
    w = AuditWriter(db.conn)
    q = ReviewQueue(db.conn, audit=review_audit_hook(w), hasher=w.hash_payload)
    perm = one(db, "SELECT contact_permission_id FROM core.action_proposal WHERE id=%s", pid)
    q.edit(pid, "alice", {"context": "consented_outreach", "consent_ref": str(perm), "body": GOOD, "discount_pct": None})
    assert verdict_of(db, pid) is None                                          # the edit cleared the old verdict
    assert service(db).evaluate(pid).verdict == "PASS" and verdict_of(db, pid) == "PASS"
    stored = findings(db, pid)
    assert [(c, s) for c, s, _ in stored] == [("LINK_HOST_NOT_ALLOWED", "BLOCK")]   # the first run's finding is still there
    assert len(passed_events(db)) == 1 and len(audit_rows(db)) == 2
    q.approve(pid, "alice")                                                       # and now a reviewer can approve it
    assert one(db, "SELECT status::text FROM core.action_proposal WHERE id=%s", pid) == "APPROVED"


def test_CV7_evaluating_twice_announces_each_pass_once_per_run(db):
    pid, _ = draft(db)
    service(db).evaluate(pid)
    service(db).evaluate(pid)
    assert len(passed_events(db)) == 2 and one(db, "SELECT count(DISTINCT event_id) FROM core.outbox_event WHERE event_type='compliance.passed'") == 2


def test_CV8_only_a_pending_proposal_is_evaluated(db):
    done, _ = draft(db, status="APPROVED")
    with pytest.raises(GuardError) as e:
        service(db).evaluate(done)
    assert e.value.code == "NOT_PENDING" and verdict_of(db, done) is None
    with pytest.raises(GuardError) as e:
        service(db).evaluate(uuid.uuid4())
    assert e.value.code == "NOT_FOUND" and findings(db, done) == [] and audit_rows(db) == []


def test_CV9_a_failing_audit_leaves_no_verdict_no_findings_and_no_event(db):
    def broken(action, details):
        raise RuntimeError("audit down")

    pid, _ = draft(db, GOOD.replace("shop.example.test", "evil.test"))
    with pytest.raises(RuntimeError):
        service(db, audit=broken).evaluate(pid)
    assert verdict_of(db, pid) is None and findings(db, pid) == [] and passed_events(db) == []
    ok, _ = draft(db)
    with pytest.raises(RuntimeError):
        service(db, audit=broken).evaluate(ok)
    assert verdict_of(db, ok) is None and passed_events(db) == []


def test_CV10_the_review_role_can_evaluate_and_the_app_role_cannot_read_bodies(db):
    a, _ = draft(db)
    db.conn.execute("SET ROLE review_svc")
    try:
        assert service(db).evaluate(a).verdict == "PASS"
    finally:
        db.conn.execute("RESET ROLE")
    b, _ = draft(db)
    db.conn.execute("SET ROLE app_rw")
    try:
        with pytest.raises(errors.InsufficientPrivilege):
            service(db).evaluate(b)
    finally:
        db.conn.execute("RESET ROLE")
    assert verdict_of(db, b) is None                                               # fail closed: no verdict was written


def test_CV11_findings_are_append_only_for_the_application_role(db):
    pid, _ = draft(db, GOOD.replace("shop.example.test", "evil.test"))
    service(db).evaluate(pid)
    db.conn.execute("SET ROLE app_rw")
    try:
        for sql in ("UPDATE core.compliance_finding SET severity='ESCALATE'", "DELETE FROM core.compliance_finding"):
            with pytest.raises(errors.InsufficientPrivilege):
                db.conn.execute(sql)
    finally:
        db.conn.execute("RESET ROLE")
    assert len(findings(db, pid)) == 1

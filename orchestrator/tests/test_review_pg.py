"""Review queue, consent, audit and the send gate together on Postgres; and the HTTP API over a real queue.
Needs TEST_ADMIN_DSN. Names RV*."""
import hashlib
import io
import json
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from psycopg import errors
from psycopg.types.json import Json

from orchestrator.review_api import ReviewApi
from orchestrator.review_service import NOT_APPROVABLE, NOT_FOUND, NOT_PENDING, ReviewError, ReviewQueue, review_audit_hook
from shared.audit import AuditWriter, canonical_bytes
from shared.consent import ConsentService

EMAIL = "jane.doe@example.test"


def one(db, q, *a):
    return db.conn.execute(q, a).fetchone()[0]


def queue(db, audit=True):
    w = AuditWriter(db.conn)
    return ReviewQueue(db.conn, audit=review_audit_hook(w) if audit is True else audit, hasher=w.hash_payload)


def new_lead(db, contact=None):
    return one(db, "INSERT INTO core.lead(source_type,signal_ref,signal_id,contact_id) VALUES ('INBOUND',%s,%s,%s) RETURNING id",
               f"sig_{uuid.uuid4().hex[:10]}", uuid.uuid4().hex * 2, contact)


def proposal(db, *, tier=1, verdict="PASS", status="PROPOSED", marketplace="ETSY", channel=None, role="COPYWRITER", body=None, lead=None,
             permission=None, days_ago=None):
    lead = lead or new_lead(db)
    channel = channel or marketplace
    ref = one(db, "INSERT INTO pii.payload_store(lead_id, body) VALUES (%s,%s) RETURNING payload_id", lead, Json(body)) if body is not None else None
    pid = one(db, "INSERT INTO core.action_proposal(lead_id,role,marketplace,channel,action_type,payload_ref,autonomy_tier,"
                  "compliance_verdict,status,contact_permission_id,idempotency_key,audit_trace_id) "
                  "VALUES (%s,%s::core.agent_role,%s::core.marketplace,%s::core.marketplace,'SEND_MESSAGE',%s,%s,%s::core.verdict,"
                  "%s::core.proposal_status,%s,gen_random_uuid(),gen_random_uuid()) RETURNING id",
              lead, role, marketplace, channel, ref, tier, verdict, status, permission)
    if days_ago:
        db.conn.execute("UPDATE core.action_proposal SET created_at = now() - make_interval(days => %s) WHERE id=%s", (days_ago, pid))
    return pid


def status_of(db, pid):
    return one(db, "SELECT status::text FROM core.action_proposal WHERE id=%s", pid)


def audit_rows(db):
    return db.conn.execute("SELECT action_type, actor, marketplace::text, lead_id, compliance_verdict, autonomy_tier, outcome "
                           "FROM audit.event WHERE action_type LIKE 'REVIEW_%' ORDER BY seq").fetchall()


def decisions(db):
    return db.conn.execute("SELECT proposal_id, decision, reason_code, reviewer FROM core.review_decision ORDER BY id").fetchall()


def test_RV1_listing_returns_only_pending_proposals_oldest_first_without_bodies(db):
    a = proposal(db, days_ago=3)
    proposal(db, status="APPROVED")
    c = proposal(db, marketplace="GUMROAD", days_ago=1)
    proposal(db, status="REJECTED")
    q = queue(db)
    assert [x.proposal_id for x in q.list_pending()] == [a, c]
    assert [x.proposal_id for x in q.list_pending(marketplace="GUMROAD")] == [c]
    assert [x.proposal_id for x in q.list_pending(limit=1)] == [a]
    assert "payload" not in q.list_pending()[0].as_dict()


def test_RV2_approving_records_the_decision_and_audits_it_once(db):
    lead = new_lead(db)
    pid = proposal(db, lead=lead)
    queue(db).approve(pid, "alice")
    assert status_of(db, pid) == "APPROVED" and decisions(db) == [(pid, "APPROVE", None, "alice")]
    assert audit_rows(db) == [("REVIEW_APPROVED", "alice", "ETSY", lead, "PASS", 1, "OK")]
    with pytest.raises(ReviewError) as e:
        queue(db).approve(pid, "alice")
    assert e.value.code == NOT_PENDING and len(decisions(db)) == 1 and len(audit_rows(db)) == 1


@pytest.mark.parametrize("verdict", ["BLOCK", "ESCALATE", None])
def test_RV3_only_a_passed_proposal_can_be_approved(db, verdict):
    pid = proposal(db, verdict=verdict)
    with pytest.raises(ReviewError) as e:
        queue(db).approve(pid, "alice")
    assert e.value.code == NOT_APPROVABLE and status_of(db, pid) == "PROPOSED"
    assert decisions(db) == [] and audit_rows(db) == []


def test_RV3b_an_unknown_proposal_is_not_found(db):
    with pytest.raises(ReviewError) as e:
        queue(db).approve(uuid.uuid4(), "alice")
    assert e.value.code == NOT_FOUND and decisions(db) == []


def test_RV4_rejecting_records_the_reason_even_for_a_blocked_proposal(db):
    lead = new_lead(db)
    pid = proposal(db, lead=lead, verdict="BLOCK")
    queue(db).reject(pid, "bob", "TONE_OFF")
    assert status_of(db, pid) == "REJECTED" and decisions(db) == [(pid, "REJECT", "TONE_OFF", "bob")]
    assert audit_rows(db) == [("REVIEW_REJECTED", "bob", "ETSY", lead, "BLOCK", 1, "TONE_OFF")]
    other = proposal(db)
    with pytest.raises(ValueError):
        queue(db).reject(other, "bob", "not a code")
    assert status_of(db, other) == "PROPOSED" and len(decisions(db)) == 1


def test_RV5_a_failing_audit_undoes_the_decision(db):
    def broken(action, details):
        raise RuntimeError("audit down")

    pid = proposal(db)
    with pytest.raises(RuntimeError):
        queue(db, audit=broken).approve(pid, "alice")
    assert status_of(db, pid) == "PROPOSED" and decisions(db) == []


def test_RV6_an_edit_replaces_the_body_rehashes_it_and_clears_the_verdict(db):
    lead = new_lead(db)
    pid = proposal(db, lead=lead, body={"subject": "s", "body": "old text"})
    old_ref = one(db, "SELECT payload_ref FROM core.action_proposal WHERE id=%s", pid)
    patched = {"subject": "s", "body": "better text"}
    queue(db).edit(pid, "alice", patched)
    new_ref, verdict, status, phash = db.conn.execute(
        "SELECT payload_ref, compliance_verdict::text, status::text, payload_hash FROM core.action_proposal WHERE id=%s", (pid,)).fetchone()
    assert new_ref != old_ref and verdict is None and status == "PROPOSED"
    assert db.conn.execute("SELECT payload_id, body FROM pii.payload_store WHERE lead_id=%s", (lead,)).fetchall() == [(new_ref, patched)]
    salt = bytes(one(db, "SELECT salt FROM pii.lead_salt WHERE lead_id=%s", lead))
    assert bytes(phash) == hashlib.sha256(salt + b"payload\x00" + canonical_bytes(patched)).digest()
    assert one(db, "SELECT count(*) FROM core.review_edit WHERE proposal_id=%s AND reviewer='alice'", pid) == 1
    assert audit_rows(db) == [("REVIEW_EDITED", "alice", "ETSY", lead, None, 1, "OK")]
    with pytest.raises(ReviewError) as e:                                     # the Guard judged the OLD text
        queue(db).approve(pid, "alice")
    assert e.value.code == NOT_APPROVABLE
    queue(db).reject(pid, "alice", "NEEDS_REWRITE")                           # but a reviewer can still reject it
    assert status_of(db, pid) == "REJECTED"


def test_RV7_edit_refusals_change_nothing(db):
    lead = new_lead(db)
    done = proposal(db, lead=lead, status="APPROVED", body={"body": "x"})
    for pid, code in ((done, NOT_PENDING), (uuid.uuid4(), NOT_FOUND)):
        with pytest.raises(ReviewError) as e:
            queue(db).edit(pid, "alice", {"body": "y"})
        assert e.value.code == code
    open_one = proposal(db, body={"body": "x"})
    for bad in ({}, [], "text"):
        with pytest.raises(ValueError):
            queue(db).edit(open_one, "alice", bad)
    assert one(db, "SELECT count(*) FROM core.review_edit") == 0 and one(db, "SELECT count(*) FROM pii.payload_store") == 2


def test_RV8_get_returns_the_body_or_nothing(db):
    with_body, without = proposal(db, body={"subject": "s", "body": "hi"}), proposal(db)
    got = queue(db).get(with_body)
    assert got["payload"] == {"subject": "s", "body": "hi"} and got["status"] == "PROPOSED" and got["marketplace"] == "ETSY"
    assert queue(db).get(without)["payload"] is None and queue(db).get(uuid.uuid4()) is None


def test_RV9_shadow_stats_count_one_tier_and_drive_the_gate(db):
    q = queue(db)
    for i in range(100):
        proposal(db, tier=0, days_ago=15)
    pending = [r[0] for r in db.conn.execute("SELECT id FROM core.action_proposal ORDER BY id").fetchall()]
    for i, pid in enumerate(pending):
        (q.approve if i < 85 else lambda p, r: q.reject(p, r, "OFF_BRAND"))(pid, "alice")
    proposal(db, tier=1)                                                        # a different tier: not counted
    edited = proposal(db, tier=0, body={"body": "x"})
    q.edit(edited, "alice", {"body": "y"})
    s = q.shadow_stats("ETSY", "COPYWRITER")
    assert (s.approved, s.rejected, s.edited, s.proposals, s.blocked) == (85, 15, 1, 101, 0)
    assert s.passes_tier1_gate(datetime.now(timezone.utc))
    for _ in range(8):
        proposal(db, tier=0, verdict="BLOCK")
    assert not q.shadow_stats("ETSY", "COPYWRITER").passes_tier1_gate(datetime.now(timezone.utc))   # 8 of 109 blocked
    assert q.shadow_stats("GUMROAD", "COPYWRITER").proposals == 0


def test_RV10_review_consent_and_the_send_gate_work_as_one_chain(db):
    consent = ConsentService(db.conn)
    perm = consent.capture(EMAIL, "EMAIL", "https://example.test/optin")
    contact = one(db, "SELECT contact_id FROM pii.contact_permission WHERE id=%s", perm)
    ok = proposal(db, lead=new_lead(db, contact), permission=perm, channel="EMAIL")
    queue(db).approve(ok, "alice")
    db.conn.execute("UPDATE core.action_proposal SET status='SENDING' WHERE id=%s", (ok,))     # consent valid: the gate opens
    assert status_of(db, ok) == "SENDING"

    withdrawn = proposal(db, lead=new_lead(db, contact), permission=perm, channel="EMAIL")
    queue(db).approve(withdrawn, "alice")
    consent.withdraw(contact, "EMAIL")                                                          # consent withdrawn after approval
    with pytest.raises(errors.Error) as e:
        db.conn.execute("UPDATE core.action_proposal SET status='SENDING' WHERE id=%s", (withdrawn,))
    assert e.value.sqlstate == "CP002"

    draft = proposal(db, tier=0, lead=new_lead(db, contact), permission=perm, channel="EMAIL")
    queue(db).approve(draft, "alice")                                                           # approved for the shadow gate...
    with pytest.raises(errors.CheckViolation):                                                  # ...but a Tier 0 draft never sends
        db.conn.execute("UPDATE core.action_proposal SET status='SENDING' WHERE id=%s", (draft,))


def test_RV11_the_review_role_does_everything_and_the_app_role_cannot_read_bodies(db):
    a, b, c = (proposal(db, body={"body": f"text {i}"}) for i in range(3))
    db.conn.execute("SET ROLE review_svc")
    try:
        q = queue(db)
        assert len(q.list_pending()) == 3 and q.get(a)["payload"] == {"body": "text 0"}
        q.approve(a, "alice")
        q.reject(b, "alice", "OFF_BRAND")
        q.edit(c, "alice", {"body": "edited"})
        assert q.shadow_stats("ETSY", "COPYWRITER").approved == 0
    finally:
        db.conn.execute("RESET ROLE")
    assert [status_of(db, x) for x in (a, b, c)] == ["APPROVED", "REJECTED", "PROPOSED"]
    d, e_ = proposal(db, body={"body": "secret"}), proposal(db)
    db.conn.execute("SET ROLE app_rw")
    try:
        q = queue(db)
        assert len(q.list_pending()) == 3  # c still PROPOSED after edit, plus d and e_
        q.approve(e_, "alice")                                                                  # core tables are allowed
        with pytest.raises(errors.InsufficientPrivilege):                                       # message bodies are not
            q.get(d)
    finally:
        db.conn.execute("RESET ROLE")


def test_RV12_hash_payload_matches_the_database_formula_and_differs_per_lead(db):
    w, a, b = AuditWriter(db.conn), new_lead(db), new_lead(db)
    ha, hb = w.hash_payload(a, {"k": "same"}), w.hash_payload(b, {"k": "same"})
    salt = bytes(one(db, "SELECT salt FROM pii.lead_salt WHERE lead_id=%s", a))
    assert ha == hashlib.sha256(salt + b"payload\x00" + canonical_bytes({"k": "same"})).digest() and ha != hb
    db.conn.execute("SELECT core.erase_lead(%s,'ops-1')", (a,))
    with pytest.raises(errors.Error) as e:
        w.hash_payload(a, {"k": "same"})
    assert e.value.sqlstate == "AU002"


# ------------------------------------------------------------------ the HTTP API over a real queue
def call(app, method, path, body=None, token="alice-token-0123456789", query=""):
    data = json.dumps(body).encode() if body is not None else b""
    env = {"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": query, "wsgi.input": io.BytesIO(data),
           "CONTENT_LENGTH": str(len(data)), "CONTENT_TYPE": "application/json" if data else ""}
    if token:
        env["HTTP_AUTHORIZATION"] = f"Bearer {token}"
    out = {}
    chunks = app(env, lambda status, headers: out.update(status=int(status.split()[0])))
    return out["status"], json.loads(b"".join(chunks))


def test_RV13_the_http_api_lists_reads_approves_rejects_and_edits_and_audits_the_token_holder(db):
    @contextmanager
    def factory():
        yield queue(db)

    app = ReviewApi(factory, {"alice-token-0123456789": "alice", "bob-token-0123456789xx": "bob"})
    lead = new_lead(db)
    a = proposal(db, lead=lead, body={"subject": "s", "body": "hi"})
    b, c = proposal(db), proposal(db, body={"body": "x"})
    status, body = call(app, "GET", "/review/actions")
    assert status == 200 and {x["proposal_id"] for x in body["actions"]} == {str(a), str(b), str(c)}
    assert call(app, "GET", f"/review/actions/{a}")[1]["action"]["payload"] == {"subject": "s", "body": "hi"}
    assert call(app, "POST", f"/review/actions/{a}/approve")[0] == 200 and status_of(db, a) == "APPROVED"
    assert call(app, "POST", f"/review/actions/{b}/reject", {"reason_code": "TONE_OFF"}, token="bob-token-0123456789xx")[0] == 200
    status, body = call(app, "POST", f"/review/actions/{c}/edit", {"patched_payload": {"body": "y"}})
    assert status == 200 and body["compliance_verdict"] is None
    assert call(app, "POST", f"/review/actions/{a}/approve")[:1] == (409,)
    assert call(app, "POST", f"/review/actions/{uuid.uuid4()}/approve")[:1] == (404,)
    assert call(app, "GET", "/review/actions", token=None)[:1] == (401,) and call(app, "GET", "/review/actions", token="nope")[:1] == (401,)
    assert [(r[0], r[1]) for r in audit_rows(db)] == [("REVIEW_APPROVED", "alice"), ("REVIEW_REJECTED", "bob"), ("REVIEW_EDITED", "alice")]

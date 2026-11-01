"""The review detail with the lead and findings on Postgres, and its contract with the dashboard's parser. Needs
TEST_ADMIN_DSN. Names RDP*."""
import io
import json
import uuid
from contextlib import contextmanager

from psycopg.types.json import Json

from orchestrator.review_api import ReviewApi
from orchestrator.review_service import ReviewQueue
from shared.consent import ConsentService
from ui.view_model import detail_view

EMAIL = "jane.doe@example.test"
BODY = {"context": "consented_outreach", "body": "Subject: Your guide is ready\n\nHi,\n\nHere it is: https://shop.example.test/l/gr-1", "source": "TEMPLATE"}


def one(db, q, *a):
    return db.conn.execute(q, a).fetchone()[0]


def lead(db, contact=None, scored=True):
    lid = one(db, "INSERT INTO core.lead(source_type,signal_ref,signal_id,contact_id) VALUES ('INBOUND',%s,%s,%s) RETURNING id",
              f"sig_{uuid.uuid4().hex[:10]}", uuid.uuid4().hex * 2, contact)
    if scored:
        db.conn.execute("UPDATE core.lead SET icp_match_score=90, pain_intensity=50, authority_signal=50, timeline_signal=80, budget_signal=80, "
                        "composite_score=75, confidence=0.80, risk_flags=ARRAY['CONSENT_RISK'], product_external_id='gr-1', status='QUALIFIED' WHERE id=%s", (lid,))
    return lid


def proposal(db, lid, verdict="ESCALATE"):
    ref = one(db, "INSERT INTO pii.payload_store(lead_id, body) VALUES (%s,%s) RETURNING payload_id", lid, Json(BODY))
    return one(db, "INSERT INTO core.action_proposal(lead_id,role,marketplace,channel,action_type,payload_ref,autonomy_tier,confidence,compliance_verdict,"
                   "status,idempotency_key,audit_trace_id) VALUES (%s,'COPYWRITER','EMAIL','EMAIL','SEND_MESSAGE',%s,0,0.6,%s::core.verdict,'PROPOSED',"
                   "gen_random_uuid(),gen_random_uuid()) RETURNING id", lid, ref, verdict)


def finding(db, pid, run, code, severity, hours_ago=0):
    db.conn.execute("INSERT INTO core.compliance_finding(proposal_id, run_id, rule_code, severity, evaluated_at) VALUES (%s,%s,%s,%s, now() - make_interval(hours => %s))",
                    (pid, run, code, severity, hours_ago))


def test_RDP1_the_detail_returns_the_lead_scores_and_only_the_latest_runs_findings(db):
    lid = lead(db)
    pid = proposal(db, lid)
    old, new = uuid.uuid4(), uuid.uuid4()
    finding(db, pid, old, "LINK_HOST_NOT_ALLOWED", "BLOCK", hours_ago=1)
    finding(db, pid, new, "URGENCY_PRESSURE", "ESCALATE")
    got = ReviewQueue(db.conn).get(pid)
    assert got["lead"]["composite_score"] == 75 and got["lead"]["confidence"] == 0.8 and got["lead"]["risk_flags"] == ["CONSENT_RISK"]
    assert (got["lead"]["product_external_id"], got["lead"]["status"], got["lead"]["source_type"]) == ("gr-1", "QUALIFIED", "INBOUND")
    assert [(f["rule_code"], f["severity"]) for f in got["findings"]] == [("URGENCY_PRESSURE", "ESCALATE")]
    json.dumps(got)                                                              # everything is JSON-ready


def test_RDP2_an_unscored_lead_and_a_proposal_with_no_findings_still_serialise(db):
    pid = proposal(db, lead(db, scored=False), verdict=None)
    got = ReviewQueue(db.conn).get(pid)
    assert got["findings"] == [] and got["lead"]["composite_score"] is None and got["lead"]["status"] == "SIGNAL_CAPTURED"
    json.dumps(got)


def test_RDP3_what_the_server_sends_is_what_the_dashboard_parses(db):
    pid = proposal(db, lead(db))
    finding(db, pid, uuid.uuid4(), "URGENCY_PRESSURE", "ESCALATE")
    v = detail_view(ReviewQueue(db.conn).get(pid))
    assert v.score_headline == "75/100" and v.subject == "Your guide is ready" and v.verdict_label == "Needs your judgement"
    assert [f.label for f in v.findings] == ["It uses pressure language"] and v.risk_flags == ("No consent on record for this lead",)
    assert v.can_approve is False and "Product: gr-1" in v.lead_summary and v.source_label.startswith("Written from the standard template")


def test_RDP4_no_address_name_or_contact_id_reaches_the_detail(db):
    perm = ConsentService(db.conn).capture(EMAIL, "EMAIL", "https://example.test/optin", name="Jane Doe")
    contact = one(db, "SELECT contact_id FROM pii.contact_permission WHERE id=%s", perm)
    pid = proposal(db, lead(db, contact=contact))
    got = ReviewQueue(db.conn).get(pid)
    blob = json.dumps(got)
    assert "example.test/optin" not in blob and "jane" not in blob.lower() and str(contact) not in blob and "contact" not in got["lead"]


def test_RDP5_the_http_detail_endpoint_carries_the_lead_and_findings(db):
    pid = proposal(db, lead(db))
    finding(db, pid, uuid.uuid4(), "URGENCY_PRESSURE", "ESCALATE")

    @contextmanager
    def factory():
        yield ReviewQueue(db.conn)

    app = ReviewApi(factory, {"reviewer-token-0123456789": "alice"})
    env = {"REQUEST_METHOD": "GET", "PATH_INFO": f"/review/actions/{pid}", "QUERY_STRING": "", "wsgi.input": io.BytesIO(b""), "CONTENT_LENGTH": "0",
           "HTTP_AUTHORIZATION": "Bearer reviewer-token-0123456789"}
    out = {}
    body = json.loads(b"".join(app(env, lambda s, h: out.update(status=int(s.split()[0])))))
    assert out["status"] == 200 and body["action"]["lead"]["composite_score"] == 75
    assert [f["rule_code"] for f in body["action"]["findings"]] == ["URGENCY_PRESSURE"]

"""Consented lead intake on Postgres: consent, leads, events, the qualifier loop and the send gate. Needs TEST_ADMIN_DSN.
Names LP*."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from psycopg import errors

from orchestrator.lead_intake import (ALREADY_IMPORTED, CONSENT_REFRESHED, IMPORTED, Q_EXPIRED, Q_NO_EVIDENCE, SKIPPED_WITHDRAWN, WOULD_IMPORT,
                                      ConsentedLeadIntake, IntakeError, intake_audit_hook)
from orchestrator.lead_store import PgLeadRepo
from orchestrator.qualifier import Qualifier
from orchestrator.scoring import IcpDefinition, RuleScorer
from shared.audit import AuditWriter
from shared.bus.publisher import publish as real_publish
from shared.bus.store import PgOutboxStore
from shared.bus.worker import Worker
from shared.consent import ConsentService

URL = "https://example.test/optin"
HEAD = "email,name,consented_at,source_url,product_id,kind"
JANE, KIM, LEE = "jane.doe@example.test", "kim.roe@example.test", "lee.poe@example.test"


def day(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def csv_of(*rows):
    return "\n".join([HEAD, *rows]) + "\n"


def row(email=JANE, name="Jane Doe", when=None, url=URL, product="gr-1", kind="SALE"):
    return f"{email},{name},{day(1) if when is None else when},{url},{product},{kind}"


def one(db, q, *a):
    return db.conn.execute(q, a).fetchone()[0]


def svc(db, publish=None, audit=True):
    w = AuditWriter(db.conn)
    consent = ConsentService(db.conn, (lambda a, d: w.record(d["actor"], a, marketplace=d["marketplace"])) if audit else None)
    return ConsentedLeadIntake(db.conn, consent, audit=intake_audit_hook(w) if audit else None, publish=publish)


def run(db, text, **kw):
    kw.setdefault("actor", "ops-1")
    return [r.outcome for r in svc(db).import_csv(text, **kw).results]


def counts(db):
    return tuple(one(db, f"SELECT count(*) FROM {t}") for t in ("pii.contact", "pii.contact_permission", "core.lead", "core.outbox_event"))


def contact_id(db, email):
    return one(db, "SELECT contact_id FROM pii.contact WHERE lower(email)=lower(%s)", email)


def test_LP1_an_import_creates_contacts_consents_leads_and_signals_together(db):
    outcomes = run(db, csv_of(row(), row(KIM, "Kim Roe", kind="SUBSCRIBER"), row(LEE, "Lee Poe", product="gr-2")))
    assert outcomes == [IMPORTED] * 3 and counts(db) == (3, 3, 3, 3)
    assert one(db, "SELECT count(*) FROM pii.contact WHERE quarantined") == 0
    assert db.conn.execute("SELECT DISTINCT basis, channel::text, source_url FROM pii.contact_permission").fetchall() == [("consent", "EMAIL", URL)]
    leads = db.conn.execute("SELECT source_type, source_marketplace::text, status::text, product_external_id, contact_id IS NOT NULL FROM core.lead ORDER BY product_external_id, id").fetchall()
    assert leads == [("INBOUND", "EMAIL", "SIGNAL_CAPTURED", "gr-1", True)] * 2 + [("INBOUND", "EMAIL", "SIGNAL_CAPTURED", "gr-2", True)]
    payloads = [p for (p,) in db.conn.execute("SELECT payload FROM core.outbox_event WHERE event_type='signal.captured'").fetchall()]
    assert all(p["has_contact"] is True and "@" not in json.dumps(p) for p in payloads) and {p["kind"] for p in payloads} == {"SALE", "SUBSCRIBER"}
    actions = [a for (a,) in db.conn.execute("SELECT action_type FROM audit.event ORDER BY seq").fetchall()]
    assert actions.count("CONSENT_CAPTURED") == 3 and actions.count("CONSENT_IMPORT_COMPLETED") == 1


def test_LP2_running_the_same_file_again_changes_nothing(db):
    text = csv_of(row(), row(KIM, "Kim Roe"), row(LEE, "Lee Poe"))
    run(db, text)
    before = counts(db)
    assert run(db, text) == [ALREADY_IMPORTED] * 3 and counts(db) == before


def test_LP3_an_old_list_cannot_undo_an_opt_out_but_a_newer_consent_can(db):
    run(db, csv_of(row(), row(KIM, "Kim Roe")))
    ConsentService(db.conn).withdraw(contact_id(db, JANE), "EMAIL")
    assert run(db, csv_of(row(), row(KIM, "Kim Roe"))) == [SKIPPED_WITHDRAWN, ALREADY_IMPORTED]
    assert ConsentService(db.conn).current_permission(contact_id(db, JANE), "EMAIL") is None
    later = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    assert run(db, csv_of(row(when=later))) == [CONSENT_REFRESHED]                     # a fresh consent restores them
    assert ConsentService(db.conn).current_permission(contact_id(db, JANE), "EMAIL") is not None and one(db, "SELECT count(*) FROM core.lead") == 2


def test_LP4_rows_without_usable_consent_are_kept_unreachable_until_a_valid_row_arrives(db):
    assert run(db, csv_of(row(JANE, when=""), row(KIM, "Kim Roe", when="2024-01-01"))) == [Q_NO_EVIDENCE, Q_EXPIRED]
    assert counts(db) == (2, 0, 0, 0) and one(db, "SELECT count(*) FROM pii.contact WHERE quarantined") == 2
    assert run(db, csv_of(row(JANE))) == [IMPORTED]
    cid = contact_id(db, JANE)
    assert one(db, "SELECT quarantined FROM pii.contact WHERE contact_id=%s", cid) is False and ConsentService(db.conn).current_permission(cid, "EMAIL")
    assert one(db, "SELECT quarantined FROM pii.contact WHERE contact_id=%s", contact_id(db, KIM)) is True


def test_LP5_an_address_in_any_case_reuses_the_existing_contact(db):
    ConsentService(db.conn).capture("Jane.Doe@Example.test", "EMAIL", URL, name="Original")
    assert run(db, csv_of(row(JANE, name="Other"))) == [IMPORTED] and one(db, "SELECT count(*) FROM pii.contact") == 1
    assert one(db, "SELECT name FROM pii.contact") == "Original"


def test_LP6_the_loop_runs_imported_leads_are_scored_with_a_contact_and_no_consent_risk(db):
    run(db, csv_of(row(), row(KIM, "Kim Roe", kind="SUBSCRIBER")))
    worker = Worker(PgOutboxStore(db.conn), "w1")
    Qualifier(RuleScorer(IcpDefinition(frozenset({"gr-1"}))), PgLeadRepo()).register(worker)
    assert worker.drain().retried == 0
    rows = db.conn.execute("SELECT l.status::text, l.composite_score, l.risk_flags FROM core.lead l JOIN pii.contact c ON c.contact_id=l.contact_id "
                           "ORDER BY l.composite_score DESC").fetchall()
    assert rows == [("QUALIFIED", 75, []), ("NEEDS_REVIEW", 65, [])]                   # a contact on record: no CONSENT_RISK flag
    assert len(db.conn.execute("SELECT 1 FROM core.outbox_event WHERE event_type='lead.qualified'").fetchall()) == 1


def test_LP7_the_consent_an_import_records_opens_the_send_gate_until_it_is_withdrawn(db):
    run(db, csv_of(row()))
    cid = contact_id(db, JANE)
    perm = ConsentService(db.conn).current_permission(cid, "EMAIL")
    lead = one(db, "SELECT id FROM core.lead WHERE contact_id=%s", cid)

    def proposal():
        return one(db, "INSERT INTO core.action_proposal(lead_id,role,marketplace,channel,action_type,autonomy_tier,compliance_verdict,status,"
                       "contact_permission_id,idempotency_key,audit_trace_id) VALUES (%s,'COPYWRITER','EMAIL','EMAIL','SEND_MESSAGE',1,'PASS','APPROVED',%s,"
                       "gen_random_uuid(),gen_random_uuid()) RETURNING id", lead, perm)

    db.conn.execute("UPDATE core.action_proposal SET status='SENDING' WHERE id=%s", (proposal(),))
    ConsentService(db.conn).withdraw(cid, "EMAIL")
    with pytest.raises(errors.Error) as e:
        db.conn.execute("UPDATE core.action_proposal SET status='SENDING' WHERE id=%s", (proposal(),))
    assert e.value.sqlstate == "CP002"


def test_LP8_each_row_is_atomic_and_a_rerun_after_a_failure_finishes_the_job(db):
    calls = []

    def flaky(conn, etype, **kw):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("bus down")
        return real_publish(conn, etype, **kw)

    text = csv_of(row(), row(KIM, "Kim Roe"))
    with pytest.raises(RuntimeError):
        svc(db, publish=flaky).import_csv(text, actor="ops-1")
    assert counts(db) == (1, 1, 1, 1)                                                  # row one committed; row two left nothing behind
    assert db.conn.execute("SELECT count(*) FROM pii.contact WHERE lower(email)=%s", (KIM,)).fetchone()[0] == 0
    assert run(db, text) == [ALREADY_IMPORTED, IMPORTED] and counts(db) == (2, 2, 2, 2)


def test_LP9_a_dry_run_writes_nothing_and_predicts_the_real_run(db):
    text = csv_of(row(), row(KIM, "Kim Roe", when=""), row("bad"))
    assert run(db, text, dry_run=True) == [WOULD_IMPORT, Q_NO_EVIDENCE, "REJECTED_INVALID_EMAIL"]
    assert counts(db) == (0, 0, 0, 0) and one(db, "SELECT count(*) FROM audit.event") == 0
    assert run(db, text) == [IMPORTED, Q_NO_EVIDENCE, "REJECTED_INVALID_EMAIL"] and counts(db)[0] == 2


def test_LP10_a_file_that_cannot_be_imported_writes_nothing(db):
    for text in ("email,phone\na@example.test,123\n", "", "name\nJane\n"):
        with pytest.raises(IntakeError):
            svc(db).import_csv(text, actor="ops-1")
    assert counts(db) == (0, 0, 0, 0)


def test_LP11_the_review_role_can_import_and_the_app_role_cannot(db):
    db.conn.execute("SET ROLE review_svc")
    try:
        assert run(db, csv_of(row())) == [IMPORTED]
    finally:
        db.conn.execute("RESET ROLE")
    before = counts(db)
    db.conn.execute("SET ROLE app_rw")
    try:
        with pytest.raises(errors.InsufficientPrivilege):
            run(db, csv_of(row(KIM, "Kim Roe")))
    finally:
        db.conn.execute("RESET ROLE")
    assert counts(db) == before


def test_LP12_no_address_or_name_reaches_a_lead_an_event_or_the_audit_trail(db):
    run(db, csv_of(row(), row(KIM, "Kim Roe", kind="SUBSCRIBER")))
    blob = "".join(json.dumps(db.conn.execute(q).fetchall(), default=str) for q in (
        "SELECT row_to_json(l) FROM core.lead l", "SELECT row_to_json(e) FROM core.outbox_event e", "SELECT row_to_json(a) FROM audit.event a"))
    assert "example.test" not in blob and "Jane" not in blob and "Kim" not in blob

"""ConsentService against Postgres, including the database send gate it feeds. Needs TEST_ADMIN_DSN. Names CO*."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from psycopg import errors

from shared.consent import ConsentService

EMAIL = "Jane.Doe@Example.test"
URL = "https://example.test/optin"


def one(db, q, *a):
    return db.conn.execute(q, a).fetchone()[0]


def svc(db, audit=None):
    return ConsentService(db.conn, audit)


def contact_row(db, cid):
    return db.conn.execute("SELECT email, name, quarantined FROM pii.contact WHERE contact_id=%s", (cid,)).fetchone()


def proposal_for(db, contact_id, permission_id):
    lead = one(db, "INSERT INTO core.lead(source_type,signal_ref,signal_id,contact_id) VALUES ('INBOUND',%s,%s,%s) RETURNING id",
               f"sig_{uuid.uuid4().hex[:10]}", uuid.uuid4().hex * 2, contact_id)
    return one(db, "INSERT INTO core.action_proposal(lead_id,role,marketplace,channel,action_type,autonomy_tier,"
                   "compliance_verdict,status,contact_permission_id,idempotency_key,audit_trace_id) "
                   "VALUES (%s,'COPYWRITER','EMAIL','EMAIL','SEND_MESSAGE',1,'PASS','APPROVED',%s,gen_random_uuid(),gen_random_uuid()) "
                   "RETURNING id", lead, permission_id)


def send(db, pid):
    db.conn.execute("UPDATE core.action_proposal SET status='SENDING' WHERE id=%s", (pid,))


def contact_of(db, permission_id):
    return one(db, "SELECT contact_id FROM pii.contact_permission WHERE id=%s", permission_id)


def test_CO1_capture_creates_an_unquarantined_contact_and_a_consent_permission(db):
    t0 = datetime.now(timezone.utc) - timedelta(hours=1)
    pid = svc(db).capture(EMAIL, "EMAIL", URL, name="Jane", captured_at=t0, ttl_days=30)
    cid = contact_of(db, pid)
    assert contact_row(db, cid) == (EMAIL, "Jane", False)
    basis, channel, captured, expires, src, withdrawn = db.conn.execute(
        "SELECT basis, channel::text, captured_at, expires_at, source_url, withdrawn_at FROM pii.contact_permission WHERE id=%s",
        (pid,)).fetchone()
    assert (basis, channel, src, withdrawn) == ("consent", "EMAIL", URL, None)
    assert captured == t0 and expires == t0 + timedelta(days=30)


def test_CO2_the_same_address_in_any_case_reuses_the_contact_and_keeps_its_name(db):
    first = svc(db).capture(EMAIL, "EMAIL", URL, name="Jane")
    second = svc(db).capture(EMAIL.lower(), "EMAIL", URL)                       # no name given this time
    assert contact_of(db, first) == contact_of(db, second)
    assert one(db, "SELECT count(*) FROM pii.contact") == 1 and one(db, "SELECT count(*) FROM pii.contact_permission") == 2
    assert contact_row(db, contact_of(db, first))[1] == "Jane"


def test_CO3_fresh_consent_releases_a_quarantined_contact(db):
    s = svc(db)
    cid = s.import_unverified(EMAIL, name="Jane")
    assert contact_row(db, cid)[2] is True and s.current_permission(cid, "EMAIL") is None
    s.capture(EMAIL, "EMAIL", URL)
    assert contact_row(db, cid)[2] is False and s.current_permission(cid, "EMAIL") is not None


def test_CO4_import_unverified_is_idempotent_and_never_releases_quarantine(db):
    s = svc(db)
    cid = s.import_unverified(EMAIL)
    assert s.import_unverified(EMAIL.lower()) == cid and one(db, "SELECT count(*) FROM pii.contact") == 1
    assert contact_row(db, cid)[2] is True
    s.capture(EMAIL, "EMAIL", URL)
    assert s.import_unverified(EMAIL) == cid and contact_row(db, cid)[2] is False   # an existing contact is untouched


def test_CO5_withdrawal_stops_every_live_permission_and_keeps_the_history(db):
    s = svc(db)
    p1 = s.capture(EMAIL, "EMAIL", URL)
    cid = contact_of(db, p1)
    s.capture(EMAIL, "EMAIL", URL)
    assert s.withdraw(cid, "EMAIL") == 2 and s.current_permission(cid, "EMAIL") is None
    assert s.withdraw(cid, "EMAIL") == 0                                        # nothing left to stop
    assert one(db, "SELECT count(*) FROM pii.contact_permission WHERE withdrawn_at IS NOT NULL") == 2   # rows remain
    fresh = s.capture(EMAIL, "EMAIL", URL)                                      # re-consent is a new row
    assert s.current_permission(cid, "EMAIL") == fresh


def test_CO6_current_permission_picks_the_latest_live_one_and_skips_expired_ones(db):
    s, now = svc(db), datetime.now(timezone.utc)
    old = s.capture(EMAIL, "EMAIL", URL, captured_at=now - timedelta(days=10), ttl_days=365)
    cid = contact_of(db, old)
    s.capture(EMAIL, "EMAIL", URL, captured_at=now - timedelta(days=5), ttl_days=1)         # already expired
    assert s.current_permission(cid, "EMAIL") == old
    newest = s.capture(EMAIL, "EMAIL", URL, captured_at=now - timedelta(hours=1))
    assert s.current_permission(cid, "EMAIL") == newest


def test_CO7_the_send_gate_follows_capture_withdrawal_expiry_and_quarantine(db):
    s = svc(db)
    ok = s.capture(EMAIL, "EMAIL", URL)
    cid = contact_of(db, ok)
    send(db, proposal_for(db, cid, ok))                                          # valid consent: the gate opens

    gone = s.capture("b@example.test", "EMAIL", URL)
    s.withdraw(contact_of(db, gone), "EMAIL")
    with pytest.raises(errors.Error) as e:
        send(db, proposal_for(db, contact_of(db, gone), gone))
    assert e.value.sqlstate == "CP002"

    stale = s.capture("c@example.test", "EMAIL", URL, captured_at=datetime.now(timezone.utc) - timedelta(days=3), ttl_days=1)
    with pytest.raises(errors.Error) as e:
        send(db, proposal_for(db, contact_of(db, stale), stale))
    assert e.value.sqlstate == "CP002"

    held = s.capture("d@example.test", "EMAIL", URL)
    s.quarantine(contact_of(db, held))
    with pytest.raises(errors.Error) as e:
        send(db, proposal_for(db, contact_of(db, held), held))
    assert e.value.sqlstate == "CP002"


def test_CO8_audit_hooks_get_ids_and_codes_never_the_address(db):
    seen = []
    s = svc(db, audit=lambda a, d: seen.append((a, d)))
    cid = contact_of(db, s.capture(EMAIL, "EMAIL", URL, actor="ops-1"))
    s.withdraw(cid, "EMAIL", actor="unsub-link")
    assert seen == [("CONSENT_CAPTURED", {"actor": "ops-1", "marketplace": "EMAIL"}),
                    ("CONSENT_WITHDRAWN", {"actor": "unsub-link", "marketplace": "EMAIL"})]
    assert "example.test" not in repr(seen)


def test_CO9_only_the_pii_role_can_use_it(db):
    db.conn.execute("SET ROLE app_rw")
    try:
        with pytest.raises(errors.InsufficientPrivilege):
            svc(db).capture(EMAIL, "EMAIL", URL)
    finally:
        db.conn.execute("RESET ROLE")
    db.conn.execute("SET ROLE pii_svc")
    try:
        s = svc(db)
        pid = s.capture(EMAIL, "EMAIL", URL)
        assert s.withdraw(contact_of(db, pid), "EMAIL") == 1
    finally:
        db.conn.execute("RESET ROLE")


def test_CO10_invalid_requests_write_nothing(db):
    for call in (lambda: svc(db).capture("not-an-email", "EMAIL", URL), lambda: svc(db).capture(EMAIL, "GUMROAD", URL),
                 lambda: svc(db).capture(EMAIL, "EMAIL", "http://insecure.test")):
        with pytest.raises(ValueError):
            call()
    assert one(db, "SELECT count(*) FROM pii.contact") == 0 and one(db, "SELECT count(*) FROM pii.contact_permission") == 0


def test_CO11_a_failure_between_the_writes_leaves_no_half_consent(db):
    class SecondWriteFails:
        def __init__(self, conn):
            self.conn, self.n = conn, 0

        def transaction(self):
            return self.conn.transaction()

        def execute(self, *a, **k):
            self.n += 1
            if self.n == 2:
                raise RuntimeError("db went away")
            return self.conn.execute(*a, **k)

    with pytest.raises(RuntimeError):
        ConsentService(SecondWriteFails(db.conn)).capture(EMAIL, "EMAIL", URL)
    assert one(db, "SELECT count(*) FROM pii.contact") == 0                      # the contact insert rolled back too

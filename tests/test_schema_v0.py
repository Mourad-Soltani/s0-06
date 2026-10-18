"""S0-06 / S0-07 schema tests (T1-T25). Requires TEST_ADMIN_DSN (superuser). Synthetic data only."""
import hashlib
import os
import shutil
import subprocess
import threading
import time
import uuid

import psycopg
import pytest
from psycopg import errors
from psycopg.types.json import Json

from conftest import create_db, drop_db, dsn_for


# ----------------------------------------------------------------- helpers
class W:
    """World builder over an autocommit superuser connection."""

    def __init__(self, conn):
        self.c = conn
        self.emails = {}

    def one(self, q, *a):
        return self.c.execute(q, a).fetchone()[0]

    def contact(self, quarantined=False):
        email = f"u{uuid.uuid4().hex[:8]}@example.test"
        cid = self.one(
            "INSERT INTO pii.contact(email,name,quarantined) VALUES (%s,'Test User',%s) RETURNING contact_id",
            email, quarantined)
        self.emails[cid] = email
        return cid

    def perm(self, contact, channel="EMAIL", expired=False, withdrawn=False):
        exp = "now() - interval '1 day'" if expired else "now() + interval '30 days'"
        wd = "now()" if withdrawn else "NULL"
        return self.one(
            "INSERT INTO pii.contact_permission(contact_id,channel,basis,captured_at,expires_at,source_url,withdrawn_at) "
            f"VALUES (%s,%s,'consent', now() - interval '2 days', {exp}, 'https://example.test/optin', {wd}) RETURNING id",
            contact, channel)

    def lead(self, contact=None, signal_ref=None, signal_id=None):
        # `is None`, not `or`: "" must reach the CHECK instead of being silently replaced
        ref = signal_ref if signal_ref is not None else f"sig_{uuid.uuid4().hex[:12]}"
        sid = signal_id if signal_id is not None else hashlib.sha256(ref.encode() + os.urandom(4)).hexdigest()
        return self.one(
            "INSERT INTO core.lead(source_type,signal_ref,signal_id,contact_id) "
            "VALUES ('INBOUND',%s,%s,%s) RETURNING id", ref, sid, contact)

    def salt(self, lead, n=32):
        self.c.execute("INSERT INTO pii.lead_salt(lead_id,salt) VALUES (%s,%s)", (lead, os.urandom(n)))

    def payload(self, lead):
        return self.one("INSERT INTO pii.payload_store(lead_id,body) VALUES (%s,%s) RETURNING payload_id",
                        lead, Json({"body": "hello Test User"}))

    def proposal(self, lead, perm=None, payload=None, status="APPROVED", tier=1, verdict="PASS",
                 channel="EMAIL", action_type="SEND_MESSAGE", requires=True):
        return self.one(
            "INSERT INTO core.action_proposal(lead_id,role,marketplace,channel,action_type,payload_ref,"
            "autonomy_tier,compliance_verdict,status,requires_contact_permission,contact_permission_id,"
            "idempotency_key,audit_trace_id) "
            "VALUES (%s,'COPYWRITER',%s,%s,%s,%s,%s,%s,%s,%s,%s,gen_random_uuid(),gen_random_uuid()) RETURNING id",
            lead, channel, channel, action_type, payload, tier, verdict, status, requires, perm)

    def audit(self, lead=None, action="TEST"):
        self.c.execute("INSERT INTO audit.event(event_id,actor,lead_id,action_type) "
                       "VALUES (gen_random_uuid(),'system',%s,%s)", (lead, action))

    def erasable(self):
        """Full seed: contact, permission, lead, salt, payload, proposal WITH permission + payload refs."""
        c = self.contact()
        perm = self.perm(c)
        lead = self.lead(c)
        self.salt(lead)
        pay = self.payload(lead)
        prop = self.proposal(lead, perm=perm, payload=pay)
        self.audit(lead, "NOTE")  # audit CHECK: SEND_MESSAGE/CREATE_OFFER/REPLY require a marketplace
        return dict(contact=c, perm=perm, lead=lead, payload=pay, proposal=prop, email=self.emails[c])

    def send(self, pid):
        self.c.execute("UPDATE core.action_proposal SET status='SENDING' WHERE id=%s", (pid,))

    def assert_erased(self, s):
        assert self.one("SELECT count(*) FROM pii.contact WHERE contact_id=%s", s["contact"]) == 0
        assert self.one("SELECT count(*) FROM pii.contact_permission WHERE id=%s", s["perm"]) == 0
        assert self.one("SELECT count(*) FROM pii.payload_store WHERE lead_id=%s", s["lead"]) == 0
        assert self.one("SELECT count(*) FROM pii.lead_salt WHERE lead_id=%s", s["lead"]) == 0
        perm_ref, pay_ref, lead_id = self.c.execute(
            "SELECT contact_permission_id, payload_ref, lead_id FROM core.action_proposal WHERE id=%s",
            (s["proposal"],)).fetchone()
        assert perm_ref is None and pay_ref is None and lead_id == s["lead"]
        status, cid = self.c.execute("SELECT status, contact_id FROM core.lead WHERE id=%s", (s["lead"],)).fetchone()
        assert status == "ERASED" and cid is None
        assert self.one("SELECT count(*) FROM core.erasure_ledger WHERE lead_id=%s", s["lead"]) == 1


@pytest.fixture()
def w(db):
    return W(db.conn)


def sqlstate_of(fn):
    with pytest.raises(errors.Error) as e:
        fn()
    return e.value.sqlstate


def as_role(conn, role, fn):
    # NOTE: depends on conn being autocommit. In a non-autocommit transaction, a failed
    # statement aborts the tx and RESET ROLE below would raise InFailedSqlTransaction,
    # masking the original error.
    conn.execute(f"SET ROLE {role}")
    try:
        return fn()
    finally:
        conn.execute("RESET ROLE")


# ----------------------------------------------------------------- gate: T1-T9, T24
def test_T1_valid_permission_sends(w):
    c = w.contact()
    p = w.proposal(w.lead(c), w.perm(c))
    w.send(p)
    assert w.one("SELECT status FROM core.action_proposal WHERE id=%s", p) == "SENT"


@pytest.mark.parametrize("case", ["missing", "expired", "withdrawn", "quarantined",
                                  "other_contact", "wrong_channel", "no_contact"])
def test_T2_to_T6_denied(w, case):
    c = w.contact(quarantined=(case == "quarantined"))
    lead = w.lead(None if case == "no_contact" else c)
    if case == "missing":
        perm = None
    elif case == "other_contact":
        perm = w.perm(w.contact())
    elif case == "wrong_channel":
        perm = w.perm(c, channel="PAYHIP")
    else:
        perm = w.perm(c, expired=(case == "expired"), withdrawn=(case == "withdrawn"))
    p = w.proposal(lead, perm)
    assert sqlstate_of(lambda: w.send(p)) == "CP002"


@pytest.mark.parametrize("verdict", ["BLOCK", "ESCALATE", None])
def test_T7_verdict_must_be_pass(w, verdict):
    c = w.contact()
    p = w.proposal(w.lead(c), w.perm(c), verdict=verdict)
    assert sqlstate_of(lambda: w.send(p)) == "CP001"


def test_T8_must_come_from_approved(w):
    c = w.contact()
    p = w.proposal(w.lead(c), w.perm(c), status="PROPOSED")
    assert sqlstate_of(lambda: w.send(p)) == "CP001"


def test_T9_tier0_cannot_send(w):
    c = w.contact()
    p = w.proposal(w.lead(c), w.perm(c), tier=0)
    with pytest.raises(errors.CheckViolation):
        w.send(p)


def test_T24_cannot_insert_directly_as_sending(w):
    c = w.contact()
    lead, perm = w.lead(c), w.perm(c)
    assert sqlstate_of(lambda: w.proposal(lead, perm, status="SENDING")) == "CP001"


# ----------------------------------------------------------------- races: T10a / T10b
def _bg(fn):
    out = {}

    def run():
        try:
            out["ok"] = fn()
        except Exception as e:  # noqa: BLE001
            out["err"] = e

    t = threading.Thread(target=run)
    t.start()
    return t, out


LOCK_SENTINEL = "expected a backend blocked on a lock"  # scripts/mutation_check.py greps for this


def _wait_for_lock_waiter(w, out, query_substr, timeout=10.0):
    """Deterministic: block until a backend in THIS test database is observably waiting on a lock
    while running a statement containing `query_substr`. Fails (with the sentinel) on timeout.
    No sleeps, no is_alive() guesses. Needs a superuser / pg_monitor connection.
    SERIAL DEPENDENCY: assumes tests run one at a time (CI uses -p no:xdist). The datname scope and
    per-test database clones make cross-test interference unlikely, but parallel workers have not
    been validated; do not enable them without re-checking T10."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        n = w.one(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid() "
            "AND wait_event_type = 'Lock' AND query ILIKE %s",
            f"%{query_substr}%")
        if n > 0:
            return
        time.sleep(0.02)
    pytest.fail(f"{LOCK_SENTINEL} (query ~ {query_substr!r}); none observed in {timeout}s; "
                f"worker outcome: {out!r}")


def test_T10a_withdrawal_waits_for_in_flight_send(w, db):
    c = w.contact()
    perm = w.perm(c)
    p = w.proposal(w.lead(c), perm)
    A, B = psycopg.connect(db.dsn), psycopg.connect(db.dsn)
    try:
        A.execute("UPDATE core.action_proposal SET status='SENDING' WHERE id=%s", (p,))  # holds FOR SHARE, uncommitted

        def withdraw():
            B.execute("SET lock_timeout='15s'")
            B.execute("UPDATE pii.contact_permission SET withdrawn_at=now() WHERE id=%s", (perm,))
            B.commit()

        t, out = _bg(withdraw)
        _wait_for_lock_waiter(w, out, "contact_permission")   # the withdrawal UPDATE is provably blocked
        assert t.is_alive() and not out
        A.commit()
        t.join(20)
        assert "err" not in out
        assert w.one("SELECT status FROM core.action_proposal WHERE id=%s", p) == "SENDING"  # valid at send time
        assert w.one("SELECT withdrawn_at IS NOT NULL FROM pii.contact_permission WHERE id=%s", perm)
    finally:
        A.close()
        B.close()


def test_T10b_send_waits_for_in_flight_withdrawal_then_fails(w, db):
    c = w.contact()
    perm = w.perm(c)
    p = w.proposal(w.lead(c), perm)
    A, B = psycopg.connect(db.dsn), psycopg.connect(db.dsn)  # READ COMMITTED (default)
    try:
        B.execute("UPDATE pii.contact_permission SET withdrawn_at=now() WHERE id=%s", (perm,))  # uncommitted

        def send():
            A.execute("SET lock_timeout='15s'")
            A.execute("UPDATE core.action_proposal SET status='SENDING' WHERE id=%s", (p,))
            A.commit()

        t, out = _bg(send)
        _wait_for_lock_waiter(w, out, "action_proposal")      # the send UPDATE is provably blocked
        assert t.is_alive() and not out
        B.commit()
        t.join(20)
        assert out["err"].sqlstate == "CP002"
    finally:
        A.close()
        B.close()


# ----------------------------------------------------------------- audit immutability: T11
@pytest.mark.parametrize("stmt", ["UPDATE audit.event SET outcome='x'", "DELETE FROM audit.event",
                                  "TRUNCATE audit.event"])
def test_T11_audit_append_only(w, stmt):
    w.audit()
    assert sqlstate_of(lambda: w.c.execute(stmt)) == "AU001"  # owner/superuser: trigger blocks
    with pytest.raises(errors.InsufficientPrivilege):          # app_rw: grants block
        as_role(w.c, "app_rw", lambda: w.c.execute(stmt))


# ----------------------------------------------------------------- erasure: T12-T15, T17, T18
def test_T12_erase_with_seeded_proposal(w):
    s = w.erasable()
    w.c.execute("SELECT core.erase_lead(%s,'test')", (s["lead"],))
    w.assert_erased(s)


def test_T13_audit_untouched_and_pii_free(w):
    s = w.erasable()
    snap = "SELECT md5(string_agg(row_to_json(e)::text,'|' ORDER BY seq)) FROM audit.event e WHERE seq <= %s"
    max_seq = w.one("SELECT max(seq) FROM audit.event")
    before = w.one(snap, max_seq)
    w.c.execute("SELECT core.erase_lead(%s,'test')", (s["lead"],))
    assert w.one(snap, max_seq) == before
    assert w.one("SELECT count(*) FROM audit.event WHERE lead_id=%s AND action_type='LEAD_ERASED'", s["lead"]) == 1
    assert w.one("SELECT count(*) FROM audit.event e WHERE row_to_json(e)::text ILIKE %s", f"%{s['email']}%") == 0
    assert w.one("SELECT count(*) FROM audit.event e WHERE row_to_json(e)::text ILIKE %s", "%Test User%") == 0


def test_T14_shared_contact_two_leads(w):
    c = w.contact()
    perm = w.perm(c)
    leads, props = [], []
    for _ in range(2):
        lead = w.lead(c)
        w.salt(lead)
        props.append(w.proposal(lead, perm=perm, payload=w.payload(lead)))
        leads.append(lead)
    w.c.execute("SELECT core.erase_lead(%s,'test')", (leads[0],))
    for lead, prop in zip(leads, props):
        w.assert_erased(dict(contact=c, perm=perm, lead=lead, proposal=prop))


@pytest.mark.skipif(not (shutil.which("pg_dump") and shutil.which("pg_restore")), reason="needs pg_dump/pg_restore")
def test_T15_restore_then_replay_ledger(w, db, tmp_path):
    s = w.erasable()
    dump = tmp_path / "pre_erasure.dump"
    subprocess.run(["pg_dump", "-Fc", "-f", str(dump), "--dbname", db.dsn], check=True)
    w.c.execute("SELECT core.erase_lead(%s,'test')", (s["lead"],))
    ledger = [r[0] for r in w.c.execute("SELECT lead_id FROM core.erasure_ledger").fetchall()]

    name = f"s006_restore_{uuid.uuid4().hex[:8]}"
    create_db(name)
    try:
        subprocess.run(["pg_restore", "--dbname", dsn_for(name), str(dump)], check=True)
        with psycopg.connect(dsn_for(name), autocommit=True) as r:
            assert r.execute("SELECT count(*) FROM pii.contact").fetchone()[0] == 1   # PII is back (the problem)
            for lead_id in ledger:                                                    # runbook step
                r.execute("SELECT core.erase_lead(%s,'restore-replay')", (lead_id,))
            assert r.execute("SELECT count(*) FROM pii.contact").fetchone()[0] == 0
            assert r.execute("SELECT count(*) FROM pii.payload_store").fetchone()[0] == 0
            assert r.execute("SELECT count(*) FROM pii.lead_salt").fetchone()[0] == 0
    finally:
        drop_db(name)


def test_T17_erase_twice_is_idempotent(w):
    s = w.erasable()
    w.c.execute("SELECT core.erase_lead(%s,'test')", (s["lead"],))
    w.c.execute("SELECT core.erase_lead(%s,'test')", (s["lead"],))
    assert w.one("SELECT count(*) FROM audit.event WHERE lead_id=%s AND action_type='LEAD_ERASED'", s["lead"]) == 1


def test_T18_pii_svc_can_erase(w):
    s = w.erasable()
    as_role(w.c, "pii_svc", lambda: w.c.execute("SELECT core.erase_lead(%s,'pii_svc')", (s["lead"],)))
    w.assert_erased(s)


def test_T25_app_rw_cannot_erase(w):
    s = w.erasable()
    with pytest.raises(errors.InsufficientPrivilege):
        as_role(w.c, "app_rw", lambda: w.c.execute("SELECT core.erase_lead(%s,'app_rw')", (s["lead"],)))


# ----------------------------------------------------------------- privileges / constraints: T16, T19-T23
def test_T16_app_rw_cannot_read_pii(w):
    w.contact()
    with pytest.raises(errors.InsufficientPrivilege):
        as_role(w.c, "app_rw", lambda: w.c.execute("SELECT * FROM pii.contact").fetchall())


def test_T19_permission_bypass_only_for_reply(w):
    c = w.contact()
    lead = w.lead(c)
    p = w.proposal(lead, w.perm(c))
    with pytest.raises(errors.CheckViolation):
        w.c.execute("UPDATE core.action_proposal SET requires_contact_permission=false WHERE id=%s", (p,))
    with pytest.raises(errors.CheckViolation):
        w.proposal(lead, requires=False, action_type="SEND_MESSAGE")
    w.proposal(lead, requires=False, action_type="REPLY")  # allowed


def test_T20_duplicate_signal_id_rejected(w):
    sid = hashlib.sha256(b"dup").hexdigest()
    w.lead(signal_id=sid)
    with pytest.raises(errors.UniqueViolation):
        w.lead(signal_id=sid)


@pytest.mark.parametrize("status", ["SENDING", "SENT", "FAILED", "ROLLED_BACK"])
def test_T21_tier0_never_attempts_send(w, status):
    c = w.contact()
    p = w.proposal(w.lead(c), w.perm(c), tier=0)
    with pytest.raises(errors.CheckViolation):
        w.c.execute("UPDATE core.action_proposal SET status=%s WHERE id=%s", (status, p))


@pytest.mark.parametrize("n,ok", [(31, False), (32, True), (33, False)])
def test_T22_salt_length(w, n, ok):
    lead = w.lead()
    if ok:
        w.salt(lead, n)
    else:
        with pytest.raises(errors.CheckViolation):
            w.salt(lead, n)


@pytest.mark.parametrize("ref", ["a@b.co", "two words", "x" * 129, "", "José"])
def test_T23_signal_ref_rejects_pii_shaped(w, ref):
    with pytest.raises(errors.CheckViolation):
        w.lead(signal_ref=ref)


@pytest.mark.parametrize("ref", ["sale_123", "ord-42:abc.def", str(uuid.uuid4()), "x" * 128])
def test_T23_signal_ref_accepts_opaque_ids(w, ref):
    w.lead(signal_ref=ref)


@pytest.mark.skip(reason="T23b: Scout contract test - needs Scout code (Sprint 2). DB half is covered above.")
def test_T23b_scout_never_writes_pii_shaped_signal_ref():
    ...


def test_offer_discount_units_are_type_consistent(w):
    """ADR-12: PERCENT uses _pct only, FIXED uses _minor only."""
    c = w.contact()
    lead = w.lead(c)
    prod = w.one("INSERT INTO core.product(canonical_title,canonical_description,base_price_minor,currency) "
                 "VALUES ('t','d',1900,'USD') RETURNING id")
    ins = ("INSERT INTO core.offer(lead_id,product_id,marketplace,offer_type,discount_type,"
           "discount_value_minor,discount_value_pct,checkout_url,idempotency_key) "
           "VALUES (%s,%s,'LEMON_SQUEEZY','COUPON_CODE',%s,%s,%s,'https://example.test/c',gen_random_uuid())")
    w.c.execute(ins, (lead, prod, "PERCENT", None, 15))
    w.c.execute(ins, (lead, prod, "FIXED", 500, None))
    for bad in [("PERCENT", 500, 15), ("PERCENT", None, 150), ("FIXED", None, 15), ("FIXED", 500, 15)]:
        with pytest.raises(errors.CheckViolation):
            w.c.execute(ins, (lead, prod, *bad))

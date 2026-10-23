"""Audit writer against Postgres (0001..0004). Needs TEST_ADMIN_DSN. Names are AW*."""
import hashlib
import threading
import uuid

import psycopg
import pytest
from psycopg import errors

from shared.audit import AuditWriter, canonical_bytes, kill_switch_audit_hook
from shared.killswitch import KillSwitch, PgKillStore

EMAIL, NAME = "jane.doe@example.test", "Jane Doe"


def one(db, q, *a):
    return db.conn.execute(q, a).fetchone()[0]


def new_lead(db):
    return one(db, "INSERT INTO core.lead(source_type,signal_ref,signal_id) VALUES ('INBOUND',%s,%s) RETURNING id",
               f"sig_{uuid.uuid4().hex[:10]}", uuid.uuid4().hex * 2)


def role(db, name, fn):
    db.conn.execute(f"SET ROLE {name}")
    try:
        return fn()
    finally:
        db.conn.execute("RESET ROLE")


def test_AW1_rows_hold_ids_codes_and_hashes_only(db):
    lead, w = new_lead(db), AuditWriter(db.conn)
    seq = w.record("system", "SEND_MESSAGE", marketplace="GUMROAD", lead_id=lead,
                   payload={"to": EMAIL, "name": NAME, "body": "hello"}, external_response=b"250 OK " + EMAIL.encode(),
                   compliance_verdict="PASS", autonomy_tier=1, outcome="SENT")
    assert isinstance(seq, int)
    text = one(db, "SELECT row_to_json(e)::text FROM audit.event e WHERE seq=%s", seq)
    assert EMAIL not in text and NAME not in text and "hello" not in text
    ph, rh = db.conn.execute("SELECT payload_hash, external_response_hash FROM audit.event WHERE seq=%s", (seq,)).fetchone()
    assert len(ph) == 32 and len(rh) == 32 and ph != rh


def test_AW2_hash_is_sha256_of_salt_label_zero_and_canonical_data(db):
    lead, w = new_lead(db), AuditWriter(db.conn)
    payload = {"b": 2, "a": EMAIL}
    seq = w.record("system", "NOTE", lead_id=lead, payload=payload)
    salt = bytes(one(db, "SELECT salt FROM pii.lead_salt WHERE lead_id=%s", lead))
    assert len(salt) == 32
    expected = hashlib.sha256(salt + b"payload" + b"\x00" + canonical_bytes(payload)).digest()
    assert bytes(one(db, "SELECT payload_hash FROM audit.event WHERE seq=%s", seq)) == expected


def test_AW3_hashes_are_stable_order_independent_and_label_separated(db):
    lead, w = new_lead(db), AuditWriter(db.conn)
    s1 = w.record("system", "NOTE", lead_id=lead, payload={"a": 1, "b": 2})
    s2 = w.record("system", "NOTE", lead_id=lead, payload={"b": 2, "a": 1})
    s3 = w.record("system", "NOTE", lead_id=lead, payload={"a": 1, "b": 3})
    s4 = w.record("system", "NOTE", lead_id=lead, external_response=canonical_bytes({"a": 1, "b": 2}))
    h = lambda s, c="payload_hash": bytes(one(db, f"SELECT {c} FROM audit.event WHERE seq=%s", s))  # noqa: E731
    assert h(s1) == h(s2) and h(s1) != h(s3)
    assert h(s4, "external_response_hash") != h(s1)                  # same bytes, other label, other hash
    assert one(db, "SELECT count(*) FROM pii.lead_salt WHERE lead_id=%s", lead) == 1   # one salt per lead


def test_AW4_the_same_payload_hashes_differently_for_different_leads(db):
    w, a, b = AuditWriter(db.conn), new_lead(db), new_lead(db)
    sa, sb = (w.record("system", "NOTE", lead_id=x, payload={"k": "same"}) for x in (a, b))
    assert one(db, "SELECT payload_hash FROM audit.event WHERE seq=%s", sa) != \
           one(db, "SELECT payload_hash FROM audit.event WHERE seq=%s", sb)


def test_AW5_erasure_unlinks_hashes_and_blocks_new_ones(db):
    lead, w = new_lead(db), AuditWriter(db.conn)
    seq = w.record("system", "NOTE", lead_id=lead, payload={"k": EMAIL})
    before = bytes(one(db, "SELECT payload_hash FROM audit.event WHERE seq=%s", seq))
    db.conn.execute("SELECT core.erase_lead(%s,'test')", (lead,))
    assert one(db, "SELECT count(*) FROM pii.lead_salt WHERE lead_id=%s", lead) == 0       # the key is gone
    assert bytes(one(db, "SELECT payload_hash FROM audit.event WHERE seq=%s", seq)) == before   # the row stays
    with pytest.raises(errors.Error) as e:
        w.record("system", "NOTE", lead_id=lead, payload={"k": EMAIL})                        # no new salt, no new hash
    assert e.value.sqlstate == "AU002"
    assert one(db, "SELECT count(*) FROM pii.lead_salt WHERE lead_id=%s", lead) == 0
    assert w.record("system", "NOTE", lead_id=lead) is not None                               # no hashing needed: fine


def test_AW6_event_id_makes_recording_idempotent(db):
    w, eid = AuditWriter(db.conn), uuid.uuid4()
    assert isinstance(w.record("system", "NOTE", event_id=eid), int)
    assert w.record("system", "NOTE", event_id=eid) is None
    assert one(db, "SELECT count(*) FROM audit.event WHERE event_id=%s", eid) == 1


def test_AW7_the_writer_is_the_only_way_in(db):
    raw = ("INSERT INTO audit.event(event_id,actor,action_type) VALUES (gen_random_uuid(),'system','NOTE')")
    for r in ("app_rw", "audit_writer"):
        with pytest.raises(errors.InsufficientPrivilege):
            role(db, r, lambda: db.conn.execute(raw))
        assert isinstance(role(db, r, lambda: AuditWriter(db.conn).record(r, "NOTE")), int)    # the function works
    with pytest.raises(errors.InsufficientPrivilege):                                          # not granted to others
        role(db, "pii_svc", lambda: db.conn.execute(
            "SELECT audit.hash_for(gen_random_uuid(), 'payload', '\\x00'::bytea, '\\x00'::bytea)"))


@pytest.mark.parametrize("cols,vals", [
    ("actor,action_type", "'a@b.co','NOTE'"), ("actor,action_type", "'system','lower'"),
    ("actor,action_type,outcome", "'system','NOTE','ok'"), ("actor,action_type,autonomy_tier", "'system','NOTE',3"),
    ("actor,action_type,compliance_verdict", "'system','NOTE','MAYBE'"),
    ("actor,action_type,payload_hash", "'system','NOTE','\\x0102'::bytea"),
    ("actor,action_type,external_response_hash", "'system','NOTE','\\x0102'::bytea")])
def test_AW8_table_checks_bind_even_the_owner(db, cols, vals):
    with pytest.raises(errors.CheckViolation):
        db.conn.execute(f"INSERT INTO audit.event(event_id,{cols}) VALUES (gen_random_uuid(),{vals})")


def test_AW9_concurrent_first_use_creates_one_salt_and_equal_hashes(db):
    lead, hashes, errs = new_lead(db), [], []

    def run():
        try:
            with psycopg.connect(db.dsn, autocommit=True) as conn:
                seq = AuditWriter(conn).record("system", "NOTE", lead_id=lead, payload={"k": "same"})
                hashes.append(bytes(conn.execute("SELECT payload_hash FROM audit.event WHERE seq=%s", (seq,)).fetchone()[0]))
        except Exception as exc:  # noqa: BLE001
            errs.append(exc)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errs, errs
    assert len(hashes) == 4 and len(set(hashes)) == 1
    assert one(db, "SELECT count(*) FROM pii.lead_salt WHERE lead_id=%s", lead) == 1


def test_AW10_kill_switch_events_are_audited(db):
    class Revoker:
        def revoke(self, marketplace):
            return None

    ks = KillSwitch(PgKillStore(db.conn), Revoker(), audit=kill_switch_audit_hook(AuditWriter(db.conn)))
    ks.kill("GUMROAD", "ops-1", "INCIDENT")
    ks.revive("GUMROAD", "ops-2")
    rows = db.conn.execute("SELECT action_type, marketplace::text, actor, outcome FROM audit.event "
                           "WHERE action_type LIKE 'KILL_SWITCH%' ORDER BY seq").fetchall()
    assert rows == [("KILL_SWITCH_ENGAGED", "GUMROAD", "ops-1", "OK"), ("KILL_SWITCH_CLEARED", "GUMROAD", "ops-2", "OK")]


def test_AW11_hash_for_refuses_unknown_leads_and_labels(db):
    lead = new_lead(db)
    for args in (("gen_random_uuid()", "'payload'"), (f"'{lead}'", "'secret'")):
        with pytest.raises(errors.Error) as e:
            db.conn.execute(f"SELECT audit.hash_for({args[0]}, {args[1]}, '\\x00'::bytea, '\\x00'::bytea)")
        assert e.value.sqlstate == "AU002"
    assert one(db, "SELECT count(*) FROM pii.lead_salt") == 0               # nothing was created by a refusal


def test_AW12_erase_lead_still_audits_and_now_requires_an_opaque_actor(db):
    lead = new_lead(db)
    db.conn.execute("SELECT core.erase_lead(%s,'ops-1')", (lead,))
    assert one(db, "SELECT count(*) FROM audit.event WHERE lead_id=%s AND action_type='LEAD_ERASED'", lead) == 1
    other = new_lead(db)
    with pytest.raises(errors.CheckViolation):                               # an email-shaped actor is refused...
        db.conn.execute("SELECT core.erase_lead(%s,'ops@example.test')", (other,))
    assert one(db, "SELECT status FROM core.lead WHERE id=%s", other) != "ERASED"   # ...and nothing was erased

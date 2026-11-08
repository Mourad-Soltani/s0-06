"""The address resolver and the provisioned login roles, on Postgres. Needs TEST_ADMIN_DSN. Names AR*."""
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

import psycopg
import pytest
from psycopg import errors
from psycopg.conninfo import make_conninfo

from orchestrator.address_resolver import PgAddressResolver

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "infra" / "docker"))
import provision_roles  # noqa: E402

PASSWORDS = {"SAP_WORKER_PASSWORD": "w" * 24, "SAP_REVIEW_PASSWORD": "r" * 24, "SAP_WEBHOOK_PASSWORD": "h" * 24}


def contact(db, email="jane.doe@example.test", quarantined=False):
    return db.conn.execute("INSERT INTO pii.contact(email, quarantined) VALUES (%s, %s) RETURNING contact_id", (email, quarantined)).fetchone()[0]


def resolver(db):
    @contextmanager
    def connect():
        yield db.conn
    return PgAddressResolver(connect)


def test_AR1_a_contact_with_an_address_resolves_and_anything_else_resolves_to_nothing(db):
    ok, quarantined, erased = contact(db), contact(db, "q@example.test", True), contact(db, None, False)
    r = resolver(db)
    assert r(str(ok)) == "jane.doe@example.test"
    assert r(str(quarantined)) is None and r(str(erased)) is None and r(str(uuid.uuid4())) is None
    for junk in ("", "not-a-uuid", "1; DROP TABLE pii.contact", None):
        assert r(junk) is None


def test_AR2_reading_addresses_needs_pii_access_and_the_reconcilers_role_has_none(db):
    cid = contact(db)
    r = resolver(db)
    db.conn.execute("SET ROLE review_svc")
    try:
        assert r(str(cid)) == "jane.doe@example.test"
    finally:
        db.conn.execute("RESET ROLE")
    db.conn.execute("SET ROLE app_rw")
    try:
        with pytest.raises(errors.InsufficientPrivilege):
            r(str(cid))
    finally:
        db.conn.execute("RESET ROLE")


def test_AR3_provisioning_creates_working_least_privilege_login_roles_and_is_repeatable(db):
    try:
        assert provision_roles.provision(db.conn, PASSWORDS) == ["sap_worker", "sap_review", "sap_webhook"]
        assert provision_roles.provision(db.conn, {**PASSWORDS, "SAP_WORKER_PASSWORD": "n" * 24}) == ["sap_worker", "sap_review", "sap_webhook"]      # rotation
        flags = db.conn.execute("SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolcanlogin FROM pg_roles WHERE rolname IN ('sap_worker','sap_review','sap_webhook') "
                                "ORDER BY rolname").fetchall()
        assert flags == [("sap_review", False, False, False, True), ("sap_webhook", False, False, False, True), ("sap_worker", False, False, False, True)]
        cid = contact(db)
        for role, password in (("sap_worker", "n" * 24), ("sap_review", "r" * 24)):
            with psycopg.connect(make_conninfo(db.dsn, user=role, password=password), autocommit=True) as c:
                assert c.execute("SELECT email FROM pii.contact WHERE contact_id = %s", (cid,)).fetchone() == ("jane.doe@example.test",)
                assert c.execute("SELECT count(*) FROM core.outbox_event").fetchone()[0] == 0
                with pytest.raises(errors.InsufficientPrivilege):
                    c.execute("CREATE TABLE core.not_allowed (x int)")
        # the receiver faces the internet: it may write the inbox and read nothing personal
        with psycopg.connect(make_conninfo(db.dsn, user="sap_webhook", password="h" * 24), autocommit=True) as c:
            assert c.execute("SELECT count(*) FROM core.signal_inbox").fetchone()[0] == 0
            c.execute("INSERT INTO core.signal_inbox (marketplace, signal_ref, kind, occurred_at) VALUES ('LEMON_SQUEEZY', 'ls_order_1', 'SALE', now())")
            for forbidden in ("SELECT email FROM pii.contact", "SELECT body FROM pii.payload_store"):
                with pytest.raises(errors.InsufficientPrivilege):
                    c.execute(forbidden)
    finally:
        for role in ("sap_worker", "sap_review", "sap_webhook"):
            db.conn.execute(f"DROP OWNED BY {role}")
            db.conn.execute(f"DROP ROLE IF EXISTS {role}")

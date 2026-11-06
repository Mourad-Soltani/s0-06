"""The webhook signal inbox on Postgres. Needs TEST_ADMIN_DSN. Names SI*."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from psycopg import errors

from shared.models import Marketplace, Signal
from shared.signal_inbox import PgSignalInbox

NOW = datetime.now(timezone.utc)


def sig(ref, mp=Marketplace.LEMON_SQUEEZY, product="p1", ago=0, kind="SALE"):
    return Signal(signal_ref=ref, kind=kind, marketplace=mp, product_external_id=product, occurred_at=NOW - timedelta(minutes=ago))


def one(db, q, *a):
    return db.conn.execute(q, a).fetchone()[0]


def test_SI1_a_new_signal_is_stored_once_and_a_repeat_adds_nothing(db):
    inbox, lead = PgSignalInbox(db.conn), uuid.uuid4()
    assert inbox.write(sig("ls_order_1"), lead) is True and inbox.write(sig("ls_order_1"), uuid.uuid4()) is False
    assert one(db, "SELECT count(*) FROM core.signal_inbox") == 1 and one(db, "SELECT attributed_lead_id FROM core.signal_inbox") == lead   # the first write stands


def test_SI2_reading_filters_by_marketplace_and_time_and_returns_oldest_first(db):
    inbox = PgSignalInbox(db.conn)
    for s in (sig("c", ago=1), sig("a", ago=50), sig("b", ago=50), sig("old", ago=60 * 24 * 3), sig("other", Marketplace.PAYHIP, ago=5)):
        inbox.write(s)
    got = inbox.read("LEMON_SQUEEZY", NOW - timedelta(hours=2))
    assert [g.signal_ref for g in got] == ["a", "b", "c"]                          # oldest first, ties by reference; old and other marketplace excluded
    assert all(g.marketplace is Marketplace.LEMON_SQUEEZY and g.kind == "SALE" and g.product_external_id == "p1" for g in got)
    assert [g.signal_ref for g in inbox.read("PAYHIP", NOW - timedelta(hours=1))] == ["other"] and len(inbox.read("LEMON_SQUEEZY", NOW - timedelta(hours=2), limit=2)) == 2


def test_SI3_the_same_reference_on_two_marketplaces_is_two_signals(db):
    inbox = PgSignalInbox(db.conn)
    assert inbox.write(sig("same", Marketplace.LEMON_SQUEEZY)) and inbox.write(sig("same", Marketplace.PAYHIP)) and one(db, "SELECT count(*) FROM core.signal_inbox") == 2


def test_SI4_the_inbox_has_no_column_that_could_hold_a_person(db):
    cols = {c for (c,) in db.conn.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='core' AND table_name='signal_inbox'").fetchall()}
    assert cols == {"marketplace", "signal_ref", "kind", "product_external_id", "occurred_at", "received_at", "attributed_lead_id"}


def test_SI5_the_application_role_can_append_and_read_but_not_change_or_remove(db):
    db.conn.execute("SET ROLE app_rw")
    try:
        inbox = PgSignalInbox(db.conn)
        assert inbox.write(sig("ls_order_9")) and [s.signal_ref for s in inbox.read("LEMON_SQUEEZY", NOW - timedelta(hours=1))] == ["ls_order_9"]
        for sql in ("UPDATE core.signal_inbox SET kind = 'QA'", "DELETE FROM core.signal_inbox"):
            with pytest.raises(errors.InsufficientPrivilege):
                db.conn.execute(sql)
    finally:
        db.conn.execute("RESET ROLE")

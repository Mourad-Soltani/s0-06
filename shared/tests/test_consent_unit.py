"""ConsentService: input validation and orchestration with a fake connection. No database. Names CN*."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from shared.consent import ALLOWED_CHANNELS, DEFAULT_TTL_DAYS, ConsentService

URL = "https://example.test/optin"
EMAIL = "jane.doe@example.test"
T0 = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
CID, PID = uuid.uuid4(), uuid.uuid4()


class Cur:
    def __init__(self, row=None, rowcount=0):
        self.row, self.rowcount = row, rowcount

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


class FakeConn:
    """Records statements; answers from a queue of canned cursors."""
    def __init__(self, *cursors):
        self.queue, self.calls, self.events = list(cursors), [], []

    def execute(self, sql, params=()):
        self.calls.append((" ".join(sql.split()), params))
        self.events.append("sql")
        return self.queue.pop(0) if self.queue else Cur()

    def transaction(self):
        return Tx(self)


def test_CN1_constants():
    assert ALLOWED_CHANNELS == ("EMAIL",) and DEFAULT_TTL_DAYS == 365


@pytest.mark.parametrize("email", ["", "no-at-sign", "a@b", "a b@c.de", "@c.de", "a@@c.de", None, "x" * 250 + "@e.co"])
def test_CN2_invalid_emails_are_refused_before_the_database(email):
    for call in (lambda: ConsentService(None).capture(email, "EMAIL", URL),
                 lambda: ConsentService(None).import_unverified(email)):
        with pytest.raises(ValueError):
            call()                                                    # conn=None: reaching the database would raise AttributeError


@pytest.mark.parametrize("kw", [dict(channel="GUMROAD"), dict(channel="email"), dict(channel=""),
                                dict(source_url="http://example.test/x"), dict(source_url=""), dict(source_url=None),
                                dict(source_url="https://has space.test"), dict(ttl_days=0), dict(ttl_days=-5),
                                dict(captured_at=datetime(2026, 1, 1))])
def test_CN3_consent_needs_a_real_record_of_where_when_and_for_how_long(kw):
    args = dict(email=EMAIL, channel="EMAIL", source_url=URL)
    args.update(kw)
    with pytest.raises(ValueError):
        ConsentService(None).capture(**args)


def test_CN4_withdraw_refuses_unknown_channels_before_the_database():
    with pytest.raises(ValueError):
        ConsentService(None).withdraw(CID, "GUMROAD")


def test_CN5_capture_writes_the_contact_then_the_permission_in_one_transaction():
    conn = FakeConn(Cur((CID,)), Cur((PID,)))
    seen = []
    got = ConsentService(conn, audit=lambda a, d: seen.append((a, d))).capture(
        "  " + EMAIL + "  ", "EMAIL", URL, name="Jane", captured_at=T0, ttl_days=30, actor="ops-1")
    assert got == PID and conn.events == ["begin", "sql", "sql", "commit"]
    (sql1, p1), (sql2, p2) = conn.calls
    assert "INSERT INTO pii.contact " in sql1 and "quarantined = false" in sql1 and p1 == (EMAIL, "Jane")
    assert "INSERT INTO pii.contact_permission" in sql2 and "'consent'" in sql2
    assert p2 == (CID, "EMAIL", T0, T0 + timedelta(days=30), URL)
    assert seen == [("CONSENT_CAPTURED", {"actor": "ops-1", "marketplace": "EMAIL"})]
    assert EMAIL not in repr(seen)                                           # the audit hook never sees the address


def test_CN6_a_failure_between_the_two_writes_rolls_the_pair_back():
    class Boom(FakeConn):
        def execute(self, sql, params=()):
            if len(self.calls) == 1:
                self.calls.append((sql, params))
                raise RuntimeError("db down")
            return super().execute(sql, params)

    conn = Boom(Cur((CID,)))
    seen = []
    with pytest.raises(RuntimeError):
        ConsentService(conn, audit=lambda a, d: seen.append(a)).capture(EMAIL, "EMAIL", URL)
    assert conn.events[-1] == "rollback" and seen == []                       # no audit for a consent that did not happen


def test_CN7_import_unverified_returns_the_new_or_the_existing_contact_without_changing_it():
    new = FakeConn(Cur((CID,)))
    assert ConsentService(new).import_unverified(EMAIL, name="J") == CID
    assert "quarantined" in new.calls[0][0] and "true" in new.calls[0][0].lower() and len(new.calls) == 1
    existing = FakeConn(Cur(None), Cur((CID,)))
    assert ConsentService(existing).import_unverified(EMAIL) == CID
    assert "SELECT contact_id FROM pii.contact" in existing.calls[1][0] and len(existing.calls) == 2


def test_CN8_withdraw_audits_only_when_something_was_stopped():
    seen = []
    svc = lambda n: ConsentService(FakeConn(Cur(rowcount=n)), audit=lambda a, d: seen.append((a, d)))  # noqa: E731
    assert svc(2).withdraw(CID, "EMAIL", actor="unsub-link") == 2
    assert seen == [("CONSENT_WITHDRAWN", {"actor": "unsub-link", "marketplace": "EMAIL"})]
    assert svc(0).withdraw(CID, "EMAIL") == 0 and len(seen) == 1
    conn = FakeConn(Cur(rowcount=1))
    ConsentService(conn).withdraw(CID, "EMAIL", at=T0)
    assert conn.calls[0][1] == (T0, CID, "EMAIL") and "withdrawn_at IS NULL" in conn.calls[0][0]


def test_CN9_current_permission_looks_for_the_latest_live_one():
    conn = FakeConn(Cur((PID,)))
    assert ConsentService(conn).current_permission(CID, "EMAIL") == PID
    sql = conn.calls[0][0]
    assert "withdrawn_at IS NULL" in sql and "expires_at > now()" in sql and "ORDER BY captured_at DESC" in sql
    assert ConsentService(FakeConn(Cur(None))).current_permission(CID, "EMAIL") is None


def test_CN10_quarantine_sets_the_flag_for_one_contact():
    conn = FakeConn()
    ConsentService(conn).quarantine(CID)
    assert "quarantined = true" in conn.calls[0][0] and conn.calls[0][1] == (CID,)

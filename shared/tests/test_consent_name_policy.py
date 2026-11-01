"""An existing contact's name is never overwritten by a later consent capture; a missing name is filled in. Names CNM*.
CNM1 needs no database; the rest need TEST_ADMIN_DSN."""
from shared.consent import ConsentService

URL = "https://example.test/optin"
EMAIL = "Jane.Doe@Example.test"


class _Cur:
    def __init__(self, row=None, rowcount=0):
        self.row, self.rowcount = row, rowcount

    def fetchone(self):
        return self.row


class _Tx:
    def __enter__(self):
        return None

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params=()):
        self.calls.append(" ".join(sql.split()))
        return _Cur(("id",))

    def transaction(self):
        return _Tx()


def one(db, q, *a):
    return db.conn.execute(q, a).fetchone()[0]


def name_of(db, email):
    return one(db, "SELECT name FROM pii.contact WHERE lower(email)=lower(%s)", email)


def test_CNM1_the_upsert_prefers_the_stored_name_over_the_new_one():
    conn = _Conn()
    ConsentService(conn).capture("jane@example.test", "EMAIL", URL, name="Other")
    sql = conn.calls[0]
    assert "COALESCE(pii.contact.name, EXCLUDED.name)" in sql and "COALESCE(EXCLUDED.name, pii.contact.name)" not in sql


def test_CNM2_a_later_capture_with_a_different_name_does_not_replace_the_stored_one(db):
    s = ConsentService(db.conn)
    s.capture(EMAIL, "EMAIL", URL, name="Original")
    s.capture(EMAIL.lower(), "EMAIL", URL, name="Other")
    s.capture(EMAIL.upper(), "EMAIL", URL, name="Another")
    assert name_of(db, EMAIL) == "Original" and one(db, "SELECT count(*) FROM pii.contact") == 1


def test_CNM3_a_missing_name_is_filled_in_once_and_then_kept(db):
    s = ConsentService(db.conn)
    s.import_unverified(EMAIL)                                   # no name on record
    assert name_of(db, EMAIL) is None
    s.capture(EMAIL, "EMAIL", URL, name="Filled")
    assert name_of(db, EMAIL) == "Filled"
    s.capture(EMAIL, "EMAIL", URL, name="Other")
    assert name_of(db, EMAIL) == "Filled"


def test_CNM4_importing_an_unverified_duplicate_never_changes_the_stored_name(db):
    s = ConsentService(db.conn)
    s.capture(EMAIL, "EMAIL", URL, name="Original")
    s.import_unverified(EMAIL.lower(), name="Other")
    assert name_of(db, EMAIL) == "Original"

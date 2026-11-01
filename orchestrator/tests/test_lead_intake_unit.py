"""Consented lead intake: CSV rules, per-row database steps (fake connection), dry run, reporting. No database. Names LI*."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from orchestrator.lead_intake import (ALREADY_IMPORTED, CONSENT_REFRESHED, IMPORTED, Q_EXPIRED, Q_NO_EVIDENCE, SKIPPED_DUPLICATE,
                                      SKIPPED_WITHDRAWN, WOULD_IMPORT, ConsentedLeadIntake, IntakeError, lead_identity, parse_csv)
from shared.bus.events import EventType, event_id_for

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
URL = "https://example.test/optin"
HEAD = "email,name,consented_at,source_url,product_id,kind"
CID, PERM, LEAD = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def parse(*lines, head=HEAD, **kw):
    return parse_csv("\n".join([head, *lines]) + "\n", now=NOW, **kw)


def one(line, **kw):
    (row,) = parse(line, **kw)
    return row


def row(email="jane@example.test", name="Jane", when="2026-10-01", url=URL, product="gr-1", kind="SALE"):
    return f"{email},{name},{when},{url},{product},{kind}"


# ------------------------------------------------------------------ the file as a whole
@pytest.mark.parametrize("text,code", [
    ("", "EMPTY_FILE"), ("\n\n  \n", "EMPTY_FILE"), ("name,consented_at\nJane,2026-10-01\n", "MISSING_EMAIL_COLUMN"),
    ("email,phone\na@b.co,123\n", "UNKNOWN_COLUMN"), ("email,email\na@b.co,a@b.co\n", "DUPLICATE_COLUMN"),
    ("email\n" + "x" * 1_000_001, "FILE_TOO_LARGE"), ("email\n" + "".join(f"a{i}@example.test\n" for i in range(5001)), "TOO_MANY_ROWS"),
    ('email\n"unclosed', "MALFORMED_CSV")])
def test_LI1_a_file_that_cannot_be_imported_is_refused_with_a_fixed_code(text, code):
    with pytest.raises(IntakeError) as e:
        parse_csv(text, now=NOW)
    assert e.value.code == code and "phone" not in str(e.value) and "unclosed" not in str(e.value)


def test_LI1b_encoding_default_url_and_header_tolerance():
    with pytest.raises(IntakeError) as e:
        parse_csv(b"\xff\xfeemail\n", now=NOW)
    assert e.value.code == "NOT_UTF8"
    with pytest.raises(IntakeError) as e:
        parse_csv(HEAD + "\n", now=NOW, default_source_url="http://insecure.test")
    assert e.value.code == "INVALID_DEFAULT_SOURCE_URL"
    text = "\ufeffEmail , Consented_At ,Source_URL\njane@example.test,2026-10-01,https://example.test/optin\n"
    (r,) = parse_csv(text.encode("utf-8"), now=NOW)
    assert r.outcome is None and r.email == "jane@example.test" and r.kind == "SUBSCRIBER" and r.line == 2


# ------------------------------------------------------------------ one row
def test_LI2_a_good_row_is_ready_and_carries_what_the_database_step_needs():
    r = one(row(kind="sale"))
    assert (r.outcome, r.email, r.name, r.source_url, r.product_id, r.kind, r.line) == (None, "jane@example.test", "Jane", URL, "gr-1", "SALE", 2)
    assert r.consented_at == datetime(2026, 10, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize("line,outcome", [
    (row(email="not-an-email"), "REJECTED_INVALID_EMAIL"), (row(email=""), "REJECTED_INVALID_EMAIL"),
    (row(kind="WAT"), "REJECTED_INVALID_KIND"), (row(product="has space"), "REJECTED_INVALID_PRODUCT"),
    (row(url="http://example.test/optin"), "REJECTED_INVALID_SOURCE_URL"), (row(url="https://has space.test"), "REJECTED_INVALID_SOURCE_URL"),
    (row(when="yesterday"), "REJECTED_INVALID_CONSENT_DATE"), (row(when="2026-13-45"), "REJECTED_INVALID_CONSENT_DATE"),
    (row(when="2026-12-01"), "REJECTED_CONSENT_IN_FUTURE"), ("jane@example.test,Jane,2026-10-01", "REJECTED_INVALID_ROW"),
    (row(when=""), Q_NO_EVIDENCE), (row(url=""), Q_NO_EVIDENCE), (row(when="2024-01-01"), Q_EXPIRED)])
def test_LI3_every_row_ends_in_a_named_outcome(line, outcome):
    r = one(line)
    assert r.outcome == outcome and r.line == 2


def test_LI3b_a_rejected_row_keeps_no_address_or_name():
    r = one(row(email="not-an-email"))
    assert (r.email, r.name) == ("", None)
    assert one(row(when="2026-12-01")).email == "" and one(row(when="2026-12-01")).name is None


def test_LI4_a_default_source_url_fills_in_and_a_row_url_wins():
    assert one(row(url=""), default_source_url="https://example.test/default").source_url == "https://example.test/default"
    assert one(row(url="https://example.test/own"), default_source_url="https://example.test/default").source_url == "https://example.test/own"
    assert one(row(url="")).outcome == Q_NO_EVIDENCE


@pytest.mark.parametrize("when,expected", [("2026-10-01", datetime(2026, 10, 1, tzinfo=timezone.utc)),
                                           ("2026-10-01T10:30:00Z", datetime(2026, 10, 1, 10, 30, tzinfo=timezone.utc)),
                                           ("2026-10-01T10:30:00+02:00", datetime(2026, 10, 1, 8, 30, tzinfo=timezone.utc)),
                                           ("2026-10-01 10:30:00", datetime(2026, 10, 1, 10, 30, tzinfo=timezone.utc))])
def test_LI5_dates_in_any_iso_form_become_aware_utc(when, expected):
    assert one(row(when=when)).consented_at == expected


def test_LI5b_the_future_and_expiry_boundaries_are_exact():
    iso = lambda t: t.isoformat()  # noqa: E731
    assert one(row(when=iso(NOW + timedelta(days=1)))).outcome is None
    assert one(row(when=iso(NOW + timedelta(days=1, seconds=1)))).outcome == "REJECTED_CONSENT_IN_FUTURE"
    assert one(row(when=iso(NOW - timedelta(days=365)))).outcome == Q_EXPIRED                      # exactly at the limit: expired
    assert one(row(when=iso(NOW - timedelta(days=365) + timedelta(seconds=1)))).outcome is None
    assert one(row(when=iso(NOW - timedelta(days=100))), ttl_days=90).outcome == Q_EXPIRED


def test_LI6_duplicates_are_judged_among_rows_that_would_create_a_lead():
    rows = parse(row(), row(email="JANE@example.test"), row(product="gr-2"), row(email="kim@example.test", when=""), row(email="kim@example.test"))
    assert [r.outcome for r in rows] == [None, SKIPPED_DUPLICATE, None, Q_NO_EVIDENCE, None]       # kim's valid row is not shadowed


def test_LI7_csv_quirks_quoting_blank_lines_and_hostile_names():
    rows = parse('"jane@example.test","Doe, Jane ""JD""",2026-10-01,' + URL + ",gr-1,SALE", "", "  ,  ,  ", row(email="kim@example.test", name="K\x00im\x07" + "x" * 200))
    assert [r.outcome for r in rows] == [None, None] and rows[0].name == 'Doe, Jane "JD"'
    assert "\x00" not in rows[1].name and "\x07" not in rows[1].name and len(rows[1].name) <= 120
    assert [r.line for r in rows] == [2, 5]                                                       # line numbers count the blank lines


# ------------------------------------------------------------------ the database step, with fakes
class Cur:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class Tx:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        self.conn.log.append("begin")

    def __exit__(self, t, v, tb):
        self.conn.log.append("rollback" if t else "commit")
        return False


class Conn:
    """Routes a statement to a canned answer by a fragment of its text."""
    def __init__(self, **answers):
        self.answers, self.calls, self.log = answers, [], []

    def execute(self, sql, params=()):
        sql = " ".join(sql.split())
        self.calls.append((sql, params))
        for needle, value in self.answers.items():
            if needle.replace("_", " ") in sql or needle in sql:
                return Cur(value)
        raise AssertionError(f"unexpected statement: {sql[:80]}")

    def transaction(self):
        return Tx(self)


class Consent:
    def __init__(self):
        self.captures, self.unverified = [], []

    def capture(self, email, channel, source_url, *, name=None, captured_at=None, ttl_days=365, actor="system"):
        self.captures.append((email, channel, source_url, name, captured_at, ttl_days, actor))
        return PERM

    def import_unverified(self, email, *, name=None):
        self.unverified.append((email, name))
        return CID


def intake(conn, audit=None):
    pubs, consent = [], Consent()
    svc = ConsentedLeadIntake(conn, consent, audit=audit, publish=lambda c, et, **kw: pubs.append((et, kw)), clock=lambda: NOW)
    return svc, consent, pubs


NEW = dict(**{"FROM pii.contact WHERE lower(email)": None, "SELECT contact_id FROM pii.contact_permission WHERE id": (CID,),
              "INSERT INTO core.lead": (LEAD,)})


def test_LI8_a_new_contact_gets_a_consent_a_lead_and_a_signal_in_one_transaction():
    conn, seen = Conn(**NEW), []
    svc, consent, pubs = intake(conn, audit=lambda a, d: seen.append((a, d)))
    report = svc.import_csv("\n".join([HEAD, row()]), actor="ops-1")
    assert [(r.line, r.outcome) for r in report.results] == [(2, IMPORTED)] and conn.log == ["begin", "commit"]
    assert consent.captures == [("jane@example.test", "EMAIL", URL, "Jane", datetime(2026, 10, 1, tzinfo=timezone.utc), 365, "ops-1")]
    ref, sid = lead_identity(CID, "gr-1")
    insert = [p for s, p in conn.calls if "INSERT INTO core.lead" in s][0]
    assert insert == ("EMAIL", ref, sid, CID, "gr-1")
    (etype, kw), = pubs
    assert etype == EventType.SIGNAL_CAPTURED and kw["event_id"] == event_id_for(EventType.SIGNAL_CAPTURED, sid)
    assert kw["payload"] == {"kind": "SALE", "occurred_at": datetime(2026, 10, 1, tzinfo=timezone.utc).isoformat(), "has_contact": True,
                             "product_external_id": "gr-1"}
    assert kw["marketplace"] == "EMAIL" and kw["lead_id"] == LEAD and kw["trace_id"] == LEAD and "@" not in str(kw["payload"])
    assert seen == [("CONSENT_IMPORT_COMPLETED", {"actor": "ops-1", "marketplace": "EMAIL", "outcome": "OK"})]


def existing(withdrawn=None, same=None, lead=(LEAD,)):
    return Conn(**{"FROM pii.contact WHERE lower(email)": (CID,), "max(withdrawn_at)": (withdrawn,), "captured_at = %s AND source_url": same,
                   "INSERT INTO core.lead": lead})


@pytest.mark.parametrize("withdrawn,skipped", [(datetime(2026, 10, 1, tzinfo=timezone.utc), True),                 # at the consent date
                                               (datetime(2026, 10, 15, tzinfo=timezone.utc), True),                 # after it
                                               (datetime(2026, 9, 30, 23, 59, 59, tzinfo=timezone.utc), False),     # before it: re-consented
                                               (None, False)])
def test_LI9_an_old_list_never_undoes_an_opt_out(withdrawn, skipped):
    conn = existing(withdrawn)
    svc, consent, pubs = intake(conn)
    (r,) = svc.import_csv("\n".join([HEAD, row()]), actor="ops").results
    if skipped:
        assert r.outcome == SKIPPED_WITHDRAWN and consent.captures == [] and pubs == []
        assert not any("INSERT INTO core.lead" in s for s, _ in conn.calls)
    else:
        assert r.outcome == IMPORTED and len(consent.captures) == 1 and len(pubs) == 1


def test_LI10_re_running_a_file_reuses_the_same_consent_and_creates_nothing_new():
    conn = existing(same=(PERM,), lead=None)
    svc, consent, pubs = intake(conn)
    (r,) = svc.import_csv("\n".join([HEAD, row()]), actor="ops").results
    assert r.outcome == ALREADY_IMPORTED and consent.captures == [] and pubs == []
    conn2 = existing(same=None, lead=None)                                                    # a NEW consent for a known lead
    svc2, consent2, pubs2 = intake(conn2)
    assert svc2.import_csv("\n".join([HEAD, row()]), actor="ops").results[0].outcome == CONSENT_REFRESHED
    assert len(consent2.captures) == 1 and pubs2 == []


def test_LI11_quarantined_rows_are_kept_unreachable_with_no_lead_and_no_transaction():
    conn = Conn()
    svc, consent, pubs = intake(conn)
    report = svc.import_csv("\n".join([HEAD, row(when=""), row(email="kim@example.test", name="Kim", when="2024-01-01")]), actor="ops")
    assert [r.outcome for r in report.results] == [Q_NO_EVIDENCE, Q_EXPIRED]
    assert consent.unverified == [("jane@example.test", "Jane"), ("kim@example.test", "Kim")] and consent.captures == []
    assert conn.calls == [] and conn.log == [] and pubs == []


def test_LI12_a_dry_run_reports_without_writing_anything():
    seen = []
    conn = Conn(**{"FROM pii.contact WHERE lower(email)": None})
    svc, consent, pubs = intake(conn, audit=lambda a, d: seen.append(a))
    report = svc.import_csv("\n".join([HEAD, row(), row(email="kim@example.test", when=""), row(email="bad")]), actor="ops", dry_run=True)
    assert [r.outcome for r in report.results] == [WOULD_IMPORT, Q_NO_EVIDENCE, "REJECTED_INVALID_EMAIL"] and report.dry_run
    assert consent.captures == [] and consent.unverified == [] and pubs == [] and seen == []
    assert all(s.startswith("SELECT") for s, _ in conn.calls)
    known = Conn(**{"FROM pii.contact WHERE lower(email)": (CID,), "max(withdrawn_at)": (None,), "captured_at = %s AND source_url": None,
                    "FROM core.lead WHERE signal_id": (1,)})
    assert intake(known)[0].import_csv("\n".join([HEAD, row()]), actor="ops", dry_run=True).results[0].outcome == ALREADY_IMPORTED


def test_LI13_each_ready_row_gets_its_own_transaction_and_the_report_is_in_file_order():
    conn = Conn(**NEW)
    svc, *_ = intake(conn)
    report = svc.import_csv("\n".join([HEAD, row(), row(email="bad"), row(email="kim@example.test"), row(email="x@example.test", when="")]), actor="ops")
    assert [(r.line, r.outcome) for r in report.results] == [(2, IMPORTED), (3, "REJECTED_INVALID_EMAIL"), (4, IMPORTED), (5, Q_NO_EVIDENCE)]
    assert conn.log == ["begin", "commit", "begin", "commit"] and report.counts == {IMPORTED: 2, "REJECTED_INVALID_EMAIL": 1, Q_NO_EVIDENCE: 1}


def test_LI14_a_failure_midway_propagates_and_earlier_rows_stay_committed():
    class Boom(Conn):
        def execute(self, sql, params=()):
            if "INSERT INTO core.lead" in sql and len([c for c in self.calls if "INSERT INTO core.lead" in c[0]]) == 1:
                self.calls.append((sql, params))
                raise RuntimeError("db down")
            return super().execute(sql, params)

    conn = Boom(**NEW)
    svc, *_ = intake(conn)
    with pytest.raises(RuntimeError):
        svc.import_csv("\n".join([HEAD, row(), row(email="kim@example.test")]), actor="ops")
    assert conn.log == ["begin", "commit", "begin", "rollback"]


def test_LI15_the_report_and_errors_never_contain_an_address_or_a_name():
    conn = Conn(**NEW)
    svc, *_ = intake(conn)
    report = svc.import_csv("\n".join([HEAD, row(email="secret.person@example.test", name="Secret Person")]), actor="ops")
    assert "secret" not in repr(report).lower() and "Person" not in repr(report)
    with pytest.raises(IntakeError) as e:
        parse_csv("email,secret.person@example.test\nx\n", now=NOW)
    assert "secret" not in str(e.value)


@pytest.mark.parametrize("kw", [dict(actor=""), dict(actor="a@b.co"), dict(actor=None), dict(actor="ops", ttl_days=0),
                                dict(actor="ops", ttl_days=True), dict(actor="ops", ttl_days=-5)])
def test_LI16_the_actor_and_expiry_are_validated_before_anything_is_read(kw):
    conn = Conn()
    svc, *_ = intake(conn)
    with pytest.raises(ValueError):
        svc.import_csv("\n".join([HEAD, row()]), **kw)
    assert conn.calls == []


def test_LI17_lead_identity_is_stable_opaque_and_specific():
    a, b = lead_identity(CID, "gr-1"), lead_identity(CID, "gr-1")
    assert a == b and a != lead_identity(CID, "gr-2") and a != lead_identity(CID, None) and a != lead_identity(uuid.uuid4(), "gr-1")
    import re
    assert re.match(r"^[A-Za-z0-9_:.-]{1,128}$", a[0]) and re.match(r"^[0-9a-f]{64}$", a[1]) and a[0].startswith("imp_")

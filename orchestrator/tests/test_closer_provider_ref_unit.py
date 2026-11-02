"""The Closer now records the provider reference with the SENT status. No database. Names CPR*."""
import hashlib

from adapters.errors import RateLimited, TransportError
from orchestrator.tests.test_closer_unit import build, row


def finish_call(conn):
    return [(s, p) for s, p in conn.calls if "SET status = %s::core.proposal_status" in s][0]


def test_CPR1_a_sent_proposal_gets_the_hashed_provider_reference_in_the_same_update():
    closer, conn, channel, *_ = build(row())
    closer.process(__import__("uuid").uuid4())
    sql, params = finish_call(conn)
    assert "provider_ref = COALESCE(%s, provider_ref)" in sql and "AND status = 'SENDING'" in sql
    assert params[0] == "SENT" and params[1] == hashlib.sha256(b"<abc123@mail.example.test>").hexdigest()[:32] and len(params[1]) == 32


def test_CPR2_a_send_that_did_not_happen_records_no_reference():
    for exc, status in ((RateLimited(5.0), "APPROVED"), (TransportError(), "FAILED")):
        closer, conn, channel, *_ = build(row(), channel_exc=exc)
        closer.process(__import__("uuid").uuid4())
        _, params = finish_call(conn)
        assert params[0] == status and params[1] is None

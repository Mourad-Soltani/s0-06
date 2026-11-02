"""The reconciliation job with fakes: every outcome, the guards, the rules it must never break. No database. Names RN*."""
import hashlib
import uuid
from datetime import timedelta

import pytest

from adapters.errors import AuthExpired, LookupUnsupported, RateLimited, TransportError
from orchestrator.reconciler import RECOVERED_SENT, REQUEUED, RESOLVED_ELSEWHERE, Reconciler
from shared.bus.events import EventType, event_id_for
from shared.models import SendResult

LEAD = uuid.uuid4()
PROVIDER_ID = "<abc123@mail.example.test>"
STALE = timedelta(minutes=15)


class Cur:
    def __init__(self, rows=None, rowcount=0):
        self.rows, self.rowcount = rows or [], rowcount

    def fetchall(self):
        return self.rows


class Tx:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        self.conn.log.append("begin")

    def __exit__(self, t, v, tb):
        self.conn.log.append("rollback" if t else "commit")
        return False


class Conn:
    def __init__(self, stuck, updated=1):
        self.stuck, self.updated, self.calls, self.log = stuck, updated, [], []

    def execute(self, sql, params=()):
        sql = " ".join(sql.split())
        self.calls.append((sql, params))
        if sql.startswith("SELECT id, lead_id"):
            return Cur(rows=self.stuck)
        return Cur(rowcount=self.updated)

    def transaction(self):
        return Tx(self)


class Adapter:
    def __init__(self, result=None, exc=None):
        self.result, self.exc, self.asked = result, exc, []

    def find_send(self, key):
        self.asked.append(key)
        if self.exc:
            raise self.exc
        return self.result

    def send_reply(self, *a, **k):
        raise AssertionError("the reconciler must never send")


def stuck_row(pid=None, channel="EMAIL", tier=1):
    return (pid or uuid.uuid4(), LEAD, "EMAIL", channel, tier, uuid.uuid4())


def run(rows, adapter=None, updated=1, alert=None, **kw):
    conn, audits, pubs = Conn(rows, updated), [], []

    class Connect:
        def __enter__(self_inner):
            return conn

        def __exit__(self_inner, *a):
            return False

    r = Reconciler(lambda: Connect(), {"EMAIL": adapter} if adapter is not None else {}, stale_after=kw.pop("stale_after", STALE),
                   audit_factory=lambda c: (lambda a, d: audits.append((a, d))), publish=lambda c, et, **k: pubs.append((et, k)), alert=alert)
    return r, conn, audits, pubs


def test_RN1_it_looks_only_at_stale_tier_1_sends_oldest_first_and_reads_no_message_content():
    r, conn, audits, pubs = run([])
    assert r.run(limit=7) == []
    (sql, params), = conn.calls
    assert "status = 'SENDING' AND autonomy_tier >= 1 AND updated_at < now() - %s ORDER BY updated_at, id LIMIT %s" in sql and params == (STALE, 7)
    assert "payload" not in sql and "body" not in sql and "contact" not in sql and "email" not in sql.lower().replace("channel", "")


def test_RN2_a_send_the_provider_confirms_becomes_sent_with_its_reference_and_nothing_is_sent_again():
    pid = uuid.uuid4()
    adapter = Adapter(SendResult(external_message_id=PROVIDER_ID))
    r, conn, audits, pubs = run([stuck_row(pid)], adapter)
    (res,) = r.run()
    assert (res.proposal_id, res.outcome) == (pid, RECOVERED_SENT) and len(adapter.asked) == 1
    ref = hashlib.sha256(PROVIDER_ID.encode()).hexdigest()[:32]
    sql, params = conn.calls[1]
    assert "status = %s::core.proposal_status" in sql and "provider_ref = COALESCE(%s, provider_ref)" in sql and params == ("SENT", ref, pid, STALE)
    assert audits == [("RECONCILER_SENT", {"marketplace": "EMAIL", "lead_id": LEAD, "tier": 1, "outcome": "OK"})]
    (etype, kw), = pubs
    assert etype == EventType.ACTION_SENT and kw["event_id"] == event_id_for(EventType.ACTION_SENT, pid) and kw["trace_id"] == LEAD
    assert kw["payload"] == {"proposal_id": str(pid), "link": "UNKNOWN", "provider_ref": ref, "deduplicated": False, "recovered": True}


def test_RN3_a_send_the_provider_says_never_happened_goes_back_in_line_for_the_closers_sweep():
    pid = uuid.uuid4()
    r, conn, audits, pubs = run([stuck_row(pid)], Adapter(None))
    (res,) = r.run()
    assert res.outcome == REQUEUED and conn.calls[1][1] == ("APPROVED", None, pid, STALE) and pubs == []
    assert audits[0][0] == "RECONCILER_REQUEUED" and audits[0][1]["outcome"] == "OK"


@pytest.mark.parametrize("exc,code", [(LookupUnsupported(), "LOOKUP_UNSUPPORTED"), (RateLimited(5.0), "RATE_LIMITED"), (AuthExpired(), "AUTH_EXPIRED"),
                                      (TransportError(), "TRANSPORT_ERROR"), (RuntimeError("x"), "RUNTIME_ERROR")])
def test_RN4_when_the_provider_cannot_answer_the_proposal_stays_sending_and_is_audited_and_alerted(exc, code):
    pid, alerts = uuid.uuid4(), []
    r, conn, audits, pubs = run([stuck_row(pid)], Adapter(exc=exc), alert=lambda p, o: alerts.append((p, o)))
    (res,) = r.run()
    assert (res.outcome, res.alerted) == (f"UNRESOLVED_{code}", True) and alerts == [(pid, f"UNRESOLVED_{code}")]
    sql, params = conn.calls[1]
    assert "SET updated_at = now()" in sql and "status = %s" not in sql and params == (pid, STALE)         # only the clock moves
    assert audits == [("RECONCILER_UNRESOLVED", {"marketplace": "EMAIL", "lead_id": LEAD, "tier": 1, "outcome": code})] and pubs == []


def test_RN5_a_broken_alert_channel_does_not_stop_the_other_proposals_being_settled():
    a, b = uuid.uuid4(), uuid.uuid4()

    def broken(p, o):
        raise RuntimeError("pager down")

    r, conn, audits, pubs = run([stuck_row(a), stuck_row(b)], Adapter(exc=TransportError()), alert=broken)
    results = r.run()
    assert [(x.proposal_id, x.alerted) for x in results] == [(a, False), (b, False)] and len(audits) == 2


@pytest.mark.parametrize("adapter", [Adapter(SendResult(external_message_id="x")), Adapter(None), Adapter(exc=TransportError())])
def test_RN6_if_the_closer_finished_in_the_meantime_the_reconciler_does_nothing_more(adapter):
    r, conn, audits, pubs = run([stuck_row()], adapter, updated=0)
    (res,) = r.run()
    assert res.outcome == RESOLVED_ELSEWHERE and audits == [] and pubs == [] and res.alerted is False


def test_RN7_every_update_is_guarded_by_the_current_state_and_staleness():
    for adapter in (Adapter(SendResult(external_message_id="x")), Adapter(None), Adapter(exc=TransportError())):
        r, conn, *_ = run([stuck_row()], adapter)
        r.run()
        sql = conn.calls[1][0]
        assert "AND status = 'SENDING' AND updated_at < now() - %s" in sql and sql.startswith("UPDATE core.action_proposal")


def test_RN8_no_adapter_for_the_channel_is_unresolved_not_a_guess():
    r, conn, audits, pubs = run([stuck_row(channel="EMAIL")], None)
    (res,) = r.run()
    assert res.outcome == "UNRESOLVED_NO_CHANNEL_ADAPTER" and audits[0][1]["outcome"] == "NO_CHANNEL_ADAPTER"


def test_RN9_it_never_sends_in_any_path_and_leaks_nothing_personal():
    for adapter in (Adapter(SendResult(external_message_id=PROVIDER_ID)), Adapter(None), Adapter(exc=RuntimeError(PROVIDER_ID))):
        r, conn, audits, pubs = run([stuck_row()], adapter)
        r.run()                                                                   # send_reply raises AssertionError if it is ever called
        blob = repr(audits) + repr(pubs)
        assert "@" not in blob and "mail.example.test" not in blob


def test_RN10_each_stuck_proposal_is_settled_on_its_own_with_its_own_transaction():
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    class Sequence:
        def __init__(self, *steps):
            self.steps = list(steps)

        def find_send(self, key):
            step = self.steps.pop(0)
            if isinstance(step, BaseException):
                raise step
            return step

    r, conn, audits, pubs = run([stuck_row(a), stuck_row(b), stuck_row(c)], Sequence(SendResult(external_message_id="x"), None, TransportError()))
    assert [(x.proposal_id, x.outcome) for x in r.run()] == [(a, RECOVERED_SENT), (b, REQUEUED), (c, "UNRESOLVED_TRANSPORT_ERROR")]
    assert conn.log == ["begin", "commit"] * 3 and [x[0] for x in audits] == ["RECONCILER_SENT", "RECONCILER_REQUEUED", "RECONCILER_UNRESOLVED"] and len(pubs) == 1


@pytest.mark.parametrize("kw", [dict(stale_after=timedelta(seconds=59)), dict(stale_after=timedelta(0)), dict(stale_after=900), dict(stale_after=None)])
def test_RN11_a_send_in_flight_is_not_stuck_so_the_threshold_has_a_floor(kw):
    with pytest.raises(ValueError):
        Reconciler(lambda: None, {}, **kw)


@pytest.mark.parametrize("limit", [0, 501, True, "5", None])
def test_RN12_the_batch_size_is_bounded(limit):
    r, *_ = run([])
    with pytest.raises(ValueError):
        r.run(limit=limit)

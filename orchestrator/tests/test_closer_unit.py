"""The Closer with fakes: link attribution, the claim-before-send order, every refusal and failure. No database. Names CL*."""
import uuid
from decimal import Decimal
from urllib.parse import urlencode

import pytest

from adapters.errors import AuthExpired, RateLimited, TransportError
from orchestrator.closer import CONSUMER, KILLED, NOT_APPROVED, SENT, SHADOW, CloserConfig, CloserError, Closer, attribute_link
from orchestrator.guard import ComplianceGuard
from orchestrator.guard_rules import GuardConfig
from shared.bus.events import Event, EventType
from shared.models import ComplianceVerdict, SendResult, Verdict

PID, LEAD, CONTACT, PERM, KEY = (uuid.uuid4() for _ in range(5))
PLAIN, ATTR = "https://shop.example.test/l/gr-1", f"https://shop.example.test/l/gr-1?ref={LEAD}"
BODY = f"Subject: Your guide is ready\n\nHi,\n\nHere it is: {PLAIN}\n\nThanks!"


class Cur:
    def __init__(self, row=None, rows=None):
        self.row, self.rows = row, rows or []

    def fetchone(self):
        return self.row

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
    def __init__(self, log, row, live=True, errors=None, sweep=()):
        self.log, self.row, self.live, self.errors, self.sweep, self.calls = log, row, live, errors or {}, sweep, []

    def execute(self, sql, params=()):
        sql = " ".join(sql.split())
        self.calls.append((sql, params))
        for needle, exc in self.errors.items():
            if needle in sql:
                self.log.append("error")
                raise exc
        if "SET status = 'SENDING'" in sql:
            self.log.append("claim")
        elif "SET status = 'FAILED'" in sql:
            self.log.append("failed_before_claim")
        elif "SET status = %s::core.proposal_status" in sql:
            self.log.append(f"finish:{params[0]}")
        if "FOR UPDATE OF p" in sql:
            return Cur(self.row)
        if "FROM pii.contact_permission" in sql:
            return Cur((1,) if self.live else None)
        if "SELECT id FROM core.action_proposal WHERE status = 'APPROVED'" in sql:
            return Cur(rows=[(i,) for i in self.sweep])
        return Cur()

    def transaction(self):
        return Tx(self)


def row(tier=1, status="APPROVED", body=BODY, permission=PERM, contact=CONTACT, product="gr-1", payload_extra=None, channel="EMAIL"):
    payload = {"context": "consented_outreach", "body": body, "product_external_id": product, "discount_pct": None, **(payload_extra or {})} if body is not None else {}
    return (LEAD, "EMAIL", channel, "SEND_MESSAGE", tier, status, KEY, permission, payload, contact, product)


class Products:
    def build_checkout_url(self, pid, params):
        return f"https://shop.example.test/l/{pid}" + (f"?{urlencode(sorted(params.items()))}" if params else "")


class Channel:
    def __init__(self, log, exc=None, external="<abc123@mail.example.test>"):
        self.log, self.exc, self.external, self.sent = log, exc, external, []

    def send_reply(self, conversation_id, body, key):
        self.log.append("send")
        self.sent.append((conversation_id, body, key))
        if self.exc:
            raise self.exc
        return SendResult(external_message_id=self.external)


class PassAdapter:
    def preflight_check(self, action):
        return ComplianceVerdict(verdict=Verdict.PASS)


GUARD = ComplianceGuard({"EMAIL": PassAdapter()}, GuardConfig(allowed_link_hosts=frozenset({"shop.example.test"}), max_discount_pct=Decimal("20")))


class DbError(Exception):
    def __init__(self, sqlstate):
        super().__init__("db")
        self.sqlstate = sqlstate


def build(r, *, live=True, errors=None, channel_exc=None, sweep=(), channels="default", **kw):
    log, audits, pubs = [], [], []
    conn = Conn(log, r, live, errors, sweep)
    channel = Channel(log, channel_exc)

    class Connect:
        def __enter__(self_inner):
            return conn

        def __exit__(self_inner, *a):
            return False

    closer = Closer(lambda: Connect(), Products(), {"EMAIL": channel} if channels == "default" else channels, GUARD,
                    audit_factory=lambda c: (lambda a, d: audits.append((a, d))), publish=lambda c, et, **k: pubs.append((et, k)), **kw)
    return closer, conn, channel, log, audits, pubs


# ------------------------------------------------------------------ the link
@pytest.mark.parametrize("body,expected,how", [
    (f"See {PLAIN} now", f"See {ATTR} now", "ATTRIBUTED"), (f"A {PLAIN} and B {PLAIN}.", f"A {ATTR} and B {ATTR}.", "ATTRIBUTED"),
    (f"({PLAIN}).", f"({ATTR}).", "ATTRIBUTED"), (f"Link: {ATTR}", f"Link: {ATTR}", "ALREADY_ATTRIBUTED"),
    ("No link at all", f"No link at all\n\nHere is the link: {ATTR}", "APPENDED"),
    (f"Other https://shop.example.test/l/gr-10 only", f"Other https://shop.example.test/l/gr-10 only\n\nHere is the link: {ATTR}", "APPENDED"),
    (f"Path {PLAIN}/x and query {PLAIN}?utm=1", f"Path {PLAIN}/x and query {PLAIN}?utm=1\n\nHere is the link: {ATTR}", "APPENDED"),
    (f"Trailing   \n\n", f"Trailing\n\nHere is the link: {ATTR}", "APPENDED")])
def test_CL1_the_plain_link_becomes_the_attributed_one_exactly_and_a_missing_link_is_appended(body, expected, how):
    assert attribute_link(body, PLAIN, ATTR) == (expected, how)


def test_CL1b_only_the_link_changes_the_rest_of_the_approved_text_is_untouched():
    final, _ = attribute_link(BODY, PLAIN, ATTR)
    assert final.replace(ATTR, PLAIN) == BODY


# ------------------------------------------------------------------ sending
def test_CL2_an_approved_tier_1_draft_is_claimed_and_committed_before_it_is_sent_then_recorded():
    closer, conn, channel, log, audits, pubs = build(row())
    result = closer.process(PID)
    assert result.outcome == SENT
    assert log == ["begin", "begin", "claim", "commit", "commit", "send", "begin", "finish:SENT", "commit"]      # the claim is durable first
    (contact, body, key), = channel.sent
    assert (contact, key) == (str(CONTACT), KEY) and f"?ref={LEAD}" in body and body.replace(f"?ref={LEAD}", "") == BODY
    assert audits == [("CLOSER_SENT", {"marketplace": "EMAIL", "lead_id": LEAD, "tier": 1, "outcome": "OK"})]
    (etype, kw), = pubs
    assert etype == EventType.ACTION_SENT and kw["payload"]["link"] == "ATTRIBUTED" and kw["payload"]["deduplicated"] is False
    assert kw["lead_id"] == LEAD and kw["trace_id"] == LEAD and kw["marketplace"] == "EMAIL"


def test_CL3_a_draft_without_the_link_gets_one_appended_before_it_is_sent():
    closer, conn, channel, log, audits, pubs = build(row(body="Subject: Hello\n\nHi there, no link"))
    assert closer.process(PID).outcome == SENT
    assert channel.sent[0][1].endswith(f"Here is the link: {ATTR}") and pubs[0][1]["payload"]["link"] == "APPENDED"


def test_CL4_a_tier_0_draft_is_never_sent_it_is_recorded_as_shadow_mode():
    closer, conn, channel, log, audits, pubs = build(row(tier=0))
    assert closer.process(PID).outcome == SHADOW
    assert channel.sent == [] and "claim" not in log and pubs == [] and audits[0][1]["outcome"] == SHADOW


@pytest.mark.parametrize("status", ["PROPOSED", "SENDING", "SENT", "FAILED", "REJECTED", "ROLLED_BACK"])
def test_CL5_a_replay_or_a_decided_proposal_is_skipped_without_any_side_effect(status):
    closer, conn, channel, log, audits, pubs = build(row(status=status))
    assert closer.process(PID).outcome == NOT_APPROVED
    assert channel.sent == [] and "claim" not in log and audits == [] and pubs == []


def test_CL6_a_missing_proposal_raises_so_the_bus_retries_then_gives_up():
    closer, *_ = build(None)
    with pytest.raises(CloserError) as e:
        closer.process(PID)
    assert e.value.code == "NOT_FOUND"


def test_CL7_an_active_kill_switch_leaves_the_proposal_approved_for_a_later_sweep():
    closer, conn, channel, log, audits, pubs = build(row(), errors={"SET status = 'SENDING'": DbError("CP003")})
    assert closer.process(PID).outcome == KILLED
    assert channel.sent == [] and "failed_before_claim" not in log and not any(x.startswith("finish") for x in log) and pubs == []
    assert audits[0][1]["outcome"] == KILLED


@pytest.mark.parametrize("sqlstate,outcome", [("CP002", "CONSENT_NOT_CURRENT"), ("CP001", "GATE_REFUSED")])
def test_CL8_a_gate_refusal_marks_the_proposal_failed_and_sends_nothing(sqlstate, outcome):
    closer, conn, channel, log, audits, pubs = build(row(), errors={"SET status = 'SENDING'": DbError(sqlstate)})
    assert closer.process(PID).outcome == outcome
    assert channel.sent == [] and "failed_before_claim" in log and audits[0] == ("CLOSER_FAILED", {"marketplace": "EMAIL", "lead_id": LEAD, "tier": 1, "outcome": outcome})


def test_CL8b_an_unexpected_database_error_is_not_swallowed():
    closer, *_ = build(row(), errors={"SET status = 'SENDING'": DbError("XX000")})
    with pytest.raises(DbError):
        closer.process(PID)


def test_CL9_the_final_text_is_checked_again_and_a_failure_sends_nothing():
    bad = f"Subject: Hi\n\nSee {PLAIN} and also https://evil.test/x"
    closer, conn, channel, log, audits, pubs = build(row(body=bad))
    result = closer.process(PID)
    assert result.outcome == "FINAL_CHECK_LINK_HOST_NOT_ALLOWED" and channel.sent == [] and "claim" not in log and "failed_before_claim" in log
    withdrawn, _, ch2, log2, *_ = build(row(), live=False)
    assert withdrawn.process(PID).outcome == "FINAL_CHECK_NO_CURRENT_PERMISSION" and ch2.sent == []


@pytest.mark.parametrize("exc,status,outcome", [
    (RateLimited(5.0), "APPROVED", "RETRY_LATER_RATE_LIMITED"), (AuthExpired(), "APPROVED", "RETRY_LATER_AUTH_EXPIRED"),
    (TransportError(), "FAILED", "SEND_FAILED_TRANSPORT_ERROR"), (RuntimeError("x"), "FAILED", "SEND_FAILED_RUNTIME_ERROR")])
def test_CL10_a_send_that_certainly_did_not_happen_goes_back_in_line_and_an_unknown_one_is_never_retried_blindly(exc, status, outcome):
    closer, conn, channel, log, audits, pubs = build(row(), channel_exc=exc)
    assert closer.process(PID).outcome == outcome
    assert log[-3:] == ["begin", f"finish:{status}", "commit"] and pubs == [] and audits[0][0] == "CLOSER_FAILED" and len(channel.sent) == 1


@pytest.mark.parametrize("kw,outcome", [(dict(product=None), "NO_PRODUCT"), (dict(contact=None), "NOTHING_TO_SEND"), (dict(body=None), "NOTHING_TO_SEND")])
def test_CL11_a_proposal_missing_what_a_send_needs_fails_cleanly(kw, outcome):
    closer, conn, channel, log, audits, pubs = build(row(**kw))
    assert closer.process(PID).outcome == outcome and channel.sent == [] and "claim" not in log


def test_CL11b_no_adapter_for_the_channel_fails_cleanly():
    closer, conn, channel, log, audits, pubs = build(row(), channels={})
    assert closer.process(PID).outcome == "NO_CHANNEL_ADAPTER" and "claim" not in log


def test_CL12_if_recording_the_result_fails_after_a_send_the_email_is_not_sent_again_on_a_replay():
    closer, conn, channel, log, audits, pubs = build(row(), errors={"SET status = %s::core.proposal_status": RuntimeError("db down")})
    with pytest.raises(RuntimeError):
        closer.process(PID)
    assert len(channel.sent) == 1
    again, _, ch2, log2, *_ = build(row(status="SENDING"))                     # what the database now holds: a claimed, unrecorded send
    assert again.process(PID).outcome == NOT_APPROVED and ch2.sent == []


def test_CL13_the_bus_handler_processes_the_proposal_on_its_own_connection_and_rejects_malformed_events():
    closer, conn, channel, log, audits, pubs = build(row())
    ev = lambda payload: Event(id=1, event_id=uuid.uuid4(), event_type=EventType.ACTION_APPROVED, marketplace="EMAIL", lead_id=LEAD,  # noqa: E731
                              payload_ref=None, payload=payload, trace_id=LEAD, attempts=1)
    closer.handle(object(), ev({"proposal_id": str(PID), "tier": 1}))          # the worker's connection is never touched
    assert len(channel.sent) == 1
    for bad in ({}, {"proposal_id": "garbage"}, None, {"proposal_id": 5}):
        with pytest.raises((ValueError, AttributeError)):
            closer.handle(object(), ev(bad))


def test_CL14_the_sweep_processes_waiting_approved_proposals_and_reports_each():
    other = uuid.uuid4()
    closer, conn, channel, log, audits, pubs = build(row(), sweep=[PID, other])
    results = closer.sweep(limit=10)
    assert [r.proposal_id for r in results] == [PID, other] and all(r.outcome == SENT for r in results)
    sql = [s for s, _ in conn.calls if "status = 'APPROVED' AND autonomy_tier >= 1" in s][0]
    assert "ORDER BY updated_at, id LIMIT %s" in sql


def test_CL15_nothing_personal_reaches_the_audit_the_event_or_the_ledger():
    closer, conn, channel, log, audits, pubs = build(row())
    closer.process(PID)
    blob = repr(audits) + repr(pubs)
    assert "@" not in blob and "mail.example.test" not in blob and "Thanks" not in blob and str(CONTACT) not in blob
    ref = pubs[0][1]["payload"]["provider_ref"]
    assert len(ref) == 32 and all(c in "0123456789abcdef" for c in ref)


def test_CL16_the_consumer_registers_for_action_approved_and_the_link_parameter_is_configurable():
    class W:
        subs = []

        def subscribe(self, etype, name, handler):
            self.subs.append((etype, name, handler))

    closer, conn, channel, *_ = build(row(), config=CloserConfig(ref_param="lead"))
    w = W()
    closer.register(w)
    assert w.subs == [(EventType.ACTION_APPROVED, CONSUMER, closer.handle)] and CONSUMER == "closer"
    closer.process(PID)
    assert f"?lead={LEAD}" in channel.sent[0][1]

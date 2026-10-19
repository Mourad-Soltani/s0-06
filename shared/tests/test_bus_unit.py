"""Unit tests for the worker's control flow and the event helpers. No database, no fixtures."""
import re
import uuid

import pytest

from shared.bus.events import KNOWN_EVENT_TYPES, Event, EventType, error_code, event_id_for
from shared.bus.worker import RunResult, Worker

CONN = object()  # stands in for the store's connection


def make_event(pk=1, event_type=EventType.SIGNAL_CAPTURED, attempts=1):
    return Event(id=pk, event_id=uuid.uuid4(), event_type=event_type, marketplace=None, lead_id=None,
                 payload_ref=None, payload={}, trace_id=None, attempts=attempts)


class FakeStore:
    """Mirrors the contract of PgOutboxStore: per-consumer receipts, and a failed handler leaves no receipt."""

    def __init__(self, events=()):
        self.events = list(events)
        self.receipts = set()
        self.acked, self.failed = [], []
        self.fail_status = "PENDING"
        self.ack_result = True

    def claim(self, worker, limit, lease_seconds, max_attempts):
        out, self.events = self.events[:limit], self.events[limit:]
        return out

    def process(self, consumer, event, handler):
        key = (consumer, event.event_id)
        if key in self.receipts:
            return False
        handler(CONN, event)          # raises -> no receipt, like a rolled-back transaction
        self.receipts.add(key)
        return True

    def ack(self, event_pk, worker):
        self.acked.append(event_pk)
        return self.ack_result

    def fail(self, event_pk, worker, code, max_attempts, backoff_seconds):
        self.failed.append((event_pk, code))
        return self.fail_status

    def reap(self, max_attempts):
        return 7


def test_U1_routes_to_every_subscriber_once_and_acks():
    ev = make_event()
    store, seen = FakeStore([ev]), []
    w = Worker(store, "w1")
    w.subscribe(EventType.SIGNAL_CAPTURED, "a", lambda c, e: seen.append(("a", c, e)))
    w.subscribe(EventType.SIGNAL_CAPTURED, "b", lambda c, e: seen.append(("b", c, e)))
    assert w.run_once() == RunResult(claimed=1, done=1)
    assert seen == [("a", CONN, ev), ("b", CONN, ev)]      # subscription order; handler gets conn and event
    assert store.acked == [1] and store.failed == []


def test_U2_partial_failure_retries_only_the_failed_consumer():
    ev = make_event()
    store, calls, broken = FakeStore([ev]), {"a": 0, "b": 0}, {"b": True}

    def a(c, e): calls["a"] += 1

    def b(c, e):
        calls["b"] += 1
        if broken["b"]:
            raise RuntimeError("boom")

    w = Worker(store, "w1")
    w.subscribe(EventType.SIGNAL_CAPTURED, "a", a)
    w.subscribe(EventType.SIGNAL_CAPTURED, "b", b)
    assert w.run_once() == RunResult(claimed=1, retried=1)
    assert calls == {"a": 1, "b": 1} and store.acked == []
    broken["b"] = False
    store.events.append(ev)                                # redelivery of the same event
    assert w.run_once() == RunResult(claimed=1, done=1)
    assert calls == {"a": 1, "b": 2}                       # a was NOT run again


def test_U3_failure_records_only_the_class_code_never_the_message():
    store = FakeStore([make_event()])
    w = Worker(store, "w1")

    def leaky(c, e):
        raise ValueError("person@example.test said something private")

    w.subscribe(EventType.SIGNAL_CAPTURED, "a", leaky)
    w.run_once()
    assert store.failed == [(1, "VALUE_ERROR")]
    assert "@" not in repr(store.failed) and "private" not in repr(store.failed)


@pytest.mark.parametrize("status,expected", [("DEAD", RunResult(claimed=1, dead=1)),
                                             ("PENDING", RunResult(claimed=1, retried=1)),
                                             (None, RunResult(claimed=1))])
def test_U4_failure_outcomes_are_counted(status, expected):
    store = FakeStore([make_event()])
    store.fail_status = status          # None = the claim was lost to another worker
    w = Worker(store, "w1")
    w.subscribe(EventType.SIGNAL_CAPTURED, "a", lambda c, e: 1 / 0)
    assert w.run_once() == expected


def test_U5_lost_claim_on_ack_is_not_counted_as_done():
    store = FakeStore([make_event()])
    store.ack_result = False
    w = Worker(store, "w1")
    w.subscribe(EventType.SIGNAL_CAPTURED, "a", lambda c, e: None)
    assert w.run_once() == RunResult(claimed=1)


def test_U6_unrouted_events_are_acked_and_counted():
    store = FakeStore([make_event(event_type=EventType.LEAD_SCORED)])
    assert Worker(store, "w1").run_once() == RunResult(claimed=1, done=1, unrouted=1)
    assert store.acked == [1]


def test_U7_subscribe_validation():
    w = Worker(FakeStore(), "w1")
    with pytest.raises(ValueError):
        w.subscribe("not.an_event", "a", lambda c, e: None)
    with pytest.raises(ValueError):
        w.subscribe(EventType.SIGNAL_CAPTURED, "has space", lambda c, e: None)
    with pytest.raises(ValueError):
        w.subscribe(EventType.SIGNAL_CAPTURED, "a@b", lambda c, e: None)
    w.subscribe(EventType.SIGNAL_CAPTURED, "a", lambda c, e: None)
    with pytest.raises(ValueError):
        w.subscribe(EventType.SIGNAL_CAPTURED, "a", lambda c, e: None)   # duplicate pair
    w.subscribe(EventType.LEAD_SCORED, "a", lambda c, e: None)           # same consumer, other event: fine


def test_U8_event_id_for_is_deterministic():
    a = event_id_for(EventType.SIGNAL_CAPTURED, "sig_1")
    assert a == event_id_for(EventType.SIGNAL_CAPTURED, "sig_1")
    assert a != event_id_for(EventType.SIGNAL_CAPTURED, "sig_2")
    assert a != event_id_for(EventType.LEAD_SCORED, "sig_1")
    assert a.version == 5


def test_U9_known_types_cover_the_spec():
    spec_5_1 = {"signal.captured", "lead.scored", "lead.qualified", "action.proposed", "compliance.passed",
                "action.sent", "reply.received", "offer.created", "checkout.completed",
                "adapter.rate_limited", "adapter.auth_expired"}
    assert spec_5_1 <= KNOWN_EVENT_TYPES and "adapter.killed" in KNOWN_EVENT_TYPES
    assert all(re.fullmatch(r"[a-z][a-z_]*\.[a-z][a-z_]*", t) for t in KNOWN_EVENT_TYPES)  # matches the DB CHECK


def test_U10_error_codes_always_fit_the_db_alphabet():
    weird = [type("weird name!", (Exception,), {}), type("___", (Exception,), {}), type("É", (Exception,), {}),
             type("X" * 200, (Exception,), {}), ValueError, KeyError, ZeroDivisionError]
    for cls in weird:
        assert re.fullmatch(r"[A-Z0-9_]{1,64}", error_code(cls("secret@example.test"))), cls
    assert error_code(ValueError("x")) == "VALUE_ERROR"
    assert error_code(type("___", (Exception,), {})("x")) == "ERROR"


def test_U11_drain_stops_when_idle_and_honors_max_rounds():
    store = FakeStore([make_event(pk=i) for i in range(1, 8)])
    w = Worker(store, "w1", limit=3)
    assert w.drain() == RunResult(claimed=7, done=7, unrouted=7)
    assert Worker(FakeStore([make_event(pk=i) for i in range(1, 8)]), "w1", limit=1).drain(max_rounds=2).claimed == 2


def test_U12_reap_passes_the_workers_max_attempts_through():
    assert Worker(FakeStore(), "w1", max_attempts=3).reap() == 7

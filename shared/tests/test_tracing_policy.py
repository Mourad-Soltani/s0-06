"""Tracing policy and the worker's span integration, with a fake tracer: no OpenTelemetry, no database. Names TP*."""
import uuid

import pytest

from shared.bus.events import Event, EventType
from shared.bus.worker import RunResult, Worker
from shared.tracing_policy import (NoopTracer, ViolationCounter, check_span_name, lead_trace_id, sanitize_attributes,
                                   valid_error_code)

EMAIL, NAME = "jane.doe@example.test", "Jane Doe"
LEAD, EID = uuid.uuid4(), uuid.uuid4()


def test_TP1_allowlisted_attributes_pass_and_unknown_keys_are_dropped_by_key_only():
    clean, dropped = sanitize_attributes({"lead.id": LEAD, "event.type": "lead.scored", "marketplace": "GUMROAD",
                                          "attempts": 2, "email": EMAIL, "name": NAME})
    assert clean == {"lead.id": str(LEAD), "event.type": "lead.scored", "marketplace": "GUMROAD", "attempts": 2}
    assert sorted(dropped) == ["key:email", "key:name"]
    assert EMAIL not in repr(dropped) and NAME not in repr(dropped)       # violations never carry the value


@pytest.mark.parametrize("key,value", [
    ("marketplace", EMAIL), ("consumer", "has space"), ("worker.id", "a@b.co"), ("event.type", "Not.An_Event"),
    ("lead.id", "not-a-uuid"), ("lead.id", 42), ("outcome", "done"), ("verdict", "MAYBE"), ("autonomy.tier", 3),
    ("autonomy.tier", True), ("attempts", -1), ("attempts", 1.5), ("llm.model", "a b"), ("error.code", "Boom")])
def test_TP2_invalid_values_are_dropped_and_named_by_key(key, value):
    clean, dropped = sanitize_attributes({key: value})
    assert clean == {} and dropped == [f"value:{key}"]


def test_TP3_none_is_skipped_silently_and_valid_edge_values_pass():
    clean, dropped = sanitize_attributes({"lead.id": None, "marketplace": None, "autonomy.tier": 0, "attempts": 0,
                                          "verdict": "BLOCK", "llm.model": "vendor/model-1:latest",
                                          "cost.micro_usd": 13_500, "event.id": str(EID).upper()})
    assert dropped == [] and clean["event.id"] == str(EID) and clean["autonomy.tier"] == 0 and clean["attempts"] == 0


def test_TP4_span_names_must_be_constants():
    for ok in ("bus.process_event", "action.send_message", "x"):
        check_span_name(ok)
    for bad in (f"send to {EMAIL}", "Has.Caps", "", "has space", "1leading", "x" * 65, None, 5):
        with pytest.raises(ValueError):
            check_span_name(bad)


def test_TP5_one_trace_per_lead_the_trace_id_is_the_audit_trace_id():
    u = uuid.uuid4()
    assert lead_trace_id(u) == u.int and 0 < lead_trace_id(u) < 2 ** 128
    for bad in (uuid.UUID(int=0), str(u), 7, None):
        with pytest.raises(ValueError):
            lead_trace_id(bad)


def test_TP6_error_codes_and_the_violation_counter():
    assert valid_error_code("VALUE_ERROR") and not valid_error_code("value error") and not valid_error_code(None)
    c = ViolationCounter()
    c.add(["key:email", "key:email", "value:marketplace"])
    assert c.counts == {"key:email": 2, "value:marketplace": 1}


class RecHandle:
    def __init__(self, rec):
        self.rec = rec

    def set_attribute(self, key, value):
        clean, _ = sanitize_attributes({key: value})
        self.rec["attrs"].update(clean)

    def set_error_code(self, code):
        self.rec["error"] = code


class RecTracer:
    """Records every span the worker opens, applying the same policy as the real tracer."""
    def __init__(self):
        self.spans = []

    def span(self, name, *, audit_trace_id=None, **attrs):
        from contextlib import contextmanager

        @contextmanager
        def cm():
            clean, dropped = sanitize_attributes(attrs)
            rec = {"name": name, "trace": audit_trace_id, "attrs": clean, "dropped": dropped, "error": None}
            self.spans.append(rec)
            yield RecHandle(rec)
        return cm()


class Store:
    def __init__(self, events):
        self.events, self.fail_status, self.ack_ok = list(events), "PENDING", True

    def claim(self, worker, limit, lease, max_attempts):
        out, self.events = self.events[:limit], self.events[limit:]
        return out

    def process(self, consumer, event, handler):
        handler(None, event)
        return True

    def ack(self, pk, worker):
        return self.ack_ok

    def fail(self, pk, worker, code, max_attempts, backoff):
        return self.fail_status

    def reap(self, n):
        return 0


def event(pk=1, etype=EventType.LEAD_SCORED, trace=None):
    return Event(id=pk, event_id=uuid.uuid4(), event_type=etype, marketplace="GUMROAD", lead_id=LEAD,
                 payload_ref=None, payload={"name": NAME, "to": EMAIL}, trace_id=trace or uuid.uuid4(), attempts=1)


def test_TP7_the_worker_opens_one_pii_free_span_per_event_in_the_leads_trace():
    tr, ev = RecTracer(), event()
    w = Worker(Store([ev]), "w1", tracer=tr)
    w.subscribe(EventType.LEAD_SCORED, "scorer", lambda c, e: None)
    assert w.run_once() == RunResult(claimed=1, done=1)
    (s,) = tr.spans
    assert s["name"] == "bus.process_event" and s["trace"] == ev.trace_id and s["dropped"] == []
    assert s["attrs"] == {"event.type": "lead.scored", "event.id": str(ev.event_id), "outbox.id": 1,
                          "marketplace": "GUMROAD", "lead.id": str(LEAD), "attempts": 1, "worker.id": "w1",
                          "outcome": "DONE"}
    assert EMAIL not in repr(tr.spans) and NAME not in repr(tr.spans)      # the payload never reaches a span


def test_TP8_a_failing_handler_records_the_code_and_never_its_message():
    tr = RecTracer()
    w = Worker(Store([event()]), "w1", tracer=tr)

    def leaky(c, e):
        raise RuntimeError(f"cannot email {EMAIL} ({NAME})")

    w.subscribe(EventType.LEAD_SCORED, "scorer", leaky)
    assert w.run_once() == RunResult(claimed=1, retried=1)
    (s,) = tr.spans
    assert s["error"] == "RUNTIME_ERROR" and s["attrs"]["outcome"] == "PENDING"
    assert EMAIL not in repr(tr.spans) and NAME not in repr(tr.spans)


@pytest.mark.parametrize("kw,expected", [(dict(fail_status="DEAD"), "DEAD"), (dict(fail_status=None), "CLAIM_LOST")])
def test_TP9_failure_outcomes_and_a_lost_claim_are_labelled(kw, expected):
    tr, st = RecTracer(), Store([event()])
    for k, v in kw.items():
        setattr(st, k, v)
    w = Worker(st, "w1", tracer=tr)
    w.subscribe(EventType.LEAD_SCORED, "scorer", lambda c, e: 1 / 0)
    w.run_once()
    assert tr.spans[0]["attrs"]["outcome"] == expected and tr.spans[0]["error"] == "ZERO_DIVISION_ERROR"


def test_TP10_unrouted_events_and_lost_acks_are_labelled_and_the_default_tracer_is_a_no_op():
    tr, st = RecTracer(), Store([event(etype=EventType.SIGNAL_CAPTURED)])
    assert Worker(st, "w1", tracer=tr).run_once() == RunResult(claimed=1, done=1, unrouted=1)
    assert tr.spans[0]["attrs"]["outcome"] == "UNROUTED"
    lost = Store([event()])
    lost.ack_ok = False
    w = Worker(lost, "w1", tracer=tr)
    w.subscribe(EventType.LEAD_SCORED, "scorer", lambda c, e: None)
    assert w.run_once() == RunResult(claimed=1) and tr.spans[1]["attrs"]["outcome"] == "CLAIM_LOST"
    plain = Worker(Store([event()]), "w1")                                  # no tracer given
    plain.subscribe(EventType.LEAD_SCORED, "scorer", lambda c, e: None)
    assert isinstance(plain.tracer, NoopTracer) and plain.run_once() == RunResult(claimed=1, done=1)

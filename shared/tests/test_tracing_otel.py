"""LeadTracer against the real OpenTelemetry SDK with an in-memory exporter: no database. Names TO*."""
import uuid

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from shared.bus.events import Event, EventType
from shared.bus.worker import RunResult, Worker
from shared.tracing import LeadTracer, configure_tracing

EMAIL, NAME = "jane.doe@example.test", "Jane Doe"


def make():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return LeadTracer(provider), exporter


def test_TO1_spans_for_one_lead_share_the_audit_trace_id():
    tracer, exp = make()
    tid = uuid.uuid4()
    with tracer.span("bus.process_event", audit_trace_id=tid):
        with tracer.span("action.send"):                         # a child inherits the lead's trace
            pass
    with tracer.span("action.later", audit_trace_id=tid):         # a later, separate call joins the same trace
        pass
    with tracer.span("action.other"):
        pass
    child, parent, later, other = exp.get_finished_spans()
    assert child.context.trace_id == parent.context.trace_id == later.context.trace_id == tid.int
    assert len({child.context.span_id, parent.context.span_id, later.context.span_id}) == 3
    assert child.parent.span_id == parent.context.span_id
    assert other.context.trace_id != tid.int


def test_TO2_only_allowlisted_ids_and_codes_reach_a_span():
    tracer, exp = make()
    lead = uuid.uuid4()
    with tracer.span("bus.process_event", **{"lead.id": lead, "event.type": "lead.scored", "email": EMAIL,
                                              "marketplace": EMAIL}):
        pass
    (span,) = exp.get_finished_spans()
    assert dict(span.attributes) == {"lead.id": str(lead), "event.type": "lead.scored"}
    assert tracer.violations.counts == {"key:email": 1, "value:marketplace": 1}
    assert EMAIL not in span.to_json()


def test_TO3_an_exception_sets_an_error_code_not_its_message():
    tracer, exp = make()
    with pytest.raises(ValueError):                              # the exception still propagates
        with tracer.span("bus.process_event"):
            raise ValueError(f"bad address {EMAIL}")
    (span,) = exp.get_finished_spans()
    assert span.status.status_code is StatusCode.ERROR and span.status.description == "VALUE_ERROR"
    assert list(span.events) == [] and EMAIL not in span.to_json()


def test_TO4_the_handle_filters_attributes_and_error_codes():
    tracer, exp = make()
    with tracer.span("bus.process_event") as h:
        h.set_attribute("outcome", "DONE")
        h.set_attribute("outcome", "not a code")                 # dropped
        h.set_attribute("email", EMAIL)                          # dropped
        h.set_error_code("HANDLER_FAILED")
        h.set_error_code("not a code")                           # dropped
    (span,) = exp.get_finished_spans()
    assert dict(span.attributes) == {"outcome": "DONE", "error.code": "HANDLER_FAILED"}
    assert span.status.status_code is StatusCode.ERROR and span.status.description == "HANDLER_FAILED"
    assert tracer.violations.counts == {"value:outcome": 1, "key:email": 1, "value:error.code": 1}


def test_TO5_data_bearing_span_names_are_refused():
    tracer, exp = make()
    with pytest.raises(ValueError):
        with tracer.span(f"send to {EMAIL}"):
            pass
    assert exp.get_finished_spans() == ()


def test_TO6_configure_tracing_names_the_service_and_exports_when_given_an_exporter():
    exporter = InMemorySpanExporter()
    provider = configure_tracing(exporter, "svc-under-test")
    assert provider.resource.attributes["service.name"] == "svc-under-test"
    provider.shutdown()


class Store:
    def __init__(self, events):
        self.events = list(events)

    def claim(self, worker, limit, lease, max_attempts):
        out, self.events = self.events[:limit], self.events[limit:]
        return out

    def process(self, consumer, event, handler):
        handler(None, event)
        return True

    def ack(self, pk, worker):
        return True

    def fail(self, pk, worker, code, max_attempts, backoff):
        return "PENDING"

    def reap(self, n):
        return 0


def test_TO7_end_to_end_no_pii_reaches_any_exported_span():
    """The ADR-10 scrub test: PII in the payload and in an exception message must appear nowhere in the export."""
    tracer, exp = make()
    lead, trace_id = uuid.uuid4(), uuid.uuid4()
    ev = Event(id=1, event_id=uuid.uuid4(), event_type=EventType.LEAD_SCORED, marketplace="GUMROAD", lead_id=lead,
               payload_ref=None, payload={"name": NAME, "to": EMAIL}, trace_id=trace_id, attempts=1)
    w = Worker(Store([ev]), "w1", tracer=tracer)

    def leaky(conn, e):
        raise RuntimeError(f"failed for {EMAIL} {NAME} {e.payload}")

    w.subscribe(EventType.LEAD_SCORED, "scorer", leaky)
    assert w.run_once() == RunResult(claimed=1, retried=1)
    (span,) = exp.get_finished_spans()
    blob = span.to_json()
    assert EMAIL not in blob and NAME not in blob and "failed for" not in blob
    assert span.context.trace_id == trace_id.int
    assert dict(span.attributes)["lead.id"] == str(lead) and dict(span.attributes)["event.type"] == "lead.scored"
    assert span.status.description == "RUNTIME_ERROR"

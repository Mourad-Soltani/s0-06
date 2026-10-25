"""OpenTelemetry tracing (S1-11): one trace per lead, PII-free spans.

The OpenTelemetry trace id of every span about a lead IS its audit_trace_id (128 bits), so a lead's whole lifecycle,
across processes and events, is one trace. Policy (shared/tracing_policy.py): span names are constants, attributes
are an allowlist of ids and codes, exceptions are recorded by CLASS CODE only. record_exception is switched off
and the status description is the code, because OpenTelemetry's defaults would copy the exception message, which
can carry personal data (ADR-10: traces are in erasure scope).
The exporter is the deployment's choice (an open item on the cutover checklist); none is bundled.
"""
from __future__ import annotations

import secrets
import uuid
from contextlib import contextmanager
from typing import Any, Optional

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import NonRecordingSpan, SpanContext, Status, StatusCode, TraceFlags

from shared.bus.events import error_code
from shared.tracing_policy import (ViolationCounter, check_span_name, lead_trace_id, sanitize_attributes,
                                   valid_error_code)


def configure_tracing(exporter=None, service_name: str = "sales-agent-platform") -> TracerProvider:
    """Build a provider (not installed globally). Pass an exporter to ship spans; None keeps them in-process."""
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    if exporter is not None:
        provider.add_span_processor(BatchSpanProcessor(exporter))
    return provider


class OtelHandle:
    def __init__(self, span, violations: ViolationCounter):
        self._span, self._violations = span, violations

    def set_attribute(self, key: str, value: Any) -> None:
        clean, dropped = sanitize_attributes({key: value})
        self._violations.add(dropped)
        for k, v in clean.items():
            self._span.set_attribute(k, v)

    def set_error_code(self, code: str) -> None:
        if not valid_error_code(code):
            self._violations.add(["value:error.code"])
            return
        self._span.set_attribute("error.code", code)
        self._span.set_status(Status(StatusCode.ERROR, code))


class LeadTracer:
    def __init__(self, tracer_provider: Optional[TracerProvider] = None, name: str = "sales_agent_platform"):
        provider = tracer_provider or trace.get_tracer_provider()
        self._tracer = provider.get_tracer(name)
        self.violations = ViolationCounter()

    @contextmanager
    def span(self, name: str, *, audit_trace_id: Optional[uuid.UUID] = None, **attrs):
        check_span_name(name)
        clean, dropped = sanitize_attributes(attrs)
        self.violations.add(dropped)
        parent = None
        if audit_trace_id is not None:      # join this lead's trace, even from another process or a later event
            ctx = SpanContext(trace_id=lead_trace_id(audit_trace_id), span_id=secrets.randbits(64) or 1,
                              is_remote=True, trace_flags=TraceFlags(TraceFlags.SAMPLED))
            parent = trace.set_span_in_context(NonRecordingSpan(ctx))
        with self._tracer.start_as_current_span(name, context=parent, attributes=clean, record_exception=False,
                                                set_status_on_exception=False) as span:
            try:
                yield OtelHandle(span, self.violations)
            except BaseException as exc:
                span.set_status(Status(StatusCode.ERROR, error_code(exc)))   # the code only, never the message
                raise

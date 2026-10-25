# ADR-16: Tracing: one trace per lead, PII-free

Status: proposed
Source: S1-11

## Context
Traces are in erasure scope (ADR-10), and OpenTelemetry defaults copy exception messages onto spans.

## Decision
- The trace id of every span about a lead is its `audit_trace_id` (both 128 bits), so a lead's lifecycle is one trace across processes and events.
- Span names are constants. Attributes are an allowlist of ids and codes validated against opaque alphabets; anything else is dropped and counted by key, never by value. Exceptions set an error code only: `record_exception` is off and the status description is the code.
- The exporter is the deployment's choice; none is bundled.

## Consequences
- Instrumentation never raises on a bad attribute. A span name that is not a constant is a programming error and does raise.
- Open: the exporter or collector and trace retention (cutover checklist).

## Revisit when
A backend is chosen, or attributes beyond the allowlist are needed.

"""Tracing policy (S1-11, ADR-10): what may appear on a span. Pure Python, no OpenTelemetry import, so the rules are
testable anywhere. Spans carry the lead's opaque id and codes only. Everything else is dropped and counted by key,
never by value. Instrumentation must never break the application: a bad attribute is dropped, not raised."""
from __future__ import annotations

import re
import threading
import uuid
from contextlib import contextmanager
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

_OPAQUE = re.compile(r"^[A-Za-z0-9_:.-]{1,128}$")
_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_EVENT_TYPE = re.compile(r"^[a-z][a-z_]*\.[a-z][a-z_]*$")
_MODEL = re.compile(r"^[A-Za-z0-9._:/-]{1,128}$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_NAME = re.compile(r"^[a-z][a-z0-9_.]{0,63}$")
_MARKETPLACE = re.compile(r"^[A-Z][A-Z_]{0,31}$")


def _uuid(v):
    s = str(v).lower() if isinstance(v, (uuid.UUID, str)) else None
    return s if s and _UUID.match(s) else None


def _text(rx):
    return lambda v: v if isinstance(v, str) and rx.match(v) else None


def _int(lo, hi=None):
    return lambda v: v if isinstance(v, int) and not isinstance(v, bool) and v >= lo and (hi is None or v <= hi) else None


def _verdict(v):
    return v if v in ("PASS", "BLOCK", "ESCALATE") else None


# key -> validator returning the cleaned value, or None to drop it
ALLOWED: Mapping[str, Callable[[Any], Any]] = {
    "lead.id": _uuid, "event.id": _uuid, "event.type": _text(_EVENT_TYPE), "marketplace": _text(_MARKETPLACE),
    "consumer": _text(_OPAQUE), "worker.id": _text(_OPAQUE), "action.type": _text(_CODE), "outcome": _text(_CODE),
    "error.code": _text(_CODE), "verdict": _verdict, "autonomy.tier": _int(0, 2), "outbox.id": _int(0),
    "attempts": _int(0), "llm.role": _text(_CODE), "llm.model": _text(_MODEL), "llm.input_tokens": _int(0),
    "llm.output_tokens": _int(0), "cost.micro_usd": _int(0)}


def sanitize_attributes(attrs: Mapping[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """Returns (attributes safe to attach, violations). None values are skipped silently. A violation names the KEY
    ('key:x' for a key not on the allowlist, 'value:x' for a value that failed its rule), never the value."""
    clean, dropped = {}, []
    for key, value in attrs.items():
        if value is None:
            continue
        check = ALLOWED.get(key)
        if check is None:
            dropped.append(f"key:{key}")
            continue
        cleaned = check(value)
        if cleaned is None:
            dropped.append(f"value:{key}")
        else:
            clean[key] = cleaned
    return clean, dropped


def check_span_name(name: str) -> None:
    """Span names are constants chosen in code, never data. A programming error, so this one raises."""
    if not isinstance(name, str) or not _NAME.match(name):
        raise ValueError("span names must be constant lower_snake.dotted identifiers")


def valid_error_code(code: Any) -> bool:
    return isinstance(code, str) and bool(_CODE.match(code))


def lead_trace_id(audit_trace_id: uuid.UUID) -> int:
    """One trace per lead: the OpenTelemetry trace id is the audit trace id (both are 128 bits)."""
    if not isinstance(audit_trace_id, uuid.UUID) or audit_trace_id.int == 0:
        raise ValueError("audit_trace_id must be a non-nil UUID")
    return audit_trace_id.int


class ViolationCounter:
    def __init__(self):
        self._lock, self.counts = threading.Lock(), {}

    def add(self, reasons: List[str]) -> None:
        with self._lock:
            for r in reasons:
                self.counts[r] = self.counts.get(r, 0) + 1


class NoopHandle:
    def set_attribute(self, key: str, value: Any) -> None: ...
    def set_error_code(self, code: str) -> None: ...


class NoopTracer:
    @contextmanager
    def span(self, name: str, *, audit_trace_id: Optional[uuid.UUID] = None, **attrs):
        check_span_name(name)
        yield NoopHandle()

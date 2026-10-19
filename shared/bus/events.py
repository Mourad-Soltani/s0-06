"""Event vocabulary and helpers for the Postgres outbox bus (ADR-13). No database imports."""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Optional

# Fixed namespace so event_id_for() is stable across processes and releases. Never change it.
_EVENT_ID_NAMESPACE = uuid.UUID("6f1c1d1e-5a3b-4c1e-9d2a-0b6f3c8e7a41")

CONSUMER_NAME_RE = re.compile(r"^[A-Za-z0-9_:.-]{1,128}$")  # same alphabet as core.consumer_receipt.consumer


class EventType:
    """Spec 5.1 event types, plus adapter.killed emitted by the kill switch (migration 0002)."""
    SIGNAL_CAPTURED = "signal.captured"
    LEAD_SCORED = "lead.scored"
    LEAD_QUALIFIED = "lead.qualified"
    ACTION_PROPOSED = "action.proposed"
    COMPLIANCE_PASSED = "compliance.passed"
    ACTION_SENT = "action.sent"
    REPLY_RECEIVED = "reply.received"
    OFFER_CREATED = "offer.created"
    CHECKOUT_COMPLETED = "checkout.completed"
    ADAPTER_RATE_LIMITED = "adapter.rate_limited"
    ADAPTER_AUTH_EXPIRED = "adapter.auth_expired"
    ADAPTER_KILLED = "adapter.killed"


KNOWN_EVENT_TYPES = frozenset(v for k, v in vars(EventType).items() if not k.startswith("_"))


@dataclass(frozen=True)
class Event:
    """One claimed outbox row. Carries ids and codes only; message bodies live in pii.payload_store."""
    id: int                       # outbox row id (used for ack / fail)
    event_id: uuid.UUID           # idempotency key for publish and consume
    event_type: str
    marketplace: Optional[str]
    lead_id: Optional[uuid.UUID]
    payload_ref: Optional[uuid.UUID]
    payload: Mapping[str, Any]
    trace_id: Optional[uuid.UUID]
    attempts: int


def event_id_for(event_type: str, *parts: object) -> uuid.UUID:
    """Deterministic event_id, so re-running a producer cannot publish the same logical event twice.
    Example: event_id_for(EventType.SIGNAL_CAPTURED, signal_id)."""
    return uuid.uuid5(_EVENT_ID_NAMESPACE, "|".join([event_type, *(str(p) for p in parts)]))


def error_code(exc: BaseException) -> str:
    """Map an exception to a PII-safe code. Only the CLASS name is used, never the message, because
    messages can carry personal data. Always matches ^[A-Z0-9_]{1,64}$ (the DB CHECK)."""
    name = type(exc).__name__
    snake = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    code = re.sub(r"[^A-Za-z0-9]+", "_", snake).strip("_").upper()[:64]
    return code or "ERROR"

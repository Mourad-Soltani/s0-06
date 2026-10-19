"""Transactional publisher. The caller owns the transaction and must call this in the SAME transaction
as the state change that produced the event, so the event exists if and only if the change committed."""
from __future__ import annotations

import uuid
from typing import Any, Mapping, Optional

from psycopg.types.json import Json

from shared.bus.events import KNOWN_EVENT_TYPES


def publish(
    conn,
    event_type: str,
    *,
    event_id: Optional[uuid.UUID] = None,
    marketplace: Optional[str] = None,
    lead_id: Optional[uuid.UUID] = None,
    payload_ref: Optional[uuid.UUID] = None,
    payload: Optional[Mapping[str, Any]] = None,
    trace_id: Optional[uuid.UUID] = None,
) -> uuid.UUID:
    """Insert one outbox event and return its event_id. Never commits.

    Publishing the same event_id twice is a no-op (the first row wins; a different payload for the same
    event_id is ignored). Use event_id_for() for events a retried producer might emit again.
    payload carries ids and codes only. The database rejects email-shaped values, non-objects and
    anything over 8 KB; message bodies go in pii.payload_store and are referenced by payload_ref."""
    if event_type not in KNOWN_EVENT_TYPES:
        raise ValueError(f"unknown event type {event_type!r}")
    eid = event_id or uuid.uuid4()
    conn.execute(
        "INSERT INTO core.outbox_event (event_id, event_type, marketplace, lead_id, payload_ref, payload, trace_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (event_id) DO NOTHING",
        (eid, event_type, marketplace, lead_id, payload_ref, Json(dict(payload or {})), trace_id),
    )
    return eid

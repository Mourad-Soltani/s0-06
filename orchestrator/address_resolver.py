"""Turns the email channel's opaque thread id (a contact's id) into an address at the last moment before a send (cutover kit 1, ADR-27).
Reads pii.contact only: a quarantined contact or one without an address resolves to nothing, so the transport sends nothing. Consent is NOT decided
here: the database send gate already enforced it when the proposal moved to SENDING (ADR-11). Needs a role with pii access (review_svc).
The address is returned to the caller and not stored, logged or put in an exception."""
from __future__ import annotations

import uuid
from typing import Callable, ContextManager, Optional


class PgAddressResolver:
    def __init__(self, connect: Callable[[], ContextManager]):
        self._connect = connect

    def __call__(self, thread_id: str) -> Optional[str]:
        try:
            contact_id = uuid.UUID(str(thread_id))
        except ValueError:
            return None
        with self._connect() as conn:
            row = conn.execute("SELECT email FROM pii.contact WHERE contact_id = %s AND NOT quarantined AND email IS NOT NULL", (contact_id,)).fetchone()
        return row[0] if row else None

    def __repr__(self) -> str:
        return "PgAddressResolver()"

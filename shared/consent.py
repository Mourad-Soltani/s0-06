"""ContactPermission capture and withdrawal (S2-04, ADR-02, ADR-18).

Nothing can be sent to an address without a current permission row (the database send gate, ADR-11). This is how
a row comes to exist, and how it stops existing. Capture needs an explicit consent record: an https source_url
(where consent was given), a capture time, and an expiry. It also clears the contact's quarantine, because ADR-02
releases a contact only on fresh consent. Withdrawal never deletes: it stamps withdrawn_at, so the history stays.
conn must be autocommit and its role must reach schema pii (pii_svc). The audit hook gets ids and codes only.
The default 365-day expiry is a PLACEHOLDER pending legal review (cutover item C9)."""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
ALLOWED_CHANNELS = ("EMAIL",)          # v1.1: email is the only outbound channel (ADR-08)
DEFAULT_TTL_DAYS = 365


class ConsentService:
    def __init__(self, conn, audit: Optional[Callable[[str, dict], None]] = None):
        self.conn, self._audit = conn, audit

    def capture(self, email: str, channel: str, source_url: str, *, name: Optional[str] = None,
                captured_at: Optional[datetime] = None, ttl_days: int = DEFAULT_TTL_DAYS,
                actor: str = "system") -> uuid.UUID:
        """Record consent and return the new permission id. Re-consent adds a new row; it never edits an old one."""
        email = (email or "").strip()
        if not _EMAIL.match(email) or len(email) > 254:
            raise ValueError("invalid email address")
        if channel not in ALLOWED_CHANNELS:
            raise ValueError(f"channel must be one of {ALLOWED_CHANNELS}")
        if not (isinstance(source_url, str) and source_url.startswith("https://") and " " not in source_url):
            raise ValueError("source_url must be the https address where consent was captured")
        if ttl_days <= 0:
            raise ValueError("ttl_days must be positive")
        captured = captured_at or datetime.now(timezone.utc)
        if captured.tzinfo is None:
            raise ValueError("captured_at must be timezone-aware")
        with self.conn.transaction():
            contact_id = self.conn.execute(
                "INSERT INTO pii.contact (email, name, quarantined) VALUES (%s, %s, false) "
                "ON CONFLICT (lower(email)) WHERE email IS NOT NULL DO UPDATE "
                "SET quarantined = false, name = COALESCE(EXCLUDED.name, pii.contact.name) RETURNING contact_id",
                (email, name)).fetchone()[0]
            permission_id = self.conn.execute(
                "INSERT INTO pii.contact_permission (contact_id, channel, basis, captured_at, expires_at, source_url) "
                "VALUES (%s, %s::core.marketplace, 'consent', %s, %s, %s) RETURNING id",
                (contact_id, channel, captured, captured + timedelta(days=ttl_days), source_url)).fetchone()[0]
        self._record("CONSENT_CAPTURED", actor, channel)
        return permission_id

    def import_unverified(self, email: str, *, name: Optional[str] = None) -> uuid.UUID:
        """A contact of unclear provenance: stored quarantined with no permission, so it can never be reached
        (ADR-02). An existing contact is returned untouched and stays as it was."""
        email = (email or "").strip()
        if not _EMAIL.match(email) or len(email) > 254:
            raise ValueError("invalid email address")
        row = self.conn.execute(
            "INSERT INTO pii.contact (email, name, quarantined) VALUES (%s, %s, true) "
            "ON CONFLICT (lower(email)) WHERE email IS NOT NULL DO NOTHING RETURNING contact_id", (email, name)).fetchone()
        if row:
            return row[0]
        return self.conn.execute("SELECT contact_id FROM pii.contact WHERE lower(email) = lower(%s)", (email,)).fetchone()[0]

    def withdraw(self, contact_id: uuid.UUID, channel: str, *, actor: str = "system",
                 at: Optional[datetime] = None) -> int:
        """Stop all current permissions for the contact on the channel. Returns how many were stopped."""
        if channel not in ALLOWED_CHANNELS:
            raise ValueError(f"channel must be one of {ALLOWED_CHANNELS}")
        n = self.conn.execute(
            "UPDATE pii.contact_permission SET withdrawn_at = COALESCE(%s, now()) "
            "WHERE contact_id = %s AND channel = %s::core.marketplace AND withdrawn_at IS NULL",
            (at, contact_id, channel)).rowcount
        if n:
            self._record("CONSENT_WITHDRAWN", actor, channel)
        return n

    def quarantine(self, contact_id: uuid.UUID) -> None:
        self.conn.execute("UPDATE pii.contact SET quarantined = true WHERE contact_id = %s", (contact_id,))

    def current_permission(self, contact_id: uuid.UUID, channel: str) -> Optional[uuid.UUID]:
        row = self.conn.execute(
            "SELECT id FROM pii.contact_permission WHERE contact_id = %s AND channel = %s::core.marketplace "
            "AND withdrawn_at IS NULL AND expires_at > now() ORDER BY captured_at DESC, created_at DESC LIMIT 1",
            (contact_id, channel)).fetchone()
        return row[0] if row else None

    def _record(self, action: str, actor: str, channel: str) -> None:
        if self._audit:
            self._audit(action, {"actor": actor, "marketplace": channel})

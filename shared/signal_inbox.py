"""The webhook signal inbox (S4-01, S4-02). A receiver writes signals here; an adapter's fetch_signals reads them, so the Scout keeps
pulling. Writes are idempotent per (marketplace, signal_ref): a provider that retries a webhook creates nothing new. Nothing personal is
stored. conn needs only the application role."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional, Protocol

from shared.models import Marketplace, Signal


class SignalSource(Protocol):
    def read(self, marketplace: str, since: datetime) -> List[Signal]: ...


class PgSignalInbox:
    def __init__(self, conn):
        self.conn = conn

    def write(self, signal: Signal, attributed_lead: Optional[uuid.UUID] = None) -> bool:
        """True if the signal is new, False if it was already there."""
        row = self.conn.execute(
            "INSERT INTO core.signal_inbox (marketplace, signal_ref, kind, product_external_id, occurred_at, attributed_lead_id) "
            "VALUES (%s::core.marketplace, %s, %s, %s, %s, %s) ON CONFLICT (marketplace, signal_ref) DO NOTHING RETURNING 1",
            (signal.marketplace.value, signal.signal_ref, signal.kind, signal.product_external_id, signal.occurred_at, attributed_lead)).fetchone()
        return row is not None

    def read(self, marketplace: str, since: datetime, limit: int = 1000) -> List[Signal]:
        rows = self.conn.execute(
            "SELECT signal_ref, kind, product_external_id, occurred_at FROM core.signal_inbox WHERE marketplace = %s::core.marketplace "
            "AND occurred_at >= %s ORDER BY occurred_at, signal_ref LIMIT %s", (marketplace, since, limit)).fetchall()
        mp = Marketplace(marketplace)
        return [Signal(signal_ref=r[0], kind=r[1], marketplace=mp, product_external_id=r[2], occurred_at=r[3]) for r in rows]

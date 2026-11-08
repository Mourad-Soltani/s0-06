"""A signal inbox that opens its own short-lived connection for every call (ADR-28). The webhook receiver runs many threads for a long time and
the Scout runs in a loop: one shared connection would be used from several threads and, once the database restarts, would stay broken for good.
A webhook arrives a few times a minute, so a connection per call costs nothing that matters. The inbox needs only the application role (app_rw)."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional

from shared.models import Signal
from shared.signal_inbox import PgSignalInbox


class DsnSignalInbox:
    def __init__(self, dsn: str):
        if not isinstance(dsn, str) or not dsn.startswith(("postgresql://", "postgres://")):
            raise ValueError("a postgresql:// URL is required")
        self._dsn = dsn

    def _connect(self):
        import psycopg                       # imported late: needs the database driver
        return psycopg.connect(self._dsn, autocommit=True)

    def write(self, signal: Signal, attributed_lead: Optional[uuid.UUID] = None) -> bool:
        with self._connect() as conn:
            return PgSignalInbox(conn).write(signal, attributed_lead)

    def read(self, marketplace: str, since: datetime, limit: int = 1000) -> List[Signal]:
        with self._connect() as conn:
            return PgSignalInbox(conn).read(marketplace, since, limit)

    def __repr__(self) -> str:               # the URL holds the password
        return "DsnSignalInbox()"

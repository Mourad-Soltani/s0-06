"""Postgres access for the worker. All SQL for claiming, acking and failing lives in migration 0002
(core.claim_outbox_events, outbox_ack, outbox_fail, outbox_reap_dead); this class only calls them."""
from __future__ import annotations

from typing import Callable, List, Optional

from shared.bus.events import Event

Handler = Callable[[object, Event], None]


class PgOutboxStore:
    """conn MUST be an autocommit connection. The claim/ack/fail calls then commit on their own, and the
    handler transaction in process() is opened explicitly with conn.transaction()."""

    def __init__(self, conn):
        self.conn = conn

    def claim(self, worker: str, limit: int, lease_seconds: float, max_attempts: int) -> List[Event]:
        rows = self.conn.execute(
            "SELECT id, event_id, event_type, marketplace::text, lead_id, payload_ref, payload, trace_id, attempts "
            "FROM core.claim_outbox_events(%s, %s, make_interval(secs => %s), %s)",
            (worker, limit, float(lease_seconds), max_attempts),
        ).fetchall()
        return [Event(*r) for r in rows]

    def process(self, consumer: str, event: Event, handler: Handler) -> bool:
        """Run one consumer's handler at most once per event. The receipt and the handler's own writes share
        one transaction: both commit or neither does. Returns False if this consumer already handled it."""
        with self.conn.transaction():
            got = self.conn.execute(
                "INSERT INTO core.consumer_receipt (consumer, event_id) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING RETURNING 1",
                (consumer, event.event_id),
            ).fetchone()
            if got is None:
                return False
            handler(self.conn, event)   # an exception rolls back the receipt too
        return True

    def ack(self, event_pk: int, worker: str) -> bool:
        return self.conn.execute("SELECT core.outbox_ack(%s, %s)", (event_pk, worker)).fetchone()[0]

    def fail(self, event_pk: int, worker: str, code: str, max_attempts: int, backoff_seconds: float) -> Optional[str]:
        return self.conn.execute(
            "SELECT core.outbox_fail(%s, %s, %s, %s, make_interval(secs => %s))",
            (event_pk, worker, code, max_attempts, float(backoff_seconds)),
        ).fetchone()[0]

    def reap(self, max_attempts: int) -> int:
        return self.conn.execute("SELECT core.outbox_reap_dead(%s)", (max_attempts,)).fetchone()[0]

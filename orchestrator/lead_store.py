"""Postgres side of the Scout and Qualifier. conn is an autocommit psycopg connection for the Scout (each call
commits on its own; ingest opens its own transaction), and the worker's transaction connection for the Qualifier."""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Optional

from orchestrator.scoring import Scores, SignalFeatures
from shared.bus.events import EventType, event_id_for

_QUEUE = ("NEEDS_REVIEW", "OUTREACH_DRAFTED", "APPROVAL_PENDING")


def signal_id_for(marketplace: str, signal_ref: str) -> str:
    """The lead's idempotency key: 64 hex characters (the database CHECK), unique per source and marketplace."""
    return hashlib.sha256(f"MARKETPLACE_SIGNAL|{marketplace}|{signal_ref}".encode("utf-8")).hexdigest()


class PgLeadStore:
    def __init__(self, conn):
        self.conn = conn

    def queue_depth(self) -> int:
        return self.conn.execute("SELECT count(*) FROM core.lead WHERE status::text = ANY(%s)", (list(_QUEUE),)).fetchone()[0]

    def new_leads_today(self) -> int:
        return self.conn.execute(
            "SELECT count(*) FROM core.lead WHERE created_at >= (date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')"
        ).fetchone()[0]

    def get_cursor(self, marketplace: str, account_id: str) -> Optional[datetime]:
        row = self.conn.execute("SELECT since FROM core.scout_cursor WHERE marketplace = %s::core.marketplace "
                                "AND account_id = %s", (marketplace, account_id)).fetchone()
        return row[0] if row else None

    def set_cursor(self, marketplace: str, account_id: str, since: datetime) -> None:
        """Monotonic: the cursor never moves backwards."""
        self.conn.execute(
            "INSERT INTO core.scout_cursor (marketplace, account_id, since) VALUES (%s::core.marketplace, %s, %s) "
            "ON CONFLICT (marketplace, account_id) DO UPDATE SET since = GREATEST(core.scout_cursor.since, EXCLUDED.since), "
            "updated_at = now()", (marketplace, account_id, since))

    def ingest(self, signal, marketplace: str) -> bool:
        from shared.bus.publisher import publish      # imported late: needs the database driver
        sid = signal_id_for(marketplace, signal.signal_ref)
        with self.conn.transaction():                 # the lead and its event exist together or not at all
            row = self.conn.execute(
                "INSERT INTO core.lead (source_type, source_marketplace, signal_ref, signal_id, status) "
                "VALUES ('MARKETPLACE_SIGNAL', %s::core.marketplace, %s, %s, 'SIGNAL_CAPTURED') "
                "ON CONFLICT (signal_id) DO NOTHING RETURNING id", (marketplace, signal.signal_ref, sid)).fetchone()
            if row is None:
                return False
            lead_id = row[0]
            features = SignalFeatures(kind=signal.kind, occurred_at=signal.occurred_at,
                                      product_external_id=signal.product_external_id, has_contact=False)
            publish(self.conn, EventType.SIGNAL_CAPTURED, event_id=event_id_for(EventType.SIGNAL_CAPTURED, sid),
                    marketplace=marketplace, lead_id=lead_id, payload=features.to_payload(), trace_id=lead_id)
        return True


class PgLeadRepo:
    def apply_scores(self, conn, lead_id, scores: Scores, composite_score: int, status: str) -> bool:
        row = conn.execute(
            "UPDATE core.lead SET icp_match_score=%s, pain_intensity=%s, authority_signal=%s, timeline_signal=%s, "
            "budget_signal=%s, composite_score=%s, confidence=%s, risk_flags=%s::text[], status=%s::core.lead_status, "
            "updated_at=now() WHERE id=%s AND status='SIGNAL_CAPTURED' RETURNING id",
            (scores.icp_match, scores.pain_intensity, scores.authority_signal, scores.timeline_signal,
             scores.budget_signal, composite_score, round(scores.confidence, 2), list(scores.risk_flags), status,
             lead_id)).fetchone()
        return row is not None

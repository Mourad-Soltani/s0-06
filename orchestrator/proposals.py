"""ProposalWriter (S3-01): turns a brief into a stored ActionProposal. One proposal per (lead, product, context): the
idempotency key is derived from them, so a retry returns the same proposal without a second model call, a second
body or a second event. The model call happens OUTSIDE the database transaction (a slow provider must not hold
locks); the body, the proposal, the audit event and the action.proposed event commit together. The body lives only in
pii.payload_store; the proposal carries its reference and its salted hash. The autonomy tier is 0 (draft only) until
the shadow-mode gate passes for the marketplace and role (spec 9.1, ADR-18), then 1; never 2 in v1.1.
conn needs the review_svc role: it writes message bodies."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Optional, Tuple

from orchestrator.copy_schema import DraftBrief
from orchestrator.copywriter import Copywriter
from orchestrator.router import Route
from shared.bus.events import EventType, event_id_for

_KEY_NAMESPACE = uuid.UUID("6f1c2a54-0b1e-4c1d-9a55-3e2f7d0a8b11")


def idempotency_key(brief: DraftBrief, product_id: Optional[str] = None) -> uuid.UUID:
    """product_id is the id the LEAD named when the draft was routed (S4-03): the listing can change with the fees, the lead and its product cannot."""
    return uuid.uuid5(_KEY_NAMESPACE, f"{brief.lead_id}|{product_id or brief.product.external_id}|{brief.context}")


def assign_tier(stats, now: datetime) -> int:
    return 1 if stats is not None and stats.passes_tier1_gate(now) else 0


def copy_audit_hook(writer) -> Callable[[str, dict], None]:
    def hook(action: str, d: dict) -> None:
        writer.record("copywriter", action, marketplace=d.get("marketplace"), lead_id=d.get("lead_id"),
                      autonomy_tier=d.get("tier"), outcome=d.get("outcome"))
    return hook


class ProposalWriter:
    def __init__(self, conn, copywriter: Copywriter, *, hasher: Callable[[uuid.UUID, dict], bytes],
                 stats_provider: Callable[[str], object], audit: Optional[Callable[[str, dict], None]] = None,
                 publish: Optional[Callable] = None, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.conn, self.copywriter, self._hasher, self._stats = conn, copywriter, hasher, stats_provider
        self._audit, self._publish, self._clock = audit, publish, clock

    def create(self, brief: DraftBrief, route: Optional[Route] = None) -> Tuple[uuid.UUID, bool]:
        """Returns (proposal_id, created). created is False when the proposal already existed. route (S4-03) is recorded in the payload:
        the marketplace whose checkout link the draft carries, and why. Without one the payload is exactly what it was before."""
        key = idempotency_key(brief, route.lead_product_id if route is not None else None)
        row = self.conn.execute("SELECT id FROM core.action_proposal WHERE idempotency_key = %s", (key,)).fetchone()
        if row:
            return row[0], False
        result = self.copywriter.draft(brief, trace_id=brief.lead_id)              # may spend; outside the transaction
        tier = assign_tier(self._stats(brief.marketplace), self._clock())
        payload = {"context": brief.context, "body": result.message, "source": result.source,
                   "product_external_id": brief.product.external_id,
                   "discount_pct": format(brief.discount_pct.normalize(), "f") if brief.discount_pct is not None else None,
                   "offer_code": brief.offer_code}
        if brief.contact_permission_id is not None:
            payload["consent_ref"] = str(brief.contact_permission_id)
        if route is not None:
            payload["route_marketplace"], payload["route_reason"] = route.marketplace, route.reason
        from psycopg.types.json import Json          # late import: the database driver
        with self.conn.transaction():
            ref = self.conn.execute("INSERT INTO pii.payload_store (lead_id, body) VALUES (%s, %s) RETURNING payload_id",
                                    (brief.lead_id, Json(payload))).fetchone()[0]
            created = self.conn.execute(
                "INSERT INTO core.action_proposal (lead_id, role, marketplace, channel, action_type, payload_ref, payload_hash, "
                "autonomy_tier, confidence, contact_permission_id, idempotency_key, audit_trace_id) "
                "VALUES (%s, 'COPYWRITER', %s::core.marketplace, %s::core.marketplace, 'SEND_MESSAGE', %s, %s::bytea, %s, %s, %s, %s, %s) "
                "ON CONFLICT (idempotency_key) DO NOTHING RETURNING id",
                (brief.lead_id, brief.marketplace, brief.channel, ref, self._hasher(brief.lead_id, payload), tier,
                 round(result.confidence, 2), brief.contact_permission_id, key, brief.lead_id)).fetchone()
            if created is None:                                                    # lost a race: keep theirs, drop our body
                self.conn.execute("DELETE FROM pii.payload_store WHERE payload_id = %s", (ref,))
                return self.conn.execute("SELECT id FROM core.action_proposal WHERE idempotency_key = %s", (key,)).fetchone()[0], False
            proposal_id = created[0]
            if self._audit:
                self._audit("COPY_DRAFTED", {"marketplace": brief.marketplace, "lead_id": brief.lead_id, "tier": tier,
                                             "outcome": "OK" if result.fallback_reason is None else f"FALLBACK_{result.fallback_reason}"})
            publish = self._publish or _default_publish()
            publish(self.conn, EventType.ACTION_PROPOSED, event_id=event_id_for(EventType.ACTION_PROPOSED, proposal_id),
                    marketplace=brief.marketplace, lead_id=brief.lead_id, payload={"proposal_id": str(proposal_id), "tier": tier},
                    trace_id=brief.lead_id)
        return proposal_id, True


def _default_publish():
    from shared.bus.publisher import publish          # imported late: needs the database driver
    return publish

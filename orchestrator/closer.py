"""The Closer (S3-03): when a draft is approved, it adds the checkout link carrying the lead's id and sends the message.

Order of events, and why:
 1. Read the approved proposal under a row lock and decide, in one short transaction: skip it if it is not APPROVED (a replay), do
    not send a Tier 0 draft (shadow mode), build the final text, check the final text with the Guard again (only the link changed,
    but nothing leaves unchecked), then CLAIM it by moving it to SENDING. The database send gate fires on that move: consent, the
    Guard's verdict and the kill switch are enforced there. That transaction COMMITS before anything is sent.
 2. Send, outside any transaction, through the channel adapter, with the proposal's idempotency key.
 3. Record the result in a second short transaction: SENT (with the audit event and action.sent), or back to APPROVED when the
    send certainly did not happen (rate limited, auth expired: a later sweep retries), or FAILED when it is not known.
A crash after step 1 leaves the proposal in SENDING: it is never sent a second time (at most once), and BL-04 reconciles it.
The Closer uses its OWN connection, never the bus worker's transaction: a rollback there must not be able to undo a claim after an
email has gone out. The link text: every occurrence of the product's plain checkout link becomes the attributed link; if the draft has
none, a fixed line carrying it is appended. Only the link changes; the rest of the approved text is untouched."""
from __future__ import annotations

import hashlib
import re
import uuid
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Callable, List, Mapping, Optional, Tuple

from adapters.errors import AuthExpired, RateLimited
from orchestrator.guard import ComplianceGuard, _discount
from orchestrator.guard_rules import PASS, DraftFacts
from shared.bus.events import Event, EventType, error_code, event_id_for
from shared.models import ActionType, Marketplace, ProposedAction

CONSUMER = "closer"
SENT, SHADOW, NOT_APPROVED, KILLED = "SENT", "SHADOW_NOT_SENT", "SKIPPED_NOT_APPROVED", "KILL_SWITCH_ACTIVE"
APPENDED_LINE = "Here is the link: {url}"


class CloserError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class CloserConfig:
    ref_param: str = "ref"                       # the query parameter that carries the lead id


@dataclass(frozen=True)
class CloseResult:
    proposal_id: uuid.UUID
    outcome: str


def attribute_link(body: str, plain_url: str, attributed_url: str) -> Tuple[str, str]:
    """(final_body, how). how is ALREADY_ATTRIBUTED, ATTRIBUTED or APPENDED."""
    if attributed_url in body:
        return body, "ALREADY_ATTRIBUTED"
    pattern = re.escape(plain_url) + r"(?![\w/%?&#=+~-])"
    replaced, n = re.subn(pattern, lambda m: attributed_url, body)
    if n:
        return replaced, "ATTRIBUTED"
    return body.rstrip() + "\n\n" + APPENDED_LINE.format(url=attributed_url), "APPENDED"


def _default_audit(conn) -> Callable[[str, dict], None]:
    from shared.audit import AuditWriter           # imported late
    writer = AuditWriter(conn)
    return lambda action, d: writer.record("closer", action, marketplace=d.get("marketplace"), lead_id=d.get("lead_id"),
                                           autonomy_tier=d.get("tier"), outcome=d.get("outcome"))


def _default_publish():
    from shared.bus.publisher import publish          # imported late: needs the database driver
    return publish


class Closer:
    def __init__(self, connect: Callable[[], AbstractContextManager], product_adapter, channel_adapters: Mapping[str, object],
                 guard: ComplianceGuard, *, config: Optional[CloserConfig] = None, audit_factory: Optional[Callable] = _default_audit,
                 publish: Optional[Callable] = None):
        self._connect, self.products, self.channels, self.guard = connect, product_adapter, dict(channel_adapters), guard
        self.config, self._audit_factory, self._publish = config or CloserConfig(), audit_factory, publish

    # ------------------------------------------------------------------ the bus and the sweep
    def register(self, worker) -> None:
        worker.subscribe(EventType.ACTION_APPROVED, CONSUMER, self.handle)

    def handle(self, conn, event: Event) -> None:
        """conn (the worker's transaction) is deliberately NOT used: see the module docstring."""
        raw = event.payload.get("proposal_id") if isinstance(event.payload, dict) else None
        self.process(uuid.UUID(str(raw)))

    def sweep(self, limit: int = 50) -> List[CloseResult]:
        """Process approved proposals that are waiting: after a kill switch is released, a rate limit passes, or a missed event."""
        with self._connect() as conn:
            ids = [r[0] for r in conn.execute("SELECT id FROM core.action_proposal WHERE status = 'APPROVED' AND autonomy_tier >= 1 "
                                              "ORDER BY updated_at, id LIMIT %s", (limit,)).fetchall()]
        return [self.process(i) for i in ids]

    # ------------------------------------------------------------------ one proposal
    def process(self, proposal_id: uuid.UUID) -> CloseResult:
        with self._connect() as conn:
            audit = self._audit_factory(conn) if self._audit_factory else (lambda a, d: None)
            prepared = self._prepare(conn, proposal_id, audit)
            if isinstance(prepared, CloseResult):
                return prepared
            ctx = prepared
            try:
                sent = ctx["adapter"].send_reply(str(ctx["contact_id"]), ctx["final_body"], ctx["key"])      # outside any transaction
            except (RateLimited, AuthExpired) as exc:                          # certainly not sent: back in line for the next sweep
                return self._finish(conn, audit, ctx, "APPROVED", f"RETRY_LATER_{error_code(exc)}")
            except Exception as exc:  # noqa: BLE001 - whether it left is unknown: never retry blindly
                return self._finish(conn, audit, ctx, "FAILED", f"SEND_FAILED_{error_code(exc)}")
            return self._finish(conn, audit, ctx, "SENT", "OK", sent=sent)

    def _prepare(self, conn, proposal_id, audit):
        with conn.transaction():
            row = conn.execute(
                "SELECT p.lead_id, p.marketplace::text, p.channel::text, p.action_type::text, p.autonomy_tier, p.status::text, p.idempotency_key, "
                "p.contact_permission_id, s.body, l.contact_id, l.product_external_id FROM core.action_proposal p JOIN core.lead l ON l.id = p.lead_id "
                "LEFT JOIN pii.payload_store s ON s.payload_id = p.payload_ref WHERE p.id = %s FOR UPDATE OF p", (proposal_id,)).fetchone()
            if row is None:
                raise CloserError("NOT_FOUND")
            lead_id, marketplace, channel, action_type, tier, status, key, permission, payload, contact_id, lead_product = row
            if status != "APPROVED":
                return CloseResult(proposal_id, NOT_APPROVED)                    # a replay, or decided meanwhile
            note = lambda outcome: audit("CLOSER_" + ("SHADOW" if outcome == SHADOW else "FAILED"),   # noqa: E731
                                         {"marketplace": marketplace, "lead_id": lead_id, "tier": tier, "outcome": outcome})
            if tier == 0:
                note(SHADOW)
                return CloseResult(proposal_id, SHADOW)                            # shadow mode: approved for the gate, never sent

            def fail(outcome: str) -> CloseResult:
                conn.execute("UPDATE core.action_proposal SET status = 'FAILED', updated_at = now() WHERE id = %s AND status = 'APPROVED'", (proposal_id,))
                note(outcome)
                return CloseResult(proposal_id, outcome)

            payload = payload if isinstance(payload, dict) else {}
            product_id, adapter = payload.get("product_external_id") or lead_product, self.channels.get(channel)
            if adapter is None:
                return fail("NO_CHANNEL_ADAPTER")
            if not product_id or contact_id is None or not isinstance(payload.get("body"), str):
                return fail("NO_PRODUCT" if not product_id else "NOTHING_TO_SEND")
            plain = self.products.build_checkout_url(product_id, {})
            attributed = self.products.build_checkout_url(product_id, {self.config.ref_param: str(lead_id)})
            final_body, how = attribute_link(payload["body"], plain, attributed)
            live = permission is not None and conn.execute(
                "SELECT 1 FROM pii.contact_permission cp JOIN pii.contact c ON c.contact_id = cp.contact_id WHERE cp.id = %s "
                "AND cp.withdrawn_at IS NULL AND cp.expires_at > now() AND NOT c.quarantined", (permission,)).fetchone() is not None
            facts = DraftFacts(context=str(payload.get("context", "")), body=final_body, has_current_permission=live,
                               discount_pct=_discount(payload.get("discount_pct")))
            action = ProposedAction(action_type=ActionType(action_type), marketplace=Marketplace(marketplace),
                                    payload={**payload, "body": final_body}, execution_plan=tuple(payload.get("execution_plan", ())))
            result = self.guard.evaluate(facts, action)
            if result.verdict != PASS:                                             # the final text did not pass: nothing is sent
                return fail("FINAL_CHECK_" + (result.codes[0] if result.codes else "FAILED"))
            try:
                with conn.transaction():                                           # a savepoint: the gate's refusal must not poison this transaction
                    conn.execute("UPDATE core.action_proposal SET status = 'SENDING', updated_at = now() WHERE id = %s AND status = 'APPROVED'", (proposal_id,))
            except Exception as exc:  # noqa: BLE001
                state = getattr(exc, "sqlstate", None)
                if state == "CP003":
                    note_killed = {"marketplace": marketplace, "lead_id": lead_id, "tier": tier, "outcome": KILLED}
                    audit("CLOSER_FAILED", note_killed)
                    return CloseResult(proposal_id, KILLED)                       # stays APPROVED: the sweep sends it after the release
                if state == "CP002":
                    return fail("CONSENT_NOT_CURRENT")
                if state == "CP001":
                    return fail("GATE_REFUSED")
                raise
            return {"adapter": adapter, "contact_id": contact_id, "final_body": final_body, "key": key, "proposal_id": proposal_id,
                    "lead_id": lead_id, "marketplace": marketplace, "tier": tier, "how": how}

    def _finish(self, conn, audit, ctx, status: str, outcome: str, sent=None) -> CloseResult:
        with conn.transaction():
            conn.execute("UPDATE core.action_proposal SET status = %s::core.proposal_status, updated_at = now() WHERE id = %s AND status = 'SENDING'",
                         (status, ctx["proposal_id"]))
            audit("CLOSER_SENT" if status == "SENT" else "CLOSER_FAILED",
                  {"marketplace": ctx["marketplace"], "lead_id": ctx["lead_id"], "tier": ctx["tier"], "outcome": outcome})
            if status == "SENT":
                ref = hashlib.sha256(str(sent.external_message_id).encode("utf-8")).hexdigest()[:32]       # provider ids may contain '@'
                publish = self._publish or _default_publish()
                publish(conn, EventType.ACTION_SENT, event_id=event_id_for(EventType.ACTION_SENT, ctx["proposal_id"]), marketplace=ctx["marketplace"],
                        lead_id=ctx["lead_id"], payload={"proposal_id": str(ctx["proposal_id"]), "link": ctx["how"], "provider_ref": ref,
                                                         "deduplicated": bool(sent.deduplicated)}, trace_id=ctx["lead_id"])
        return CloseResult(ctx["proposal_id"], SENT if status == "SENT" else outcome)

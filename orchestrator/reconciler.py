"""The reconciliation job (BL-04): settles proposals stuck in SENDING. The Closer commits its claim before it sends, so a crash between
the send and the recording leaves a proposal in SENDING with the message possibly sent. This job asks the PROVIDER what happened, by the
proposal's idempotency key, and only then moves the proposal:
  * the provider accepted a message with that key  -> SENT (with its provider reference, audit event and action.sent), no email sent;
  * the provider says it never did (authoritative) -> back to APPROVED, so the Closer's sweep sends it, once, with the same key;
  * the provider cannot answer (lookup unsupported, rate limited, authentication expired, network, anything else) -> it STAYS in
    SENDING. "I could not find out" is never read as "not sent". It is audited, an alert hook is called, and its clock is reset so it
    is looked at again after another stale_after, not on every run.
It NEVER sends anything. A proposal is only touched if it is still SENDING and still stale at the moment of the update, so a Closer
that finishes in the meantime wins. It reads no message body and no address, so the application role is enough. Run it on a schedule;
stale_after must be longer than the slowest legitimate send."""
from __future__ import annotations

import hashlib
import uuid
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable, List, Mapping, Optional

from shared.bus.events import EventType, error_code, event_id_for

RECOVERED_SENT, REQUEUED, RESOLVED_ELSEWHERE = "RECOVERED_SENT", "REQUEUED", "RESOLVED_ELSEWHERE"


@dataclass(frozen=True)
class ReconcileResult:
    proposal_id: uuid.UUID
    outcome: str
    alerted: bool = False


def _default_audit(conn) -> Callable[[str, dict], None]:
    from shared.audit import AuditWriter           # imported late
    writer = AuditWriter(conn)
    return lambda action, d: writer.record("reconciler", action, marketplace=d.get("marketplace"), lead_id=d.get("lead_id"),
                                           autonomy_tier=d.get("tier"), outcome=d.get("outcome"))


def _default_publish():
    from shared.bus.publisher import publish          # imported late: needs the database driver
    return publish


class Reconciler:
    def __init__(self, connect: Callable[[], AbstractContextManager], channel_adapters: Mapping[str, object], *,
                 stale_after: timedelta = timedelta(minutes=15), audit_factory: Optional[Callable] = _default_audit,
                 publish: Optional[Callable] = None, alert: Optional[Callable[[uuid.UUID, str], None]] = None):
        if not isinstance(stale_after, timedelta) or stale_after < timedelta(minutes=1):
            raise ValueError("stale_after must be at least one minute: a send in flight is not stuck")
        self._connect, self.channels, self.stale_after = connect, dict(channel_adapters), stale_after
        self._audit_factory, self._publish, self._alert = audit_factory, publish, alert

    def run(self, limit: int = 50) -> List[ReconcileResult]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be 1 to 500")
        with self._connect() as conn:
            stuck = conn.execute(
                "SELECT id, lead_id, marketplace::text, channel::text, autonomy_tier, idempotency_key FROM core.action_proposal "
                "WHERE status = 'SENDING' AND autonomy_tier >= 1 AND updated_at < now() - %s ORDER BY updated_at, id LIMIT %s",
                (self.stale_after, limit)).fetchall()
            audit = self._audit_factory(conn) if self._audit_factory else (lambda a, d: None)
            return [self._settle(conn, audit, row) for row in stuck]

    def _settle(self, conn, audit, row) -> ReconcileResult:
        pid, lead_id, marketplace, channel, tier, key = row
        facts = {"marketplace": marketplace, "lead_id": lead_id, "tier": tier}
        adapter = self.channels.get(channel)
        try:
            if adapter is None:
                raise LookupError("no adapter")
            found = adapter.find_send(key)                                      # outside any transaction: this is a call to the provider
        except Exception as exc:  # noqa: BLE001 - whatever the reason, "could not find out" is not "not sent"
            return self._unresolved(conn, audit, pid, facts, "NO_CHANNEL_ADAPTER" if adapter is None else error_code(exc))
        if found is None:
            return self._move(conn, audit, pid, facts, "APPROVED", None, "RECONCILER_REQUEUED", REQUEUED)
        ref = hashlib.sha256(str(found.external_message_id).encode("utf-8")).hexdigest()[:32]       # provider ids can contain '@'
        return self._move(conn, audit, pid, facts, "SENT", ref, "RECONCILER_SENT", RECOVERED_SENT, lead_id=lead_id)

    def _move(self, conn, audit, pid, facts, status, ref, action, outcome, lead_id=None) -> ReconcileResult:
        with conn.transaction():
            n = conn.execute(
                "UPDATE core.action_proposal SET status = %s::core.proposal_status, provider_ref = COALESCE(%s, provider_ref), updated_at = now() "
                "WHERE id = %s AND status = 'SENDING' AND updated_at < now() - %s", (status, ref, pid, self.stale_after)).rowcount
            if n == 0:
                return ReconcileResult(pid, RESOLVED_ELSEWHERE)                 # the Closer finished, or another reconciler got there first
            audit(action, {**facts, "outcome": "OK"})
            if status == "SENT":
                publish = self._publish or _default_publish()
                publish(conn, EventType.ACTION_SENT, event_id=event_id_for(EventType.ACTION_SENT, pid), marketplace=facts["marketplace"],
                        lead_id=lead_id, payload={"proposal_id": str(pid), "link": "UNKNOWN", "provider_ref": ref, "deduplicated": False,
                                                  "recovered": True}, trace_id=lead_id)
        return ReconcileResult(pid, outcome)

    def _unresolved(self, conn, audit, pid, facts, code: str) -> ReconcileResult:
        outcome = f"UNRESOLVED_{code}"
        with conn.transaction():
            n = conn.execute("UPDATE core.action_proposal SET updated_at = now() WHERE id = %s AND status = 'SENDING' AND updated_at < now() - %s",
                             (pid, self.stale_after)).rowcount                  # resets the clock: the next look is after another stale_after
            if n == 0:
                return ReconcileResult(pid, RESOLVED_ELSEWHERE)
            audit("RECONCILER_UNRESOLVED", {**facts, "outcome": code})
        alerted = False
        if self._alert:
            try:
                self._alert(pid, outcome)
                alerted = True
            except Exception:  # noqa: BLE001 - a broken alert channel must not stop the other proposals being settled
                alerted = False
        return ReconcileResult(pid, outcome, alerted)

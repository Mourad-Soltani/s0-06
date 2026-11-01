"""Compliance Guard as a bus consumer (S3-01): on every action.proposed it evaluates the proposal, so a new draft is
judged without anyone calling the Guard by hand. Runs inside the worker's per-consumer transaction. A proposal that is
no longer pending (already decided) is acknowledged quietly; a missing or malformed one raises, so it is retried and
then dead-lettered."""
from __future__ import annotations

import uuid
from typing import Callable, Optional

from orchestrator.guard import NOT_PENDING, ComplianceGuard, GuardError, GuardService, guard_audit_hook
from shared.bus.events import Event, EventType

CONSUMER = "compliance_guard"


def _default_audit(conn):
    from shared.audit import AuditWriter           # imported late
    return guard_audit_hook(AuditWriter(conn))


class GuardConsumer:
    def __init__(self, guard: ComplianceGuard, audit_factory: Optional[Callable] = _default_audit, publish: Optional[Callable] = None):
        self.guard, self._audit_factory, self._publish = guard, audit_factory, publish

    def register(self, worker) -> None:
        worker.subscribe(EventType.ACTION_PROPOSED, CONSUMER, self.handle)

    def handle(self, conn, event: Event) -> None:
        raw = event.payload.get("proposal_id") if isinstance(event.payload, dict) else None
        proposal_id = uuid.UUID(str(raw))                                  # ValueError on garbage: retried, then dead
        audit = self._audit_factory(conn) if self._audit_factory else None
        try:
            GuardService(conn, self.guard, audit=audit, publish=self._publish).evaluate(proposal_id)
        except GuardError as exc:
            if exc.code != NOT_PENDING:
                raise

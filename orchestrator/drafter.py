"""Drafting consumer (BL-03): on lead.qualified, write a draft. It checks what a draft needs (a contact, a product, a current
consent, a product the marketplace can still show with a checkout link), builds the structured brief from the marketplace's own
data, and hands it to the proposal writer, which stores one proposal per lead, product and context. That publishes
action.proposed, which the Compliance Guard consumes, and the judged draft then waits in the review queue. A lead that cannot
be drafted is skipped with a recorded reason, never silently. Transient trouble (the marketplace is rate limited) is raised so
the bus retries; a missing product is permanent and is skipped. Product details always come from the adapter, never the event."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from adapters.errors import NotFound
from orchestrator.copy_schema import DraftBrief, ProductFacts
from shared.bus.events import Event, EventType
from shared.consent import ConsentService

CONSUMER = "drafter"
SKIPS = ("NO_CONTACT", "NO_PRODUCT", "NO_CURRENT_PERMISSION", "PRODUCT_NOT_FOUND", "PRODUCT_INVALID", "NO_CHECKOUT_URL")


@dataclass(frozen=True)
class DraftingConfig:
    marketplace: str = "EMAIL"
    channel: str = "EMAIL"
    context: str = "consented_outreach"
    signal_kind: str = "QUALIFIED_LEAD"          # a neutral label: the event does not carry the original signal


def _default_audit(conn) -> Callable[[str, dict], None]:
    from shared.audit import AuditWriter           # imported late
    writer = AuditWriter(conn)
    return lambda action, d: writer.record("drafter", action, marketplace=d.get("marketplace"), lead_id=d.get("lead_id"), outcome=d.get("outcome"))


def default_writer_factory(copywriter) -> Callable:
    """Builds a ProposalWriter on the worker's own connection, so the draft commits with the event that caused it."""
    def factory(conn):
        from orchestrator.proposals import ProposalWriter, copy_audit_hook      # imported late: they need the database driver
        from orchestrator.review_service import ReviewQueue
        from shared.audit import AuditWriter
        writer, queue = AuditWriter(conn), ReviewQueue(conn)
        return ProposalWriter(conn, copywriter, hasher=writer.hash_payload, stats_provider=lambda mp: queue.shadow_stats(mp, "COPYWRITER"),
                              audit=copy_audit_hook(writer))
    return factory


class DraftingConsumer:
    def __init__(self, writer_factory: Callable, product_adapter, *, config: Optional[DraftingConfig] = None,
                 audit_factory: Optional[Callable] = _default_audit):
        self.writer_factory, self.adapter, self.config, self._audit_factory = writer_factory, product_adapter, config or DraftingConfig(), audit_factory

    def register(self, worker) -> None:
        worker.subscribe(EventType.LEAD_QUALIFIED, CONSUMER, self.handle)

    def handle(self, conn, event: Event) -> None:
        if event.lead_id is None:
            raise ValueError("lead.qualified without a lead_id")
        row = conn.execute("SELECT contact_id, product_external_id FROM core.lead WHERE id = %s", (event.lead_id,)).fetchone()
        if row is None:
            raise ValueError("lead not found")                                 # retried, then dead-lettered
        contact_id, product_id = row
        if contact_id is None:
            return self._skip(conn, event, "NO_CONTACT")
        if not product_id:
            return self._skip(conn, event, "NO_PRODUCT")
        permission = ConsentService(conn).current_permission(contact_id, self.config.channel)
        if permission is None:
            return self._skip(conn, event, "NO_CURRENT_PERMISSION")
        try:
            product = self.adapter.get_product(product_id)
        except NotFound:
            return self._skip(conn, event, "PRODUCT_NOT_FOUND")
        if not product.checkout_url:
            return self._skip(conn, event, "NO_CHECKOUT_URL")
        try:
            brief = DraftBrief(lead_id=event.lead_id, marketplace=self.config.marketplace, channel=self.config.channel, context=self.config.context,
                               product=ProductFacts(product.external_id, product.title, product.price.amount_minor, product.price.currency, product.checkout_url),
                               signal_kind=self.config.signal_kind, contact_permission_id=permission)
        except ValueError:
            return self._skip(conn, event, "PRODUCT_INVALID")
        self.writer_factory(conn).create(brief)

    def _skip(self, conn, event: Event, code: str) -> None:
        if self._audit_factory:
            self._audit_factory(conn)("DRAFT_SKIPPED", {"marketplace": self.config.marketplace, "lead_id": event.lead_id, "outcome": code})

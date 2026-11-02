"""Drafting consumer (BL-03) with fakes: every skip, the brief it builds, retries. No database. Names DR*."""
import uuid
from datetime import datetime, timezone

import pytest

from adapters.errors import NotFound, RateLimited
from orchestrator.drafter import CONSUMER, SKIPS, DraftingConfig, DraftingConsumer
from shared.bus.events import Event, EventType
from shared.models import ListingStatus, Money, ProductSnapshot

LEAD, CONTACT, PERM = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


class Cur:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class Conn:
    def __init__(self, lead=(CONTACT, "gr-1"), permission=(PERM,)):
        self.lead, self.permission, self.calls = lead, permission, []

    def execute(self, sql, params=()):
        sql = " ".join(sql.split())
        self.calls.append(sql)
        if "FROM core.lead" in sql:
            return Cur(self.lead)
        if "FROM pii.contact_permission" in sql:
            return Cur(self.permission)
        raise AssertionError(sql)


def snapshot(**kw):
    base = dict(external_id="gr-1", title="Budget Planner", description="d", price=Money(amount_minor=1900, currency="USD"),
                status=ListingStatus.ACTIVE, checkout_url="https://shop.example.test/l/gr-1", last_synced_at=NOW)
    base.update(kw)
    return ProductSnapshot(**base)


class Adapter:
    def __init__(self, result=None, exc=None):
        self.result, self.exc, self.asked = result if result is not None else snapshot(), exc, []

    def get_product(self, pid):
        self.asked.append(pid)
        if self.exc:
            raise self.exc
        return self.result


class Writer:
    def __init__(self):
        self.briefs = []

    def create(self, brief):
        self.briefs.append(brief)
        return uuid.uuid4(), True


def event(lead=LEAD):
    return Event(id=1, event_id=uuid.uuid4(), event_type=EventType.LEAD_QUALIFIED, marketplace="EMAIL", lead_id=lead, payload_ref=None,
                 payload={"composite": 75}, trace_id=lead, attempts=1)


def consumer(adapter=None, audit=True):
    writer, seen = Writer(), []
    c = DraftingConsumer(lambda conn: writer, adapter or Adapter(), audit_factory=(lambda conn: (lambda a, d: seen.append((a, d)))) if audit else None)
    return c, writer, seen


def test_DR1_a_qualified_lead_with_a_contact_a_consent_and_a_product_gets_a_brief_from_the_marketplaces_own_data():
    adapter = Adapter()
    c, writer, seen = consumer(adapter)
    c.handle(Conn(), event())
    (b,) = writer.briefs
    assert (b.lead_id, b.marketplace, b.channel, b.context, b.signal_kind, b.contact_permission_id) == (LEAD, "EMAIL", "EMAIL", "consented_outreach", "QUALIFIED_LEAD", PERM)
    assert (b.product.external_id, b.product.title, b.product.price_minor, b.product.currency, b.product.checkout_url) == (
        "gr-1", "Budget Planner", 1900, "USD", "https://shop.example.test/l/gr-1")
    assert adapter.asked == ["gr-1"] and seen == []


@pytest.mark.parametrize("conn,adapter,code", [
    (Conn(lead=(None, "gr-1")), None, "NO_CONTACT"), (Conn(lead=(CONTACT, None)), None, "NO_PRODUCT"), (Conn(lead=(CONTACT, "")), None, "NO_PRODUCT"),
    (Conn(permission=None), None, "NO_CURRENT_PERMISSION"), (Conn(), Adapter(exc=NotFound()), "PRODUCT_NOT_FOUND"),
    (Conn(), Adapter(snapshot(checkout_url=None)), "NO_CHECKOUT_URL"), (Conn(), Adapter(snapshot(title="<>``")), "PRODUCT_INVALID"),
    (Conn(), Adapter(snapshot(checkout_url="http://insecure.test/x")), "PRODUCT_INVALID")])
def test_DR2_a_lead_that_cannot_be_drafted_is_skipped_with_a_recorded_reason_and_no_draft(conn, adapter, code):
    c, writer, seen = consumer(adapter)
    c.handle(conn, event())
    assert writer.briefs == [] and seen == [("DRAFT_SKIPPED", {"marketplace": "EMAIL", "lead_id": LEAD, "outcome": code})] and code in SKIPS


def test_DR3_a_lead_that_does_not_exist_or_an_event_without_one_raises_so_the_bus_retries_then_gives_up():
    c, writer, _ = consumer()
    with pytest.raises(ValueError):
        c.handle(Conn(lead=None), event())
    with pytest.raises(ValueError):
        c.handle(Conn(), event(lead=None))
    assert writer.briefs == []


def test_DR4_trouble_at_the_marketplace_is_raised_for_a_retry_not_turned_into_a_skip():
    c, writer, seen = consumer(Adapter(exc=RateLimited(3.0)))
    with pytest.raises(RateLimited):
        c.handle(Conn(), event())
    assert writer.briefs == [] and seen == []


def test_DR5_skips_are_not_audited_when_no_audit_is_configured_and_the_consumer_registers_itself():
    c, writer, seen = consumer(audit=False)
    c.handle(Conn(lead=(None, "gr-1")), event())
    assert seen == [] and writer.briefs == []

    class W:
        subs = []

        def subscribe(self, etype, name, handler):
            self.subs.append((etype, name, handler))

    w = W()
    c.register(w)
    assert w.subs == [(EventType.LEAD_QUALIFIED, CONSUMER, c.handle)] and CONSUMER == "drafter"


def test_DR6_the_configuration_is_respected():
    adapter = Adapter()
    writer = Writer()
    c = DraftingConsumer(lambda conn: writer, adapter, config=DraftingConfig(signal_kind="SUBSCRIBER"), audit_factory=None)
    c.handle(Conn(), event())
    assert writer.briefs[0].signal_kind == "SUBSCRIBER"

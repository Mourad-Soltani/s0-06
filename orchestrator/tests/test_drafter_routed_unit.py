"""The drafting consumer with routing (S4-03), over fakes: the ranking is walked, each adapter is asked only for its own listing, a listing that
cannot be shown is passed over, marketplace trouble is raised for a retry, and without a router nothing changes. No database. Names DRR*."""
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from adapters.errors import AuthExpired, NotFound, RateLimited
from orchestrator.drafter import SKIPS, DraftingConsumer
from orchestrator.router import FALLBACK, LEAD_ORIGIN, MOST_PROFITABLE, Route, RouteChoice, RoutePlan, Router
from shared.bus.events import Event, EventType

LEAD, CONTACT, PERM = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
GR, LS, PH = ("GUMROAD", "gr-1"), ("LEMON_SQUEEZY", "ls-1"), ("PAYHIP", "RGsF")


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


def snap(external_id, url="auto", title="Budget Planner", price=1900):
    url = f"https://shop.example.test/{external_id}" if url == "auto" else url
    return SimpleNamespace(external_id=external_id, title=title, price=SimpleNamespace(amount_minor=price, currency="USD"), checkout_url=url)


class Adapter:
    def __init__(self, result=None, exc=None):
        self.result, self.exc, self.asked = result, exc, []

    def get_product(self, pid):
        self.asked.append(pid)
        if self.exc:
            raise self.exc
        return self.result if self.result is not None else snap(pid)


class FakeRouter:
    def __init__(self, plan):
        self.result, self.asked = plan, []

    def plan(self, conn, lead_id, product_id):
        self.asked.append((lead_id, product_id))
        return self.result


class Writer:
    def __init__(self):
        self.calls = []

    def create(self, brief, route=None):
        self.calls.append((brief, route))
        return uuid.uuid4(), True


def choices(*items, first=MOST_PROFITABLE):
    return RoutePlan(choices=tuple(RouteChoice(m, e, first if i == 0 else FALLBACK, None) for i, (m, e) in enumerate(items)))


def event(lead=LEAD):
    return Event(id=1, event_id=uuid.uuid4(), event_type=EventType.LEAD_QUALIFIED, marketplace="EMAIL", lead_id=lead, payload_ref=None,
                 payload={"composite": 75}, trace_id=lead, attempts=1)


def build(plan, adapters, *, audit=True):
    writer, seen, router = Writer(), [], FakeRouter(plan)
    c = DraftingConsumer(lambda conn: writer, Adapter(), router=router, product_adapters=adapters,
                         audit_factory=(lambda conn: (lambda a, d: seen.append((a, d)))) if audit else None)
    return c, writer, seen, router


def test_DRR1_the_draft_is_built_from_the_first_choice_and_records_the_route_with_the_leads_own_product_id():
    gum, ls = Adapter(), Adapter()
    c, writer, seen, router = build(choices(LS, GR), {"GUMROAD": gum, "LEMON_SQUEEZY": ls})
    c.handle(Conn(), event())
    (brief, route), = writer.calls
    assert (brief.product.external_id, brief.product.checkout_url, brief.marketplace, brief.channel) == ("ls-1", "https://shop.example.test/ls-1", "EMAIL", "EMAIL")
    assert route == Route("LEMON_SQUEEZY", MOST_PROFITABLE, "gr-1")                 # the lead named gr-1; the draft is about ls-1
    assert ls.asked == ["ls-1"] and gum.asked == [] and router.asked == [(LEAD, "gr-1")]
    assert seen == [("ROUTE_DECIDED", {"marketplace": "EMAIL", "lead_id": LEAD, "outcome": MOST_PROFITABLE})]


def test_DRR2_each_marketplaces_adapter_is_asked_only_for_its_own_listing():
    a, b = Adapter(), Adapter()
    c, writer, _, _ = build(choices(GR, LS, first=LEAD_ORIGIN), {"GUMROAD": a, "LEMON_SQUEEZY": b})
    c.handle(Conn(), event())
    assert a.asked == ["gr-1"] and b.asked == [] and writer.calls[0][1].reason == LEAD_ORIGIN


@pytest.mark.parametrize("first_adapter", [None, Adapter(exc=NotFound()), Adapter(snap("gr-1", url=None)), Adapter(snap("gr-1", title="<>``")),
                                           Adapter(snap("gr-1", url="http://insecure.test/x"))])
def test_DRR3_a_listing_that_cannot_be_shown_is_passed_over_and_the_next_one_is_used_as_a_fallback(first_adapter):
    second = Adapter()
    adapters = {"LEMON_SQUEEZY": second, **({"GUMROAD": first_adapter} if first_adapter is not None else {})}
    c, writer, seen, _ = build(choices(GR, LS, first=LEAD_ORIGIN), adapters)
    c.handle(Conn(), event())
    (brief, route), = writer.calls
    assert brief.product.external_id == "ls-1" and route == Route("LEMON_SQUEEZY", FALLBACK, "gr-1")
    assert seen == [("ROUTE_DECIDED", {"marketplace": "EMAIL", "lead_id": LEAD, "outcome": FALLBACK})]


@pytest.mark.parametrize("adapters,code", [
    ({}, "PRODUCT_NOT_FOUND"), ({"GUMROAD": Adapter(exc=NotFound()), "LEMON_SQUEEZY": Adapter(exc=NotFound())}, "PRODUCT_NOT_FOUND"),
    ({"GUMROAD": Adapter(snap("gr-1", url=None)), "LEMON_SQUEEZY": Adapter(exc=NotFound())}, "NO_CHECKOUT_URL"),
    ({"GUMROAD": Adapter(snap("gr-1", title="<>``")), "LEMON_SQUEEZY": Adapter(exc=NotFound())}, "PRODUCT_INVALID")])
def test_DRR4_when_no_listing_can_be_shown_the_lead_is_skipped_with_the_most_specific_reason(adapters, code):
    c, writer, seen, _ = build(choices(GR, LS), adapters)
    c.handle(Conn(), event())
    assert writer.calls == [] and seen == [("DRAFT_SKIPPED", {"marketplace": "EMAIL", "lead_id": LEAD, "outcome": code})] and code in SKIPS


@pytest.mark.parametrize("code", ["PRODUCT_NOT_FOUND", "NO_ACTIVE_LISTING"])
def test_DRR5_a_skip_decided_by_the_router_is_recorded_and_no_adapter_is_asked(code):
    gum = Adapter()
    c, writer, seen, _ = build(RoutePlan(skip=code), {"GUMROAD": gum})
    c.handle(Conn(), event())
    assert writer.calls == [] and gum.asked == [] and code in SKIPS
    assert seen == [("DRAFT_SKIPPED", {"marketplace": "EMAIL", "lead_id": LEAD, "outcome": code})]


@pytest.mark.parametrize("exc", [RateLimited(3.0), AuthExpired()])
def test_DRR6_marketplace_trouble_is_raised_for_a_retry_and_does_not_fall_through_to_the_next_marketplace(exc):
    second = Adapter()
    c, writer, seen, _ = build(choices(GR, LS), {"GUMROAD": Adapter(exc=exc), "LEMON_SQUEEZY": second})
    with pytest.raises(type(exc)):
        c.handle(Conn(), event())
    assert writer.calls == [] and second.asked == [] and seen == []


@pytest.mark.parametrize("conn,code", [(Conn(lead=(None, "gr-1")), "NO_CONTACT"), (Conn(lead=(CONTACT, None)), "NO_PRODUCT"),
                                       (Conn(permission=None), "NO_CURRENT_PERMISSION")])
def test_DRR7_the_earlier_checks_still_come_first_and_the_router_is_not_consulted(conn, code):
    c, writer, seen, router = build(choices(GR), {"GUMROAD": Adapter()})
    c.handle(conn, event())
    assert router.asked == [] and writer.calls == [] and seen[0][1]["outcome"] == code


def test_DRR8_routing_needs_both_a_router_and_the_adapters_or_neither():
    with pytest.raises(ValueError):
        DraftingConsumer(lambda conn: Writer(), Adapter(), router=Router())
    with pytest.raises(ValueError):
        DraftingConsumer(lambda conn: Writer(), Adapter(), product_adapters={})


def test_DRR9_without_a_router_the_single_adapter_is_used_and_the_writer_is_called_as_before():
    only, writer, seen = Adapter(), Writer(), []
    c = DraftingConsumer(lambda conn: writer, only, audit_factory=lambda conn: (lambda a, d: seen.append((a, d))))
    c.handle(Conn(), event())
    (brief, route), = writer.calls
    assert only.asked == ["gr-1"] and route is None and brief.product.external_id == "gr-1" and seen == []


def test_DRR10_the_audit_trail_of_a_routing_decision_carries_only_ids_and_a_code():
    c, _, seen, _ = build(choices(LS), {"LEMON_SQUEEZY": Adapter()})
    c.handle(Conn(), event())
    (action, d), = seen
    assert action == "ROUTE_DECIDED" and set(d) == {"marketplace", "lead_id", "outcome"} and d["outcome"] == MOST_PROFITABLE

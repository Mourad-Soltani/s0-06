"""The Payhip adapter passes the S1-04 contract suite, plus its catalogue, coupon, link and preflight tests. Names PH*."""
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from adapters.base import UnsupportedCapability
from adapters.errors import AuthExpired, NotFound, RateLimited, Rejected, UnsupportedParameter
from adapters.payhip.adapter import PayhipAdapter, PayhipProduct
from adapters.payhip.fake_transport import FakePayhipTransport
from adapters.payhip.transport import CONSERVATIVE_BUCKET
from contracts.suite import ALL_CHECKS, Harness, run_all
from shared.models import ActionType, Discount, ListingStatus, Marketplace, ProposedAction, RateModel, Signal, Verdict

T0 = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
CATALOG = [PayhipProduct("RGsF", "Budget Planner", 1900, "usd", "A planner")]


class Source:
    def __init__(self, *signals):
        self.items, self.asked = list(signals), []

    def read(self, marketplace, since):
        self.asked.append((marketplace, since))
        return [s for s in self.items if s.occurred_at >= since]


def adapter(catalog=CATALOG, source=None):
    return PayhipAdapter(FakePayhipTransport(), catalog, source, clock=lambda: T0)


def harness():
    def seed(a, n):
        keys = []
        for i in range(n):
            a.catalog.append(PayhipProduct(f"ph{i}", "Guide", 1000 + i))
            keys.append(f"ph{i}")
        return keys

    def probe(a):
        if not any(p.key == "probe" for p in a.catalog):
            a.catalog.append(PayhipProduct("probe", "Probe", 100))
        return lambda: a.create_offer_code("probe", "C" + uuid.uuid4().hex[:8], Discount(kind="PERCENT", percent=Decimal("10")), None)

    return Harness(
        name="payhip", make=lambda: adapter(catalog=[]), seed_products=seed, seed_conversation=lambda a: None,
        inject_rate_limit=lambda a, op, retry: a.transport.inject(op, RateLimited(retry)),
        known_violation_action=lambda a: ProposedAction(action_type=ActionType.CREATE_OFFER, marketplace=Marketplace.PAYHIP, payload={}, execution_plan=("captcha_bypass",)),
        compliant_action=lambda a: ProposedAction(action_type=ActionType.CREATE_OFFER, marketplace=Marketplace.PAYHIP, payload={}),
        external_effects=lambda a: len(a.transport.writes), adapter_calls=lambda a: len([c for c in a.transport.calls if c != "ping"]),
        rate_limit_op="create_coupon", rate_limit_probe=probe)


@pytest.mark.parametrize("check", ALL_CHECKS)
def test_PH1_payhip_passes_the_contract_suite(check):
    check(harness())


def test_PH2_run_all_reports_no_failures():
    assert all(msg is None for _, msg in run_all(harness()))


def test_PH3_products_come_from_the_sellers_catalogue_because_payhip_cannot_list_them():
    a = adapter()
    (p,) = a.list_products()
    assert (p.external_id, p.title, p.description, p.price.amount_minor, p.price.currency, p.status, p.checkout_url, p.last_synced_at) == (
        "RGsF", "Budget Planner", "A planner", 1900, "USD", ListingStatus.ACTIVE, "https://payhip.com/b/RGsF", T0)
    assert a.get_product("RGsF") == p and a.transport.calls == []                          # no call to Payhip: there is nothing to ask
    with pytest.raises(NotFound):
        a.get_product("nope")
    assert adapter(catalog=[]).list_products() == []


@pytest.mark.parametrize("kw", [dict(key=""), dict(key="has space"), dict(name="  "), dict(price_minor=-1), dict(currency="US")])
def test_PH3b_a_bad_catalogue_entry_is_refused_when_it_is_written(kw):
    base = dict(key="K1", name="Guide", price_minor=100, currency="USD")
    base.update(kw)
    with pytest.raises(ValueError):
        PayhipProduct(**base)


def test_PH4_a_percent_coupon_is_created_for_one_product_and_the_link_applies_it():
    a = adapter()
    r = a.create_offer_code("RGsF", "SAVE10", Discount(kind="PERCENT", percent=Decimal("10")), None)
    assert r.code == "SAVE10" and r.checkout_url == "https://payhip.com/b/RGsF?coupon=SAVE10"
    assert a.transport.writes == [("coupon", "SAVE10", "RGsF", 10, None)]


@pytest.mark.parametrize("discount,expires,code,exc", [
    (Discount(kind="FIXED", fixed_minor=500), None, "SAVE", UnsupportedParameter),                       # the unit of the amount is unconfirmed
    (Discount(kind="PERCENT", percent=Decimal("10")), T0, "SAVE", UnsupportedParameter),                  # expiry is unconfirmed too
    (Discount(kind="PERCENT", percent=Decimal("12.5")), None, "SAVE", UnsupportedParameter), (Discount(kind="PERCENT", percent=Decimal("0")), None, "SAVE", UnsupportedParameter),
    (Discount(kind="PERCENT", percent=Decimal("101")), None, "SAVE", UnsupportedParameter),
    (Discount(kind="PERCENT", percent=Decimal("10")), None, "no_underscores", ValueError), (Discount(kind="PERCENT", percent=Decimal("10")), None, "AB", ValueError)])
def test_PH4b_what_cannot_be_done_safely_is_refused_before_any_remote_change(discount, expires, code, exc):
    a = adapter()
    with pytest.raises(exc):
        a.create_offer_code("RGsF", code, discount, expires)
    assert a.transport.writes == []


def test_PH4c_a_coupon_can_only_be_made_for_a_product_in_the_catalogue():
    a = adapter()
    with pytest.raises(NotFound):
        a.create_offer_code("nope", "SAVE", Discount(kind="PERCENT", percent=Decimal("10")), None)
    assert a.transport.writes == []


def test_PH5_the_checkout_link_honours_only_the_coupon_so_a_lead_id_is_never_shared_with_payhip():
    a = adapter()
    lead = str(uuid.uuid4())
    assert a.build_checkout_url("RGsF", {}) == "https://payhip.com/b/RGsF"
    assert a.build_checkout_url("RGsF", {"ref": lead}) == "https://payhip.com/b/RGsF" and lead not in a.build_checkout_url("RGsF", {"ref": lead, "utm": lead})
    assert a.build_checkout_url("RGsF", {"coupon": "SAVE10", "ref": lead}) == "https://payhip.com/b/RGsF?coupon=SAVE10"
    with pytest.raises(NotFound):
        a.build_checkout_url("nope", {})


def test_PH5b_the_closer_leaves_a_payhip_message_unchanged_because_there_is_no_attribution_to_add():
    from orchestrator.closer import attribute_link
    a = adapter()
    plain, attributed = a.build_checkout_url("RGsF", {}), a.build_checkout_url("RGsF", {"ref": str(uuid.uuid4())})
    body = f"Subject: Hi\n\nHere it is: {plain}"
    assert attribute_link(body, plain, attributed) == (body, "ALREADY_ATTRIBUTED")


def act(**kw):
    base = dict(action_type=ActionType.CREATE_OFFER, marketplace=Marketplace.PAYHIP, payload={})
    base.update(kw)
    return ProposedAction(**base)


@pytest.mark.parametrize("kw,reasons", [(dict(), ()), (dict(action_type=ActionType.REPLY), ("MESSAGING_UNSUPPORTED",)),
                                        (dict(action_type=ActionType.UPDATE_LISTING), ("LISTING_MUTATION_UNSUPPORTED",)),
                                        (dict(marketplace=Marketplace.ETSY), ("WRONG_MARKETPLACE",)), (dict(execution_plan=("account_linking_bypass",)), ("ACCOUNT_LINKING_BYPASS",))])
def test_PH6_preflight_rules(kw, reasons):
    a = adapter()
    v = a.preflight_check(act(**kw))
    assert v.reason_codes == reasons and v.verdict is (Verdict.BLOCK if reasons else Verdict.PASS) and a.transport.calls == []


@pytest.mark.parametrize("fault,ok,code", [(None, True, None), (AuthExpired(), False, "AUTH_EXPIRED"), (RateLimited(3.0), False, "RATE_LIMITED"), (Rejected(500), False, "DEGRADED")])
def test_PH7_health(fault, ok, code):
    a = adapter()
    if fault:
        a.transport.inject("ping", fault)
    h = a.health_check()
    assert (h.ok, h.detail_code, h.checked_at) == (ok, code, T0)


def test_PH8_signals_come_only_from_the_inbox_and_the_capabilities_are_honest():
    sig = Signal(signal_ref="ph_tx_1_0", kind="SALE", marketplace=Marketplace.PAYHIP, product_external_id="RGsF", occurred_at=T0)
    src = Source(sig)
    assert adapter(source=src).fetch_signals(T0) == [sig] and src.asked == [("PAYHIP", T0)] and adapter().fetch_signals(T0) == []
    c = PayhipAdapter.capabilities
    assert (c.supports_offer_codes, c.supports_checkout_url) == (True, True) and c.rate_limit_model is RateModel.TOKEN_BUCKET
    assert not (c.supports_outbound_messaging or c.supports_listing_mutation or c.supports_acp_checkout)
    assert CONSERVATIVE_BUCKET == {"capacity": 30, "refill_per_second": 0.5}
    a = adapter()
    for call in (lambda: a.send_reply("c", "hi", uuid.uuid4()), lambda: a.update_status("p", ListingStatus.PAUSED)):
        with pytest.raises(UnsupportedCapability):
            call()

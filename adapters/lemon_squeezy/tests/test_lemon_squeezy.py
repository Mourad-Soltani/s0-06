"""The Lemon Squeezy adapter passes the S1-04 contract suite, plus its mapping, discount, checkout-link and preflight tests. No network, no
database. Names LS*."""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from adapters.base import UnsupportedCapability
from adapters.errors import AuthExpired, MalformedResponse, NotFound, RateLimited, Rejected, UnsupportedParameter
from adapters.lemon_squeezy import mapping
from adapters.lemon_squeezy.adapter import DEFAULT_BUCKET, LemonSqueezyAdapter
from adapters.lemon_squeezy.fake_transport import FakeLemonSqueezyTransport
from contracts.suite import ALL_CHECKS, Harness, run_all
from shared.models import ActionType, Discount, ListingStatus, Marketplace, ProposedAction, RateModel, Signal, Verdict

T0 = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


class Source:
    def __init__(self, *signals):
        self.items, self.asked = list(signals), []

    def read(self, marketplace, since):
        self.asked.append((marketplace, since))
        return [s for s in self.items if s.occurred_at >= since]


def adapter(source=None):
    return LemonSqueezyAdapter(FakeLemonSqueezyTransport(), source, clock=lambda: T0)


def harness():
    def seed(a, n):
        ids = []
        for i in range(n):
            a.transport.add_product(f"ls-{i}", price=1000 + i)
            ids.append(f"ls-{i}")
        return ids

    return Harness(
        name="lemon_squeezy", make=adapter, seed_products=seed, seed_conversation=lambda a: None,
        inject_rate_limit=lambda a, op, retry: a.transport.inject(op, RateLimited(retry)),
        known_violation_action=lambda a: ProposedAction(action_type=ActionType.CREATE_OFFER, marketplace=Marketplace.LEMON_SQUEEZY, payload={},
                                                        execution_plan=("captcha_bypass",)),
        compliant_action=lambda a: ProposedAction(action_type=ActionType.CREATE_OFFER, marketplace=Marketplace.LEMON_SQUEEZY, payload={}),
        external_effects=lambda a: len(a.transport.writes), adapter_calls=lambda a: len([c for c in a.transport.calls if c != "ping"]),
        rate_limit_op="get_products")


@pytest.mark.parametrize("check", ALL_CHECKS)
def test_LS1_lemon_squeezy_passes_the_contract_suite(check):
    check(harness())


def test_LS2_run_all_reports_no_failures():
    assert all(msg is None for _, msg in run_all(harness()))


def raw(**over):
    base = {"id": "p1", "name": "Guide", "description": "<p>d</p>", "status": "published", "price": 1900, "currency": "usd",
            "buy_now_url": "https://shop.test/buy/p1"}
    base.update(over)
    return {k: v for k, v in base.items() if v is not ...}


def test_LS3_product_mapping_and_status_rules():
    p = mapping.product_snapshot(raw(), T0)
    assert (p.external_id, p.title, p.price.amount_minor, p.price.currency, p.status, p.checkout_url, p.last_synced_at) == (
        "p1", "Guide", 1900, "USD", ListingStatus.ACTIVE, "https://shop.test/buy/p1", T0)
    assert mapping.product_snapshot(raw(status="draft"), T0).status is ListingStatus.DRAFT
    assert mapping.product_snapshot(raw(status="pending"), T0).status is ListingStatus.PAUSED
    assert mapping.product_snapshot(raw(id=7), T0).external_id == "7" and mapping.product_snapshot(raw(description=None), T0).description == ""
    assert mapping.product_snapshot(raw(buy_now_url="http://insecure.test/x"), T0).checkout_url is None
    assert mapping.product_snapshot(raw(buy_now_url=None), T0).checkout_url is None


@pytest.mark.parametrize("bad,field", [(dict(id=...), "id"), (dict(name=...), "name"), (dict(status=...), "status"), (dict(price=...), "price"),
                                       (dict(currency=...), "currency"), (dict(price="19.99"), "price"), (dict(price=True), "price"), (dict(name=5), "name")])
def test_LS3b_malformed_products_name_the_field_never_the_value(bad, field):
    with pytest.raises(MalformedResponse) as e:
        mapping.product_snapshot(raw(**bad), T0)
    assert field in str(e.value) and "19.99" not in str(e.value)


@pytest.mark.parametrize("bad", [dict(name="  "), dict(price=-1), dict(currency="US"), dict(currency="1$$")])
def test_LS3c_invalid_values_are_malformed(bad):
    with pytest.raises(MalformedResponse):
        mapping.product_snapshot(raw(**bad), T0)


def test_LS4_signals_come_from_the_inbox_for_this_marketplace_and_none_without_one():
    sig = Signal(signal_ref="ls_order_1", kind="SALE", marketplace=Marketplace.LEMON_SQUEEZY, product_external_id="p1", occurred_at=T0)
    old = Signal(signal_ref="ls_order_0", kind="SALE", marketplace=Marketplace.LEMON_SQUEEZY, product_external_id="p1", occurred_at=T0 - timedelta(days=2))
    src = Source(old, sig)
    a = adapter(src)
    assert a.fetch_signals(T0 - timedelta(hours=1)) == [sig] and src.asked == [("LEMON_SQUEEZY", T0 - timedelta(hours=1))]
    assert adapter().fetch_signals(T0) == [] and a.transport.calls == []


def test_LS5_percent_and_fixed_discounts_are_limited_to_the_products_variants():
    a = adapter()
    a.transport.add_product("p1")
    r = a.create_offer_code("p1", "SAVE10", Discount(kind="PERCENT", percent=Decimal("10")), None)
    assert r.code == "SAVE10" and r.checkout_url == "https://shop.test/buy/p1?discount=SAVE10"
    a.create_offer_code("p1", "FIVE5", Discount(kind="FIXED", fixed_minor=500), None)
    expiry = T0 + timedelta(days=7)
    out = a.create_offer_code("p1", "LATE20", Discount(kind="PERCENT", percent=Decimal("20")), expiry)
    assert out.expires_at == expiry
    assert a.transport.writes == [("discount", "SAVE10", 10, "percent", ("p1-v1",), None), ("discount", "FIVE5", 500, "fixed", ("p1-v1",), None),
                                  ("discount", "LATE20", 20, "percent", ("p1-v1",), expiry)]


@pytest.mark.parametrize("discount,expires,code,exc", [
    (Discount(kind="PERCENT", percent=Decimal("12.5")), None, "SAVE", UnsupportedParameter), (Discount(kind="PERCENT", percent=Decimal("0")), None, "SAVE", UnsupportedParameter),
    (Discount(kind="PERCENT", percent=Decimal("101")), None, "SAVE", UnsupportedParameter), (Discount(kind="FIXED", fixed_minor=0), None, "SAVE", UnsupportedParameter),
    (Discount(kind="PERCENT", percent=Decimal("10")), None, "has_underscore", ValueError), (Discount(kind="PERCENT", percent=Decimal("10")), None, "AB", ValueError),
    (Discount(kind="PERCENT", percent=Decimal("10")), None, "x" * 65, ValueError), (Discount(kind="PERCENT", percent=Decimal("10")), datetime(2026, 12, 1), "SAVE", ValueError)])
def test_LS5b_refusals_happen_before_any_remote_change(discount, expires, code, exc):
    a = adapter()
    a.transport.add_product("p1")
    with pytest.raises(exc):
        a.create_offer_code("p1", code, discount, expires)
    assert a.transport.writes == []


def test_LS5c_an_unknown_product_is_not_found_and_changes_nothing():
    a = adapter()
    with pytest.raises(NotFound):
        a.create_offer_code("nope", "SAVE", Discount(kind="FIXED", fixed_minor=1), None)
    assert a.transport.writes == []


def test_LS6_checkout_links_carry_custom_data_and_discounts_in_the_documented_form():
    a = adapter()
    a.transport.add_product("p1")
    lead = uuid.uuid4()
    url = a.build_checkout_url("p1", {"ref": str(lead)})
    assert url == "https://shop.test/buy/p1?checkout%5Bcustom%5D%5Bref%5D=" + str(lead)
    both = a.build_checkout_url("p1", {"ref": "x", "discount": "SAVE10"})
    assert both == "https://shop.test/buy/p1?discount=SAVE10&checkout%5Bcustom%5D%5Bref%5D=x"            # parameters in name order: deterministic
    assert a.build_checkout_url("p1", {}) == "https://shop.test/buy/p1"
    a.transport.add_product("p2", buy_now_url="https://shop.test/buy/p2?embed=1")
    assert a.build_checkout_url("p2", {"discount": "A1B"}) == "https://shop.test/buy/p2?embed=1&discount=A1B"


@pytest.mark.parametrize("key", ["Ref", "a-b", "1x", "", "x" * 33, "a b"])
def test_LS6b_custom_keys_are_restricted(key):
    a = adapter()
    a.transport.add_product("p1")
    with pytest.raises(ValueError):
        a.build_checkout_url("p1", {key: "v"})


def test_LS6c_a_product_without_a_secure_buy_link_cannot_produce_a_checkout_link():
    a = adapter()
    a.transport.add_product("p1", buy_now_url="http://insecure.test/x")
    with pytest.raises(MalformedResponse):
        a.build_checkout_url("p1", {})
    with pytest.raises(NotFound):
        a.build_checkout_url("nope", {})


def act(**kw):
    base = dict(action_type=ActionType.CREATE_OFFER, marketplace=Marketplace.LEMON_SQUEEZY, payload={})
    base.update(kw)
    return ProposedAction(**base)


@pytest.mark.parametrize("kw,reasons", [(dict(), ()), (dict(action_type=ActionType.SEND_MESSAGE, payload={"context": "buyer_initiated"}), ("MESSAGING_UNSUPPORTED",)),
                                        (dict(action_type=ActionType.UPDATE_LISTING), ("LISTING_MUTATION_UNSUPPORTED",)),
                                        (dict(marketplace=Marketplace.GUMROAD), ("WRONG_MARKETPLACE",)), (dict(execution_plan=("browser_automation",)), ("BROWSER_AUTOMATION",))])
def test_LS7_preflight_rules(kw, reasons):
    a = adapter()
    v = a.preflight_check(act(**kw))
    assert v.reason_codes == reasons and v.verdict is (Verdict.BLOCK if reasons else Verdict.PASS) and a.transport.calls == []


@pytest.mark.parametrize("fault,ok,code", [(None, True, None), (AuthExpired(), False, "AUTH_EXPIRED"), (RateLimited(3.0), False, "RATE_LIMITED"), (Rejected(500), False, "DEGRADED")])
def test_LS8_health(fault, ok, code):
    a = adapter()
    if fault:
        a.transport.inject("ping", fault)
    h = a.health_check()
    assert (h.ok, h.detail_code, h.checked_at) == (ok, code, T0)


def test_LS9_declared_capabilities_and_the_documented_rate_limit():
    c = LemonSqueezyAdapter.capabilities
    assert (c.supports_offer_codes, c.supports_checkout_url) == (True, True) and c.rate_limit_model is RateModel.FIXED_WINDOW
    assert not (c.supports_outbound_messaging or c.supports_listing_mutation or c.supports_acp_checkout)
    assert DEFAULT_BUCKET == {"capacity": 300, "refill_per_second": 5.0}                 # 300 requests a minute
    a = adapter()
    for call in (lambda: a.send_reply("c", "hi", uuid.uuid4()), lambda: a.update_status("p", ListingStatus.PAUSED)):
        with pytest.raises(UnsupportedCapability):
            call()
    a.transport.inject("get_products", RateLimited(60.0))
    with pytest.raises(RateLimited):
        a.list_products()

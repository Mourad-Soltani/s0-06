"""S2-01: the Gumroad adapter passes the S1-04 contract suite, plus its own mapping, offer, preflight and
transport tests. No network, no database. Names GR* (adapter) and HT* (HTTP transport)."""
import email.message
import urllib.error
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from adapters.base import UnsupportedCapability
from adapters.errors import (AdapterError, AuthExpired, MalformedResponse, NotFound, RateLimited, Rejected,
                             TransportError, UnsupportedParameter)
from adapters.gumroad import mapping
from adapters.gumroad.adapter import GumroadAdapter
from adapters.gumroad.fake_transport import FakeGumroadTransport
from adapters.gumroad.transport import HttpGumroadTransport
from contracts.suite import ALL_CHECKS, Harness, run_all
from shared.models import (ActionType, Discount, ListingStatus, Marketplace, ProposedAction, Signal, Verdict)

T0 = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)

def adapter():
    return GumroadAdapter(FakeGumroadTransport(), clock=lambda: T0)

def gumroad_harness():
    def seed(a, n):
        ids = []
        for i in range(n):
            a.transport.add_product(f"gr-{i}", price=1000 + i)
            ids.append(f"gr-{i}")
        return ids

    return Harness(
        name="gumroad", make=adapter, seed_products=seed, seed_conversation=lambda a: None,
        inject_rate_limit=lambda a, op, retry: a.transport.inject(op, RateLimited(retry)),
        known_violation_action=lambda a: ProposedAction(action_type=ActionType.CREATE_OFFER,
                                                        marketplace=Marketplace.GUMROAD, payload={},
                                                        execution_plan=("browser_automation",)),
        compliant_action=lambda a: ProposedAction(action_type=ActionType.CREATE_OFFER,
                                                  marketplace=Marketplace.GUMROAD, payload={}),
        external_effects=lambda a: len(a.transport.writes),
        adapter_calls=lambda a: len([c for c in a.transport.calls if c != "ping"]),
        rate_limit_op="get_products")

@pytest.mark.parametrize("check", ALL_CHECKS)
def test_GR1_gumroad_passes_the_contract_suite(check):
    check(gumroad_harness())

def test_GR2_run_all_reports_no_failures():
    assert all(msg is None for _, msg in run_all(gumroad_harness()))

def raw(**over):
    base = {"id": "p1", "name": "Guide", "price": 1900, "currency": "usd", "published": True,
            "short_url": "https://gum.test/l/p1", "description": "d"}
    base.update(over)
    return {k: v for k, v in base.items() if v is not ...}

def test_GR3_product_mapping():
    p = mapping.product_snapshot(raw(), T0)
    assert (p.external_id, p.title, p.price.amount_minor, p.price.currency, p.status, p.checkout_url,
            p.last_synced_at) == ("p1", "Guide", 1900, "USD", ListingStatus.ACTIVE, "https://gum.test/l/p1", T0)
    assert mapping.product_snapshot(raw(published=False), T0).status is ListingStatus.PAUSED
    assert mapping.product_snapshot(raw(id=77), T0).external_id == "77"
    assert mapping.product_snapshot(raw(description=None, short_url=None), T0).description == ""

@pytest.mark.parametrize("bad,field", [
    (dict(id=...), "id"), (dict(name=...), "name"), (dict(price=...), "price"), (dict(currency=...), "currency"),
    (dict(published=...), "published"), (dict(price="19.99"), "price"), (dict(price=True), "price"),
    (dict(published="yes"), "published"), (dict(name=5), "name")])
def test_GR3b_malformed_products_name_the_field_never_the_value(bad, field):
    with pytest.raises(MalformedResponse) as e:
        mapping.product_snapshot(raw(**bad), T0)
    assert field in str(e.value) and "19.99" not in str(e.value) and "yes" not in str(e.value)

@pytest.mark.parametrize("bad", [dict(name="   "), dict(price=-1), dict(currency="US"), dict(currency="12$")])
def test_GR3c_invalid_values_are_malformed(bad):
    with pytest.raises(MalformedResponse):
        mapping.product_snapshot(raw(**bad), T0)

def sale(**over):
    base = {"id": "A-m3CDDC5dlrSdKZp0RFhA==", "created_at": "2026-10-19T10:30:00Z", "product_id": "p1",
            "email": "jane.doe@example.test", "full_name": "Jane Doe", "ip_country": "FR"}
    base.update(over)
    return {k: v for k, v in base.items() if v is not ...}

def test_GR4_sale_signals_are_opaque_and_drop_buyer_details():
    s = mapping.sale_signal(sale())
    assert s.kind == "SALE" and s.marketplace is Marketplace.GUMROAD and s.product_external_id == "p1"
    assert s.signal_ref.startswith("gr_sale_") and all(c.isalnum() or c == "_" for c in s.signal_ref)
    assert s.occurred_at == datetime(2026, 10, 19, 10, 30, tzinfo=timezone.utc)
    text = repr(s)
    assert "jane.doe" not in text and "Jane" not in text and "FR" not in text
    assert mapping.sale_signal(sale()).signal_ref == s.signal_ref != mapping.sale_signal(sale(id="other")).signal_ref
    assert mapping.sale_signal(sale(created_at="2026-10-19T10:30:00")).occurred_at.tzinfo is not None
    assert mapping.sale_signal(sale(product_id=...)).product_external_id is None

@pytest.mark.parametrize("bad", [dict(id=...), dict(created_at=...), dict(created_at="yesterday"), dict(created_at=5)])
def test_GR4b_malformed_sales(bad):
    with pytest.raises(MalformedResponse):
        mapping.sale_signal(sale(**bad))

def test_GR5_fetch_signals_filters_to_the_exact_time_and_sorts():
    a = adapter()
    a.transport.sales = [sale(id="s3", created_at="2026-10-19T09:00:00Z"), sale(id="s1", created_at="2026-10-19T07:00:00Z"),
                         sale(id="s2", created_at="2026-10-19T08:00:00Z"), sale(id="old", created_at="2026-10-18T23:59:00Z")]
    since = datetime(2026, 10, 19, 7, 30, tzinfo=timezone.utc)
    got = a.fetch_signals(since)
    assert [s.signal_ref for s in got] == [mapping.sale_signal(sale(id=i)).signal_ref for i in ("s2", "s3")]
    assert all(isinstance(s, Signal) for s in got)

def with_product():
    a = adapter()
    a.transport.add_product("p1", short_url="https://gum.test/l/p1")
    return a

def test_GR6_percent_and_fixed_offer_codes():
    a = with_product()
    r = a.create_offer_code("p1", "LEAD_abc-1", Discount(kind="PERCENT", percent=Decimal("10")), None)
    assert (r.code, r.checkout_url) == ("LEAD_abc-1", "https://gum.test/l/p1/LEAD_abc-1")
    a.create_offer_code("p1", "FIVE", Discount(kind="FIXED", fixed_minor=500), None)
    assert a.transport.writes == [("offer_code", "p1", "LEAD_abc-1"), ("offer_code", "p1", "FIVE")]

@pytest.mark.parametrize("discount,expires,code,exc", [
    (Discount(kind="PERCENT", percent=Decimal("12.5")), None, "OK", UnsupportedParameter),
    (Discount(kind="PERCENT", percent=Decimal("10")), T0, "OK", UnsupportedParameter),
    (Discount(kind="PERCENT", percent=Decimal("10")), None, "bad code", ValueError),
    (Discount(kind="PERCENT", percent=Decimal("10")), None, "x" * 65, ValueError)])
def test_GR6b_unsupported_requests_are_refused_before_any_remote_change(discount, expires, code, exc):
    a = with_product()
    with pytest.raises(exc):
        a.create_offer_code("p1", code, discount, expires)
    assert a.transport.writes == []

def test_GR6c_unknown_product_and_missing_url():
    a = adapter()
    with pytest.raises(NotFound):
        a.create_offer_code("nope", "C", Discount(kind="FIXED", fixed_minor=1), None)
    a.transport.add_product("p2")
    a.transport.products["p2"]["short_url"] = None
    with pytest.raises(MalformedResponse):
        a.create_offer_code("p2", "C", Discount(kind="FIXED", fixed_minor=1), None)

def test_GR7_checkout_urls():
    a = with_product()
    assert a.build_checkout_url("p1", {"b": "2", "a": "1"}) == "https://gum.test/l/p1?a=1&b=2"
    assert a.build_checkout_url("p1", {}) == "https://gum.test/l/p1"
    with pytest.raises(NotFound):
        a.build_checkout_url("nope", {})

def act(**kw):
    base = dict(action_type=ActionType.CREATE_OFFER, marketplace=Marketplace.GUMROAD, payload={})
    base.update(kw)
    return ProposedAction(**base)

@pytest.mark.parametrize("kw,reasons", [
    (dict(), ()),
    (dict(action_type=ActionType.SEND_MESSAGE, payload={"context": "buyer_initiated"}), ("MESSAGING_UNSUPPORTED",)),
    (dict(action_type=ActionType.REPLY), ("MESSAGING_UNSUPPORTED",)),
    (dict(action_type=ActionType.UPDATE_LISTING), ("LISTING_MUTATION_UNSUPPORTED",)),
    (dict(marketplace=Marketplace.ETSY), ("WRONG_MARKETPLACE",)),
    (dict(execution_plan=("captcha_bypass",)), ("CAPTCHA_BYPASS",)),
    (dict(marketplace=Marketplace.ETSY, execution_plan=("browser_automation",)), ("BROWSER_AUTOMATION", "WRONG_MARKETPLACE"))])
def test_GR8_preflight_rules(kw, reasons):
    a = adapter()
    v = a.preflight_check(act(**kw))
    assert v.reason_codes == reasons and v.verdict is (Verdict.BLOCK if reasons else Verdict.PASS)
    assert a.transport.calls == []

@pytest.mark.parametrize("fault,ok,code", [(None, True, None), (AuthExpired(), False, "AUTH_EXPIRED"),
                                           (RateLimited(3.0), False, "RATE_LIMITED"), (Rejected(500), False, "DEGRADED")])
def test_GR9_health(fault, ok, code):
    a = adapter()
    if fault:
        a.transport.inject("ping", fault)
    h = a.health_check()
    assert (h.ok, h.detail_code, h.checked_at) == (ok, code, T0)

def test_GR10_declared_capabilities_and_error_passthrough():
    c = GumroadAdapter.capabilities
    assert (c.supports_offer_codes, c.supports_checkout_url) == (True, True)
    assert not (c.supports_outbound_messaging or c.supports_listing_mutation or c.supports_acp_checkout)
    a = adapter()
    for call in (lambda: a.send_reply("c", "hi", __import__("uuid").uuid4()), lambda: a.update_status("p", ListingStatus.PAUSED)):
        with pytest.raises(UnsupportedCapability):
            call()
    a.transport.inject("get_products", RateLimited(2.0))
    with pytest.raises(RateLimited):
        a.list_products()
    with pytest.raises(NotFound):
        a.get_product("nope")

class Resp:
    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.body

class Opener:
    def __init__(self, *script):
        self.script, self.requests = list(script), []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return Resp(item if isinstance(item, bytes) else item.encode())

def http_error(code, retry_after=None):
    h = email.message.Message()
    if retry_after is not None:
        h["Retry-After"] = retry_after
    return urllib.error.HTTPError("https://api.gumroad.com/x", code, "msg", h, None)

def transport(*script, **kw):
    op = Opener(*script)
    return HttpGumroadTransport("tok-123", opener=op, **kw), op

def test_HT1_requests_carry_the_token_and_the_right_paths():
    t, op = transport('{"success":true,"products":[]}', '{"success":true,"user":{}}',
                      '{"success":true,"product":{"id":"a=b/c"}}')
    assert t.get_products() == []
    t.ping()
    assert t.get_product("a=b/c") == {"id": "a=b/c"}
    urls = [r.full_url for r in op.requests]
    assert urls == ["https://api.gumroad.com/v2/products", "https://api.gumroad.com/v2/user",
                    "https://api.gumroad.com/v2/products/a%3Db%2Fc"]
    assert all(r.get_header("Authorization") == "Bearer tok-123" and r.get_method() == "GET" for r in op.requests)

def test_HT2_offer_codes_are_posted_as_a_form():
    t, op = transport('{"success":true,"offer_code":{"name":"X"}}', '{"success":true,"offer_code":{}}')
    t.create_offer_code("p1", name="X", percent_off=10)
    t.create_offer_code("p1", name="Y", amount_off_cents=500)
    assert [r.get_method() for r in op.requests] == ["POST", "POST"]
    assert op.requests[0].data == b"name=X&percent_off=10" and op.requests[1].data == b"name=Y&amount_off=500"
    assert op.requests[0].full_url.endswith("/v2/products/p1/offer_codes")
    for kw in (dict(), dict(percent_off=1, amount_off_cents=1)):
        with pytest.raises(ValueError):
            t.create_offer_code("p1", name="Z", **kw)
    assert len(op.requests) == 2

def test_HT3_sales_pagination_follows_only_this_api_and_is_bounded():
    t, op = transport('{"success":true,"sales":[{"id":1}],"next_page_url":"/v2/sales?page_key=k2"}',
                      '{"success":true,"sales":[{"id":2}]}')
    assert t.get_sales(T0.date()) == [{"id": 1}, {"id": 2}]
    assert op.requests[0].full_url == "https://api.gumroad.com/v2/sales?after=2026-10-19"
    assert op.requests[1].full_url == "https://api.gumroad.com/v2/sales?page_key=k2"
    for nxt in ("https://evil.test/v2/sales", "/other", 5):
        t2, _ = transport('{"success":true,"sales":[],"next_page_url":%s}' % __import__("json").dumps(nxt))
        with pytest.raises(MalformedResponse):
            t2.get_sales(T0.date())
    loop = '{"success":true,"sales":[],"next_page_url":"/v2/sales?page_key=again"}'
    t3, _ = transport(*([loop] * 5), max_pages=3)
    with pytest.raises(MalformedResponse):
        t3.get_sales(T0.date())

@pytest.mark.parametrize("fault,exc", [
    (http_error(401), AuthExpired), (http_error(403), AuthExpired), (http_error(404), NotFound),
    (http_error(429, "7"), RateLimited), (http_error(500), Rejected), (urllib.error.URLError("dns"), TransportError),
    (TimeoutError(), TransportError), ("not json", MalformedResponse), ("[1,2]", MalformedResponse),
    ('{"success":false}', Rejected), ('{"success":true}', MalformedResponse), ('{"success":true,"products":"x"}', MalformedResponse)])
def test_HT4_every_failure_maps_to_a_typed_error(fault, exc):
    t, _ = transport(fault)
    with pytest.raises(exc):
        t.get_products()

def test_HT5_retry_after_is_parsed_and_status_is_kept():
    for header, expected in (("7", 7.0), ("0", None), ("soon", None), (None, None)):
        t, _ = transport(http_error(429, header))
        with pytest.raises(RateLimited) as e:
            t.get_products()
        assert e.value.retry_after_seconds == expected
    t, _ = transport(http_error(503))
    with pytest.raises(Rejected) as e:
        t.get_products()
    assert e.value.status == 503

def test_HT6_the_token_never_appears_in_an_error():
    for fault in (http_error(401), http_error(500), urllib.error.URLError("tok-123 in message"), "tok-123 not json"):
        t, _ = transport(fault)
        with pytest.raises(AdapterError) as e:
            t.get_products()
        assert "tok-123" not in str(e.value) and "tok-123" not in repr(e.value)
    with pytest.raises(ValueError):
        HttpGumroadTransport("")

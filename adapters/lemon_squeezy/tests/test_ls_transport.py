"""Lemon Squeezy over HTTP with a fake opener: headers, rate-limit handling, pagination, JSON:API shapes. No network. Names LT*."""
import io
import json
import urllib.error
from datetime import datetime, timezone

import pytest

from adapters.errors import AuthExpired, MalformedResponse, NotFound, RateLimited, Rejected, TransportError
from adapters.lemon_squeezy.transport import BASE, DEFAULT_RETRY_SECONDS, HttpLemonSqueezyTransport, _NoRedirect

KEY = "ls-api-key-0123456789"


class Resp:
    def __init__(self, body, headers=None):
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.headers = headers or {}

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
        return item if isinstance(item, Resp) else Resp(item)


def http(code, headers=None):
    return urllib.error.HTTPError(BASE + "/v1/x", code, "msg", headers or {}, io.BytesIO(b""))


def transport(*script, **kw):
    op = Opener(*script)
    return HttpLemonSqueezyTransport(KEY, 7, opener=op, **kw), op


def item(i, **attrs):
    base = {"name": f"P{i}", "description": "d", "status": "published", "price": 1900, "buy_now_url": f"https://shop.test/buy/{i}"}
    base.update(attrs)
    return {"type": "products", "id": str(i), "attributes": base}


STORE = {"data": {"type": "stores", "id": "7", "attributes": {"currency": "EUR"}}}


def test_LT1_requests_carry_the_key_and_the_json_api_content_types():
    t, op = transport({"data": {}}, {"data": [item(1)]}, STORE)
    t.ping()
    products = t.get_products()
    assert products == [{"id": "1", "name": "P1", "description": "d", "status": "published", "price": 1900, "buy_now_url": "https://shop.test/buy/1", "currency": "EUR"}]
    ping, listing, store = op.requests
    assert ping.full_url == BASE + "/v1/users/me" and store.full_url == BASE + "/v1/stores/7"
    assert "filter%5Bstore_id%5D=7" in listing.full_url and "page%5Bsize%5D=100" in listing.full_url
    for r in op.requests:
        assert r.get_header("Authorization") == f"Bearer {KEY}" and r.get_header("Accept") == "application/vnd.api+json"
        assert r.get_header("Content-type") == "application/vnd.api+json" and r.get_method() == "GET"


def test_LT2_the_remaining_allowance_is_read_from_the_response_and_a_429_empties_it():
    t, _ = transport(Resp({"data": {}}, {"X-Ratelimit-Remaining": "42", "X-Ratelimit-Limit": "300"}), http(429))
    assert t.rate_limit_remaining is None
    t.ping()
    assert t.rate_limit_remaining == 42
    with pytest.raises(RateLimited) as e:
        t.ping()
    assert t.rate_limit_remaining == 0 and e.value.retry_after_seconds == DEFAULT_RETRY_SECONDS == 60.0


@pytest.mark.parametrize("header,expected", [("7", 7.0), ("0", 60.0), ("soon", 60.0), (None, 60.0)])
def test_LT2b_retry_after_is_used_when_given_and_a_minute_otherwise(header, expected):
    headers = {"Retry-After": header} if header is not None else {}
    t, _ = transport(http(429, headers))
    with pytest.raises(RateLimited) as e:
        t.ping()
    assert e.value.retry_after_seconds == expected


@pytest.mark.parametrize("fault,exc", [(http(401), AuthExpired), (http(403), AuthExpired), (http(404), NotFound), (http(500), Rejected),
                                       (urllib.error.URLError("dns"), TransportError), (TimeoutError(), TransportError), ("not json", MalformedResponse),
                                       ("[1]", MalformedResponse)])
def test_LT3_every_failure_maps_to_a_typed_error(fault, exc):
    t, _ = transport(fault)
    with pytest.raises(exc):
        t.ping()


def test_LT4_pagination_follows_only_this_api_and_is_bounded():
    page1 = {"data": [item(1)], "links": {"next": BASE + "/v1/products?page%5Bnumber%5D=2"}}
    t, op = transport(page1, {"data": [item(2)], "links": {}}, STORE)
    assert [p["id"] for p in t.get_products()] == ["1", "2"] and op.requests[1].full_url == BASE + "/v1/products?page%5Bnumber%5D=2"
    for bad in ("https://evil.test/v1/products", BASE + "/other"):
        t2, _ = transport({"data": [], "links": {"next": bad}})
        with pytest.raises(MalformedResponse):
            t2.get_products()
    loop = {"data": [], "links": {"next": BASE + "/v1/products?page%5Bnumber%5D=9"}}
    t3, _ = transport(*([loop] * 5), max_pages=3)
    with pytest.raises(MalformedResponse):
        t3.get_products()
    t4, _ = transport({"data": "x"})
    with pytest.raises(MalformedResponse):
        t4.get_products()


def test_LT5_the_stores_currency_is_fetched_once_and_a_missing_one_is_malformed():
    t, op = transport({"data": [item(1), item(2), item(3)]}, STORE)
    assert {p["currency"] for p in t.get_products()} == {"EUR"} and len(op.requests) == 2          # one page, one store lookup
    t2, _ = transport({"data": [item(1)]}, {"data": {"attributes": {}}})
    with pytest.raises(MalformedResponse):
        t2.get_products()
    t3, _ = transport({"data": [{"id": "1"}]})
    with pytest.raises(MalformedResponse):
        t3.get_products()


def test_LT6_a_single_product_and_its_variants():
    t, op = transport({"data": item(5)}, STORE, {"data": [{"id": "11"}, {"id": "12"}]})
    assert t.get_product("5")["id"] == "5" and t.get_variant_ids("5") == ["11", "12"]
    assert op.requests[0].full_url == BASE + "/v1/products/5" and "filter%5Bproduct_id%5D=5" in op.requests[2].full_url


def test_LT7_a_discount_is_posted_in_the_json_api_shape():
    t, op = transport({"data": {"id": "d1", "attributes": {"code": "SAVE10"}}}, {"data": {"id": "d2"}}, {})
    expiry = datetime(2026, 12, 1, 10, 0, tzinfo=timezone.utc)
    t.create_discount(code="SAVE10", amount=10, amount_type="percent", variant_ids=["11", "12"], expires_at=expiry)
    t.create_discount(code="FIVE5", amount=500, amount_type="fixed", variant_ids=["11"], expires_at=None)
    first = json.loads(op.requests[0].data)
    assert op.requests[0].get_method() == "POST" and op.requests[0].full_url == BASE + "/v1/discounts"
    assert first["data"]["type"] == "discounts" and first["data"]["attributes"] == {
        "name": "SAVE10", "code": "SAVE10", "amount": 10, "amount_type": "percent", "is_limited_to_products": True, "expires_at": "2026-12-01T10:00:00.000000Z"}
    assert first["data"]["relationships"] == {"store": {"data": {"type": "stores", "id": "7"}}, "variants": {"data": [{"type": "variants", "id": "11"}, {"type": "variants", "id": "12"}]}}
    assert "expires_at" not in json.loads(op.requests[1].data)["data"]["attributes"]
    with pytest.raises(MalformedResponse):
        transport({"data": "x"})[0].create_discount(code="C", amount=1, amount_type="fixed", variant_ids=["1"], expires_at=None)


def test_LT8_the_key_never_appears_in_an_error_or_a_repr_and_redirects_are_refused():
    t, _ = transport(http(500), urllib.error.URLError(KEY), "not json")
    assert KEY not in repr(t) and "ls-api-key" not in repr(t)
    for _ in range(3):
        with pytest.raises(Exception) as e:
            t.ping()
        assert KEY not in str(e.value) and KEY not in repr(e.value)
    with pytest.raises(ValueError):
        HttpLemonSqueezyTransport("", 1)
    assert _NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://elsewhere.test/") is None

"""Payhip over HTTP with a fake opener. No network. Names PT*."""
import io
import json
import urllib.error

import pytest

from adapters.errors import AuthExpired, MalformedResponse, NotFound, RateLimited, Rejected, TransportError
from adapters.payhip.transport import BASE, HttpPayhipTransport, _NoRedirect

KEY = "payhip-key-0123456789"


class Resp:
    def __init__(self, body):
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()

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
        return Resp(item)


def http(code, headers=None):
    return urllib.error.HTTPError(BASE + "/x", code, "msg", headers or {}, io.BytesIO(b""))


def transport(*script):
    op = Opener(*script)
    return HttpPayhipTransport(KEY, opener=op), op


def test_PT1_requests_carry_the_key_in_payhips_header_and_use_form_encoding():
    t, op = transport({"data": []}, {"data": {"code": "SAVE10"}}, {"data": {"code": "A1B"}})
    t.ping()
    t.create_coupon(code="SAVE10", product_key="RGsF", percent_off=10)
    t.create_coupon(code="A1B", product_key="RGsF", percent_off=20, usage_limit=1)
    ping, first, second = op.requests
    assert ping.full_url == BASE + "/api/v2/coupons?limit=1" and ping.get_method() == "GET" and ping.data is None
    assert first.full_url == BASE + "/api/v2/coupons" and first.get_method() == "POST"
    assert first.data == b"code=SAVE10&coupon_type=single&product_key=RGsF&percent_off=10"
    assert second.data == b"code=A1B&coupon_type=single&product_key=RGsF&percent_off=20&usage_limit=1"
    for r in op.requests:
        assert r.get_header("Payhip-api-key") == KEY and r.get_header("Accept") == "application/json" and r.get_header("Authorization") is None


@pytest.mark.parametrize("fault,exc", [(http(401), AuthExpired), (http(403), AuthExpired), (http(404), NotFound), (http(429), RateLimited), (http(500), Rejected),
                                       (urllib.error.URLError("dns"), TransportError), (TimeoutError(), TransportError), ("not json", MalformedResponse),
                                       ("[1]", MalformedResponse), ({"other": 1}, MalformedResponse), ({"data": "x"}, MalformedResponse)])
def test_PT2_every_failure_maps_to_a_typed_error(fault, exc):
    t, _ = transport(fault)
    with pytest.raises(exc):
        t.ping()


@pytest.mark.parametrize("header,expected", [("7", 7.0), ("0", None), ("soon", None), (None, None)])
def test_PT3_retry_after_is_used_when_the_platform_gives_one(header, expected):
    t, _ = transport(http(429, {"Retry-After": header} if header is not None else {}))
    with pytest.raises(RateLimited) as e:
        t.ping()
    assert e.value.retry_after_seconds == expected


def test_PT4_a_created_coupon_is_returned_as_the_data_object():
    t, _ = transport({"data": {"code": "SAVE10", "percent_off": 10}})
    assert t.create_coupon(code="SAVE10", product_key="RGsF", percent_off=10) == {"code": "SAVE10", "percent_off": 10}


def test_PT4b_a_created_coupon_must_come_back_as_an_object_but_a_list_endpoint_may_answer_with_a_list():
    t, _ = transport({"data": []})
    t.ping()                                                              # the coupons list: data is a list
    with pytest.raises(MalformedResponse):
        transport({"data": []})[0].create_coupon(code="SAVE10", product_key="RGsF", percent_off=10)


def test_PT5_the_key_never_appears_in_an_error_or_a_repr_and_redirects_are_refused():
    t, _ = transport(http(500), urllib.error.URLError(KEY), "not json")
    assert KEY not in repr(t)
    for _ in range(3):
        with pytest.raises(Exception) as e:
            t.ping()
        assert KEY not in str(e.value) and KEY not in repr(e.value)
    with pytest.raises(ValueError):
        HttpPayhipTransport("")
    assert _NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://elsewhere.test/") is None

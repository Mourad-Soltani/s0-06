"""The review API client: address rules, token privacy, requests, error mapping. No network. Names RC*."""
import io
import json
import urllib.error

import pytest

from ui.review_client import (AuthError, ReviewApiError, ReviewClient, Unreachable, _NoRedirect, validate_base_url)

TOKEN = "reviewer-token-0123456789"
PID = "8f14e45f-ceea-467a-9575-69a7a4d3c9c4"
BASE = "https://review.example.test"


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
        self.script, self.requests, self.timeouts = list(script), [], []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        self.timeouts.append(timeout)
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return Resp(item)


def http(code, body=b""):
    return urllib.error.HTTPError(BASE + "/x", code, "msg", {}, io.BytesIO(body if isinstance(body, bytes) else json.dumps(body).encode()))


def client(*script, **kw):
    op = Opener(*script)
    return ReviewClient(BASE, TOKEN, opener=op, **kw), op


@pytest.mark.parametrize("url,expected", [
    ("https://review.example.test", "https://review.example.test"), ("https://review.example.test/api/", "https://review.example.test/api"),
    ("  https://review.example.test:8443  ", "https://review.example.test:8443"), ("http://localhost:8080", "http://localhost:8080"),
    ("http://127.0.0.1:8080/", "http://127.0.0.1:8080"), ("http://[::1]:8080", "http://[::1]:8080")])
def test_RC1_good_addresses_are_normalised(url, expected):
    assert validate_base_url(url) == expected


@pytest.mark.parametrize("url", ["http://review.example.test", "ftp://review.example.test", "", "review.example.test", "https://", None, 5,
                                 "https://user:pw@review.example.test", "https://review.example.test?x=1", "https://review.example.test#f"])
def test_RC1b_unsafe_or_malformed_addresses_are_refused_with_a_fixed_message(url):
    with pytest.raises(ValueError) as e:
        validate_base_url(url)
    assert "pw" not in str(e.value) and len(str(e.value)) < 100


def test_RC2_the_token_never_appears_in_a_repr_or_an_error_and_a_blank_token_is_refused():
    c, _ = client(http(500, b"boom " + TOKEN.encode()), urllib.error.URLError(TOKEN), http(401), "not json")
    assert TOKEN not in repr(c) and TOKEN not in str(c) and repr(c) == f"ReviewClient({BASE!r})"
    for _ in range(4):
        with pytest.raises(ReviewApiError) as e:
            c.list_actions()
        assert TOKEN not in str(e.value) and TOKEN not in repr(e.value)
    for bad in ("", "   ", None, 5):
        with pytest.raises(ValueError):
            ReviewClient(BASE, bad)


def test_RC3_requests_carry_the_token_and_go_to_the_right_paths():
    c, op = client({"actions": []}, {"actions": []}, {"action": {"a": 1}}, {}, {}, {})
    c.list_actions()
    c.list_actions(limit=7, marketplace="ETSY")
    c.get_action(PID)
    c.approve(PID)
    c.reject(PID, "TONE_OFF")
    c.edit(PID, {"body": "x"})
    assert [(r.get_method(), r.full_url) for r in op.requests] == [
        ("GET", BASE + "/review/actions?limit=50"), ("GET", BASE + "/review/actions?limit=7&marketplace=ETSY"),
        ("GET", f"{BASE}/review/actions/{PID}"), ("POST", f"{BASE}/review/actions/{PID}/approve"),
        ("POST", f"{BASE}/review/actions/{PID}/reject"), ("POST", f"{BASE}/review/actions/{PID}/edit")]
    assert all(r.get_header("Authorization") == f"Bearer {TOKEN}" and r.get_header("Accept") == "application/json" for r in op.requests)
    assert [r.data for r in op.requests[3:]] == [b"{}", b'{"reason_code": "TONE_OFF"}', b'{"patched_payload": {"body": "x"}}']
    assert all(r.get_header("Content-type") == "application/json" for r in op.requests[3:]) and op.requests[0].data is None
    assert op.timeouts == [10.0] * 6


@pytest.mark.parametrize("bad", ["not-a-uuid", "", "../../etc/passwd", f"{PID}/approve", None])
def test_RC4_a_proposal_id_that_is_not_a_uuid_never_reaches_the_network(bad):
    c, op = client()
    for call in (lambda: c.get_action(bad), lambda: c.approve(bad), lambda: c.reject(bad, "TONE_OFF"), lambda: c.edit(bad, {})):
        with pytest.raises(ValueError):
            call()
    assert op.requests == []


def test_RC5_responses_must_have_the_expected_shape():
    c, _ = client({"actions": [{"proposal_id": PID}]}, {"action": {"proposal_id": PID}})
    assert c.list_actions() == [{"proposal_id": PID}] and c.get_action(PID) == {"proposal_id": PID}
    for script, call in (({}, lambda c: c.list_actions()), ({"actions": "x"}, lambda c: c.list_actions()), ({}, lambda c: c.get_action(PID)),
                         ({"action": []}, lambda c: c.get_action(PID)), ([1, 2], lambda c: c.list_actions()),
                         (b"not json", lambda c: c.list_actions()), (b"\xff\xfe", lambda c: c.list_actions())):
        c2, _ = client(script)
        with pytest.raises(ReviewApiError) as e:
            call(c2)
        assert e.value.code == "BAD_RESPONSE"


@pytest.mark.parametrize("fault,cls,code,status", [
    (http(401), AuthError, "UNAUTHENTICATED", 401), (http(404, {"error": "NOT_FOUND"}), ReviewApiError, "NOT_FOUND", 404),
    (http(409, {"error": "NOT_PENDING"}), ReviewApiError, "NOT_PENDING", 409), (http(409, {"error": "NOT_APPROVABLE"}), ReviewApiError, "NOT_APPROVABLE", 409),
    (http(501, {"error": "EDIT_UNAVAILABLE"}), ReviewApiError, "EDIT_UNAVAILABLE", 501), (http(500, b"<html>oops</html>"), ReviewApiError, "HTTP_500", 500),
    (http(500, {"error": "lowercase"}), ReviewApiError, "HTTP_500", 500), (http(500, {"error": 5}), ReviewApiError, "HTTP_500", 500),
    (http(302), ReviewApiError, "HTTP_302", 302), (urllib.error.URLError("dns"), Unreachable, "UNREACHABLE", None),
    (TimeoutError(), Unreachable, "UNREACHABLE", None), (ConnectionResetError(), Unreachable, "UNREACHABLE", None)])
def test_RC6_every_failure_becomes_a_typed_error_with_a_fixed_code(fault, cls, code, status):
    c, _ = client(fault)
    with pytest.raises(ReviewApiError) as e:
        c.approve(PID)
    assert isinstance(e.value, cls) and (e.value.code, e.value.status) == (code, status)


def test_RC7_redirects_are_not_followed_so_the_token_cannot_be_forwarded():
    assert _NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://elsewhere.test/") is None

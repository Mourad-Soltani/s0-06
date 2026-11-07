"""The Postmark transport against a scripted opener: what it sends, how it reads answers, what it refuses to leak. No network. Names PM*."""
import io
import json
import urllib.error
import uuid
from datetime import datetime, timezone

import pytest

from adapters.email_channel.message import OutboundEmail
from adapters.email_channel.postmark import PostmarkEmailTransport
from adapters.errors import AuthExpired, LookupUnsupported, MalformedResponse, NotFound, RateLimited, Rejected, TransportError

TOKEN = "pm-server-token-0123456789"
SENDER = "hello@seller.example.test"
CONTACT = str(uuid.uuid4())
KEY = str(uuid.uuid4())
ADDRESS = "jane.doe@example.test"
MAIL = OutboundEmail(thread_id=CONTACT, subject="Your guide", text="Hi\n\n--\n1 Example Street, Paris\nTo stop: https://x.test/u?thread=1\n",
                     headers={"List-Unsubscribe": "<https://x.test/u?thread=1>"})
OK = {"ErrorCode": 0, "Message": "OK", "MessageID": "0a129aee-e1cd-480d-b08d-4f48548ff48d", "To": "x"}


class Resp:
    def __init__(self, body):
        self.raw = body if isinstance(body, bytes) else json.dumps(body).encode()

    def read(self):
        return self.raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Body:
    """An error body that fails the test if anyone reads it: it would hold the recipient's address."""
    def read(self, *a):
        raise AssertionError("an HTTP error body must never be read")

    def close(self):
        pass


def http_error(code, headers=None):
    return urllib.error.HTTPError("https://api.postmarkapp.com/email", code, "x", headers or {}, Body())


class Opener:
    def __init__(self, *script):
        self.script, self.requests = list(script), []

    def __call__(self, req, timeout):
        self.requests.append((req, timeout))
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def make(*script, resolve=lambda tid: ADDRESS, **kw):
    opener = Opener(*script)
    return PostmarkEmailTransport(TOKEN, SENDER, resolve, opener=opener, **kw), opener


def test_PM1_a_send_posts_the_message_with_tracking_off_the_key_as_metadata_and_returns_the_providers_id():
    t, op = make(Resp(OK))
    assert t.send(MAIL, KEY) == OK["MessageID"]
    (req, timeout), = op.requests
    assert (req.full_url, req.get_method(), timeout) == ("https://api.postmarkapp.com/email", "POST", 15.0)
    assert req.get_header("X-postmark-server-token") == TOKEN and req.get_header("Content-type") == "application/json"
    assert json.loads(req.data) == {
        "From": SENDER, "To": ADDRESS, "Subject": "Your guide", "TextBody": MAIL.text, "MessageStream": "outbound",
        "Headers": [{"Name": "List-Unsubscribe", "Value": "<https://x.test/u?thread=1>"}], "Metadata": {"idem": KEY},
        "TrackOpens": False, "TrackLinks": "None"}                                  # link tracking off: the approved link must arrive unchanged


def test_PM2_the_thread_id_is_resolved_at_the_last_moment_and_an_unknown_contact_sends_nothing():
    seen = []
    t, op = make(Resp(OK), resolve=lambda tid: seen.append(tid) or ADDRESS)
    t.send(MAIL, KEY)
    assert seen == [CONTACT]
    t2, op2 = make(resolve=lambda tid: None)
    with pytest.raises(NotFound):
        t2.send(MAIL, KEY)
    assert op2.requests == []


@pytest.mark.parametrize("address", ["", "no-at-sign", "a@b", "a b@c.test", "a@c.test\nBcc: x@y.test", "<a@c.test>"])
def test_PM3_an_address_that_is_not_plain_is_refused_before_any_request(address):
    t, op = make(resolve=lambda tid: address)
    with pytest.raises(NotFound):
        t.send(MAIL, KEY)
    assert op.requests == []


@pytest.mark.parametrize("error,expected", [
    (http_error(401), AuthExpired), (http_error(403), AuthExpired), (http_error(404), NotFound), (http_error(500), TransportError),
    (http_error(503), TransportError), (http_error(422), Rejected), (http_error(400), Rejected),
    (urllib.error.URLError("dns"), TransportError), (TimeoutError(), TransportError), (OSError(), TransportError)])
def test_PM4_every_failure_maps_to_the_adapters_own_errors_and_the_error_body_is_never_read(error, expected):
    t, _ = make(error)
    with pytest.raises(expected) as e:
        t.send(MAIL, KEY)
    assert ADDRESS not in str(e.value) and TOKEN not in str(e.value)
    if expected is Rejected:
        assert e.value.status in (400, 422)


def test_PM4b_a_rate_limit_carries_the_retry_after_and_survives_a_missing_one():
    t, _ = make(http_error(429, {"Retry-After": "7"}), http_error(429))
    with pytest.raises(RateLimited) as e:
        t.send(MAIL, KEY)
    assert e.value.retry_after_seconds == 7.0
    with pytest.raises(RateLimited) as e2:
        t.send(MAIL, KEY)
    assert e2.value.retry_after_seconds is None


@pytest.mark.parametrize("body", [b"<html>", b"[]", b"\xff\xfe", {"ErrorCode": 0}, {"ErrorCode": 0, "MessageID": ""}, {"ErrorCode": 300, "MessageID": "x"},
                                  {"ErrorCode": 0, "MessageID": 5}])
def test_PM5_an_answer_that_cannot_be_trusted_is_a_malformed_response_not_a_success(body):
    t, _ = make(Resp(body))
    with pytest.raises(MalformedResponse):
        t.send(MAIL, KEY)


def test_PM6_what_may_not_be_sent_is_refused_before_any_request_header_injection_and_odd_keys():
    t, op = make()
    for mail in (OutboundEmail(CONTACT, "Hi\nBcc: x@y.test", "t"), OutboundEmail(CONTACT, "Hi", "t", {"List-Unsubscribe": "<a>\r\nBcc: x@y.test"}),
                 OutboundEmail(CONTACT, "Hi", "t", {"Bad Name": "v"})):
        with pytest.raises(ValueError):
            t.send(mail, KEY)
    for key in ("", "has space", "x" * 129, None):
        with pytest.raises(ValueError):
            t.send(MAIL, key)
    assert op.requests == []


def test_PM7_the_repr_and_the_constructor_never_expose_the_token_or_the_sender():
    t, _ = make()
    assert TOKEN not in repr(t) and SENDER not in repr(t) and repr(t) == "PostmarkEmailTransport(stream='outbound', lookup=False)"
    for args in (("", SENDER), (" ", SENDER), (TOKEN, "not-an-address"), (TOKEN, "a@b.test\nBcc: c@d.test"), (TOKEN, "Name <a@b.test>")):
        with pytest.raises(ValueError):
            PostmarkEmailTransport(*args, lambda tid: ADDRESS)
    for stream in ("", "Outbound", "has space", "x" * 65):
        with pytest.raises(ValueError):
            PostmarkEmailTransport(TOKEN, SENDER, lambda tid: ADDRESS, message_stream=stream)


def test_PM8_ping_is_a_read_only_authenticated_call_and_a_bad_token_is_auth_expired():
    t, op = make(Resp({"Name": "server"}), http_error(401))
    t.ping()
    assert (op.requests[0][0].full_url, op.requests[0][0].get_method(), op.requests[0][0].data) == ("https://api.postmarkapp.com/server", "GET", None)
    with pytest.raises(AuthExpired):
        t.ping()


def test_PM9_lookup_is_off_by_default_because_the_providers_search_is_not_authoritative():
    t, op = make()
    assert t.supports_lookup is False
    with pytest.raises(LookupUnsupported):
        t.lookup(KEY)
    assert op.requests == []


def test_PM10_an_enabled_lookup_asks_by_the_metadata_key_and_distinguishes_found_from_never_accepted():
    t, op = make(Resp({"TotalCount": 1, "Messages": [{"MessageID": "mid-1"}]}), Resp({"TotalCount": 0, "Messages": []}),
                 Resp({"TotalCount": "1"}), Resp({"TotalCount": 1, "Messages": [{}]}), enable_lookup=True)
    assert t.supports_lookup is True and t.lookup(KEY) == "mid-1"
    url = op.requests[0][0].full_url
    assert url.startswith("https://api.postmarkapp.com/messages/outbound?") and f"metadata_idem={KEY}" in url and "messagestream=outbound" in url
    assert t.lookup(KEY) is None
    for _ in range(2):
        with pytest.raises(MalformedResponse):
            t.lookup(KEY)                                      # an unreadable answer is never read as "never sent"


def test_PM11_replies_are_not_read_in_this_version():
    t, op = make()
    assert t.list_threads(datetime(2026, 11, 1, tzinfo=timezone.utc)) == []
    with pytest.raises(NotFound):
        t.read_thread("any")
    assert op.requests == []

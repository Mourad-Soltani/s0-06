"""The email adapter's optional provider lookup, used to settle a send whose result was never recorded. Names EL*."""
import uuid
from datetime import datetime, timezone

import pytest

from adapters.email_channel.adapter import EmailChannelAdapter
from adapters.email_channel.fake_transport import FakeEmailTransport
from adapters.email_channel.message import EmailConfig
from adapters.errors import AuthExpired, LookupUnsupported, RateLimited

CONFIG = EmailConfig("1 Example Street, Paris 75001, France", "https://example.test/unsubscribe")
BODY = "Subject: Hello\n\nHello there"


def adapter():
    return EmailChannelAdapter(FakeEmailTransport(), CONFIG, clock=lambda: datetime(2026, 11, 2, tzinfo=timezone.utc))


def test_EL1_a_key_that_was_sent_is_found_and_an_unknown_key_is_not():
    a, key = adapter(), uuid.uuid4()
    sent = a.send_reply("thread-1", BODY, key)
    found = a.find_send(key)
    assert found is not None and found.external_message_id == sent.external_message_id and found.deduplicated is False
    assert a.find_send(uuid.uuid4()) is None


def test_EL2_a_provider_that_cannot_answer_authoritatively_raises_and_is_never_asked():
    a = adapter()
    a.transport.supports_lookup = False
    with pytest.raises(LookupUnsupported):
        a.find_send(uuid.uuid4())
    assert "lookup" not in a.transport.calls
    del a.transport.supports_lookup                                           # a transport that does not even declare the capability
    with pytest.raises(LookupUnsupported):
        a.find_send(uuid.uuid4())


def test_EL3_a_failed_lookup_is_an_error_never_read_as_not_sent():
    a = adapter()
    a.transport.inject("lookup", RateLimited(5.0))
    with pytest.raises(RateLimited):
        a.find_send(uuid.uuid4())
    a.transport.auth_expired = True
    with pytest.raises(AuthExpired):
        a.find_send(uuid.uuid4())


def test_EL4_looking_up_sends_nothing_and_changes_nothing():
    a, key = adapter(), uuid.uuid4()
    a.send_reply("thread-1", BODY, key)
    before = (list(a.transport.sent), list(a.transport.sent_keys))
    a.find_send(key)
    a.find_send(uuid.uuid4())
    assert (a.transport.sent, a.transport.sent_keys) == before and a.transport.calls.count("send") == 1


def test_EL5_the_provider_is_asked_with_the_key_as_text():
    a, key = adapter(), uuid.uuid4()
    asked = []
    original = a.transport.lookup
    a.transport.lookup = lambda k: (asked.append(k), original(k))[1]
    a.find_send(key)
    assert asked == [str(key)]

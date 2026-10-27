"""S2-02: the email adapter passes the S1-04 contract suite, plus its message format, footer, preflight and
idempotency tests. No network, no database. Names EC*."""
import uuid
from datetime import datetime, timezone

import pytest

from adapters.base import UnsupportedCapability
from adapters.email_channel import message as msg
from adapters.email_channel.adapter import EmailChannelAdapter
from adapters.email_channel.fake_transport import FakeEmailTransport
from adapters.errors import AuthExpired, MalformedResponse, NotFound, RateLimited
from contracts.suite import ALL_CHECKS, Harness, run_all
from shared.models import ActionType, Discount, ListingStatus, Marketplace, ProposedAction, Verdict

T0 = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
CONFIG = msg.EmailConfig(postal_address="1 Example Street, Paris 75001, France",
                         unsubscribe_base_url="https://example.test/unsubscribe")
BODY = "Subject: Hello\n\nHello there"
EMAIL = "jane.doe@example.test"


def adapter(config=CONFIG):
    return EmailChannelAdapter(FakeEmailTransport(), config, clock=lambda: T0)


def action(**kw):
    base = dict(action_type=ActionType.SEND_MESSAGE, marketplace=Marketplace.EMAIL,
                payload={"context": "consented_outreach", "consent_ref": "perm-1"})
    base.update(kw)
    return ProposedAction(**base)


def email_harness():
    def seed_conversation(a):
        a.transport.add_thread("thread-1", "consented_outreach")
        return "thread-1"

    return Harness(
        name="email", make=adapter, seed_products=lambda a, n: [], seed_conversation=seed_conversation,
        inject_rate_limit=lambda a, op, retry: a.transport.inject(op, RateLimited(retry)),
        known_violation_action=lambda a: action(execution_plan=("browser_automation",)),
        compliant_action=lambda a: action(), external_effects=lambda a: len(a.transport.sent),
        adapter_calls=lambda a: len([c for c in a.transport.calls if c != "ping"]),
        has_catalog=False, rate_limit_op="list_threads",
        rate_limit_probe=lambda a: (lambda: a.list_conversations(T0)), message_body=BODY)


# ------------------------------------------------------------------ the contract
@pytest.mark.parametrize("check", ALL_CHECKS)
def test_EC1_email_passes_the_contract_suite(check):
    check(email_harness())


def test_EC2_run_all_reports_no_failures():
    assert all(msg is None for _, msg in run_all(email_harness()))


# ------------------------------------------------------------------ message format and footer
def test_EC3_split_message():
    assert msg.split_message("Subject: Hi there\n\nBody line 1\nline 2") == ("Hi there", "Body line 1\nline 2")


@pytest.mark.parametrize("bad", [
    "no subject line\n\nbody", "Subject: only a subject", "Subject: X\nno blank line\nbody", "Subject: \n\nbody",
    "Subject: X\n\n", "Subject: X\n\n   \n", "Subject: " + "x" * 201 + "\n\nbody", "Subject: a\rb\n\nbody",
    "subject: lowercase\n\nbody", ""])
def test_EC3b_invalid_messages_are_refused(bad):
    with pytest.raises(ValueError):
        msg.split_message(bad)


def test_EC4_compose_adds_the_footer_and_unsubscribe_header():
    e = msg.compose(BODY, CONFIG, "thread/1 a")
    assert e.subject == "Hello" and e.text.startswith("Hello there")
    assert "1 Example Street, Paris 75001, France" in e.text
    url = "https://example.test/unsubscribe?thread=thread%2F1%20a"
    assert url in e.text and e.headers == {"List-Unsubscribe": f"<{url}>"}
    other = msg.EmailConfig(postal_address=CONFIG.postal_address, unsubscribe_base_url="https://example.test/u?x=1")
    assert "u?x=1&thread=t1" in msg.compose(BODY, other, "t1").text


@pytest.mark.parametrize("kw", [dict(postal_address="short"), dict(postal_address="line one\nline two long enough"),
                                dict(unsubscribe_base_url="http://example.test/u"), dict(unsubscribe_base_url="https://a b")])
def test_EC4b_config_is_validated_at_construction(kw):
    base = dict(postal_address=CONFIG.postal_address, unsubscribe_base_url=CONFIG.unsubscribe_base_url)
    base.update(kw)
    with pytest.raises(ValueError):
        msg.EmailConfig(**base)


# ------------------------------------------------------------------ sending
def test_EC5_send_reply_composes_passes_the_key_and_is_idempotent():
    a, key = adapter(), uuid.uuid4()
    first = a.send_reply("thread-1", BODY, key)
    (sent,) = a.transport.sent
    assert sent.thread_id == "thread-1" and sent.subject == "Hello" and "Unsubscribe" not in sent.subject
    assert "To stop receiving these emails" in sent.text and a.transport.sent_keys == [str(key)]
    again = a.send_reply("thread-1", BODY, key)
    assert again.external_message_id == first.external_message_id and again.deduplicated is True
    assert len(a.transport.sent) == 1
    assert a.send_reply("thread-1", BODY, uuid.uuid4()).external_message_id != first.external_message_id


def test_EC5b_a_failed_send_is_not_remembered_so_the_retry_goes_through_once():
    a, key = adapter(), uuid.uuid4()
    a.transport.inject("send", RateLimited(5.0))
    with pytest.raises(RateLimited):
        a.send_reply("thread-1", BODY, key)
    assert a.transport.sent == []
    ok = a.send_reply("thread-1", BODY, key)
    assert ok.deduplicated is False and len(a.transport.sent) == 1
    assert a.send_reply("thread-1", BODY, key).deduplicated is True


def test_EC5c_invalid_or_oversized_bodies_never_reach_the_transport():
    a = adapter()
    for body in ("no subject\n\nbody", "Subject: X\n\n" + "y" * 10_000):
        with pytest.raises(ValueError):
            a.send_reply("thread-1", body, uuid.uuid4())
    assert a.transport.calls == []


def test_EC5d_expired_auth_sends_nothing():
    a = adapter()
    a.transport.auth_expired = True
    with pytest.raises(AuthExpired):
        a.send_reply("thread-1", BODY, uuid.uuid4())
    assert a.transport.sent == []
    h = a.health_check()
    assert (h.ok, h.detail_code) == (False, "AUTH_EXPIRED")


# ------------------------------------------------------------------ conversations and drafts
def test_EC6_conversations_map_and_keep_bodies_out_of_repr():
    a = adapter()
    a.transport.add_thread("t1", "buyer_initiated", messages=[
        {"message_id": "m1", "sender": "BUYER", "body": f"my address is {EMAIL}", "sent_at": "2026-10-19T11:00:00Z"}])
    (c,) = a.list_conversations(datetime(2026, 10, 19, 0, 0, tzinfo=timezone.utc))
    assert (c.conversation_id, c.context) == ("t1", "buyer_initiated")
    assert a.list_conversations(datetime(2026, 10, 20, tzinfo=timezone.utc)) == []
    (m,) = a.read_conversation("t1")
    assert m.sender == "BUYER" and EMAIL in m.body and EMAIL not in repr(m)
    with pytest.raises(NotFound):
        a.read_conversation("nope")


@pytest.mark.parametrize("raw_thread,raw_message", [
    ({"context": "x", "updated_at": "2026-10-19T00:00:00Z"}, None), ({"thread_id": "t", "context": "buyer_initiated"}, None),
    ({"thread_id": "t", "context": "buyer_initiated", "updated_at": "never"}, None),
    (None, {"message_id": "m", "sender": "BUYER", "body": "b"}),
    (None, {"message_id": "m", "sender": "ALIEN", "body": "b", "sent_at": "2026-10-19T00:00:00Z"})])
def test_EC6b_malformed_provider_data_is_a_typed_error(raw_thread, raw_message):
    with pytest.raises(MalformedResponse):
        msg.conversation(raw_thread) if raw_thread is not None else msg.message(raw_message)


def test_EC7_drafts_are_validated_and_never_sent():
    a = adapter()
    assert a.draft_reply("t1", BODY).draft_id == "draft-1" and a.draft_reply("t1", BODY).draft_id == "draft-2"
    with pytest.raises(ValueError):
        a.draft_reply("t1", "not a valid message")
    assert a.transport.calls == [] and a.transport.sent == []


# ------------------------------------------------------------------ preflight
@pytest.mark.parametrize("kw,reasons", [
    (dict(), ()),
    (dict(payload={"context": "consented_outreach"}), ("MISSING_CONSENT_REF",)),
    (dict(payload={"context": "consented_outreach", "consent_ref": EMAIL}), ("MISSING_CONSENT_REF",)),
    (dict(payload={"context": "buyer_initiated"}), ()),                       # replying to an inbound message
    (dict(action_type=ActionType.REPLY, payload={"context": "order_related"}), ()),
    (dict(payload={"context": "cold_outreach", "consent_ref": "perm-1"}), ("CONTEXT_NOT_ALLOWED",)),
    (dict(payload={"context": "consented_outreach", "consent_ref": "perm-1", "body": "no subject"}), ("INVALID_MESSAGE_FORMAT",)),
    (dict(payload={"context": "consented_outreach", "consent_ref": "perm-1", "body": BODY}), ()),
    (dict(payload={"context": "consented_outreach", "consent_ref": "perm-1", "body": "Subject: X\n\n" + "y" * 10_000}),
     ("MESSAGE_TOO_LONG",)),
    (dict(execution_plan=("device_verification_bypass",)), ("DEVICE_VERIFICATION_BYPASS",)),
    (dict(marketplace=Marketplace.GUMROAD), ("WRONG_MARKETPLACE",)),
    (dict(action_type=ActionType.UPDATE_LISTING, payload={}), ("LISTING_MUTATION_UNSUPPORTED",)),
    (dict(action_type=ActionType.CREATE_OFFER, payload={}), ("OFFER_CODES_UNSUPPORTED",))])
def test_EC8_preflight_rules(kw, reasons):
    a = adapter()
    v = a.preflight_check(action(**kw))
    assert v.reason_codes == reasons and v.verdict is (Verdict.BLOCK if reasons else Verdict.PASS)
    assert a.transport.calls == []                                           # policy only


# ------------------------------------------------------------------ declarations
def test_EC9_no_catalog_no_offers_no_checkout_no_listings():
    a = adapter()
    assert a.list_products() == [] and a.fetch_signals(T0) == []
    with pytest.raises(NotFound):
        a.get_product("x")
    for call in (lambda: a.create_offer_code("p", "C", Discount(kind="FIXED", fixed_minor=1), None),
                 lambda: a.build_checkout_url("p", {}), lambda: a.update_status("p", ListingStatus.PAUSED)):
        with pytest.raises(UnsupportedCapability):
            call()
    c = EmailChannelAdapter.capabilities
    assert c.supports_outbound_messaging and c.requires_idempotency_key and c.max_message_length == 10_000
    assert set(c.allowed_message_contexts) == {"consented_outreach", "buyer_initiated", "order_related"}
    assert a.transport.calls == []

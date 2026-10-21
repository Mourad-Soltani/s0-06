"""S1-03 tests: the mock adapter behaves like a marketplace adapter should, under every capability profile
and under scripted faults. No database. Names are MK* so they cannot collide with gate prefixes or -k T10."""
import itertools
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from adapters.base import UnsupportedCapability
from adapters.errors import AdapterError, AuthExpired, DuplicateOfferCode, NotFound, RateLimited
from contracts.mock_adapter import BYPASS_STEPS, default_capabilities, make_mock_adapter
from shared.models import (ActionType, Discount, ListingInput, ListingStatus, Marketplace, Message, Money,
                           ProposedAction, Signal, Verdict)

T0 = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
PRICE = Money(amount_minor=1900, currency="USD")


def mk(**kw):
    return make_mock_adapter(Marketplace.ETSY, clock=lambda: T0, **kw)


def profile(listing, offers, url, messaging):
    return default_capabilities(
        supports_listing_mutation=listing, supports_offer_codes=offers, supports_checkout_url=url,
        supports_outbound_messaging=messaging,
        **({} if messaging else dict(max_message_length=None, allowed_message_contexts=())))


def listing_input(**kw):
    return ListingInput(title="Guide", description="d", price=PRICE, **kw)


def action(**kw):
    base = dict(action_type=ActionType.SEND_MESSAGE, marketplace=Marketplace.ETSY,
                payload={"context": "buyer_initiated"})
    base.update(kw)
    return ProposedAction(**base)


# ----------------------------------------------------------------- every capability profile builds and gates correctly
@pytest.mark.parametrize("flags", list(itertools.product([True, False], repeat=4)))
def test_MK1_every_capability_profile_builds_and_gates(flags):
    listing, offers, url, messaging = flags
    a = mk(capabilities=profile(*flags))                     # the declared-not-inferred check passes
    ops = {
        "supports_listing_mutation": lambda: a.update_status("x", ListingStatus.PAUSED),
        "supports_offer_codes": lambda: a.create_offer_code("x", "C", Discount(kind="FIXED", fixed_minor=1), None),
        "supports_checkout_url": lambda: a.build_checkout_url("x", {}),
        "supports_outbound_messaging": lambda: a.list_conversations(T0),
    }
    for flag, enabled in zip(ops, flags):
        if enabled:
            try:
                ops[flag]()
            except NotFound:
                pass                                          # no such product: the capability itself worked
        else:
            with pytest.raises(UnsupportedCapability):
                ops[flag]()


# ----------------------------------------------------------------- catalog, offers, checkout
def test_MK2_catalog_round_trip():
    a = mk()
    r = a.create_or_update_listing(listing_input())
    assert (r.external_id, r.status) == ("prod-1", ListingStatus.DRAFT)
    assert [p.external_id for p in a.list_products()] == ["prod-1"]
    a.update_price("prod-1", Money(amount_minor=2900, currency="USD"))
    a.update_status("prod-1", ListingStatus.ACTIVE)
    p = a.get_product("prod-1")
    assert (p.price.amount_minor, p.status, p.last_synced_at) == (2900, ListingStatus.ACTIVE, T0)
    for call in (lambda: a.get_product("nope"), lambda: a.update_price("nope", PRICE),
                 lambda: a.update_status("nope", ListingStatus.PAUSED),
                 lambda: a.create_or_update_listing(listing_input(external_id="nope"))):
        with pytest.raises(NotFound):
            call()


def test_MK3_offer_codes_and_checkout_urls():
    a = mk()
    pid = a.create_or_update_listing(listing_input()).external_id
    offer = a.create_offer_code(pid, "SAVE10", Discount(kind="PERCENT", percent=10), None)
    assert offer.code == "SAVE10" and offer.checkout_url.endswith("?code=SAVE10")
    with pytest.raises(DuplicateOfferCode):
        a.create_offer_code(pid, "SAVE10", Discount(kind="PERCENT", percent=10), None)
    with pytest.raises(NotFound):
        a.create_offer_code("nope", "X", Discount(kind="FIXED", fixed_minor=100), None)
    assert a.build_checkout_url(pid, {"b": "2", "a": "1"}) == f"https://mock.invalid/checkout/{pid}?a=1&b=2"
    assert "?" not in a.build_checkout_url(pid, {})


# ----------------------------------------------------------------- messaging
def seeded():
    a = mk()
    a.seed_conversation("c1", "buyer_initiated",
                        [Message(message_id="m0", sender="BUYER", body="hello", sent_at=T0)])
    return a


def test_MK4_messaging_draft_send_and_idempotency():
    a = seeded()
    assert len(a.list_conversations(T0 - timedelta(days=1))) == 1
    assert a.list_conversations(T0 + timedelta(days=1)) == []
    assert [m.sender for m in a.read_conversation("c1")] == ["BUYER"]
    a.draft_reply("c1", "draft text")
    assert len(a.read_conversation("c1")) == 1                 # a draft is not a message
    k1, k2 = uuid.uuid4(), uuid.uuid4()
    first = a.send_reply("c1", "hi", k1)
    assert len(a.read_conversation("c1")) == 2 and first.deduplicated is False
    again = a.send_reply("c1", "hi", k1)
    assert again.external_message_id == first.external_message_id and again.deduplicated is True
    assert len(a.read_conversation("c1")) == 2 and len(a.sent_log) == 1
    second = a.send_reply("c1", "hi", k2)
    assert second.external_message_id != first.external_message_id and len(a.read_conversation("c1")) == 3
    for call in (lambda: a.send_reply("zzz", "hi", uuid.uuid4()), lambda: a.read_conversation("zzz"),
                 lambda: a.draft_reply("zzz", "hi")):
        with pytest.raises(NotFound):
            call()


# ----------------------------------------------------------------- scripted faults
def test_MK5_rate_limit_fault_leaves_no_partial_send():
    a = seeded()
    a.inject("send_reply", RateLimited(5.0))
    key = uuid.uuid4()
    with pytest.raises(RateLimited) as e:
        a.send_reply("c1", "hi", key)
    assert e.value.retry_after_seconds == 5.0
    assert len(a.read_conversation("c1")) == 1 and a.sent_log == []     # nothing leaked out
    ok = a.send_reply("c1", "hi", key)                                    # the retry is the first real send
    assert ok.deduplicated is False and a.calls.count("send_reply") == 2
    assert a.send_reply("c1", "hi", key).deduplicated is True


def test_MK5b_wildcard_fault_hits_any_operation_n_times():
    a = mk()
    a.inject("*", RateLimited(None), times=2)
    with pytest.raises(RateLimited):
        a.list_products()
    with pytest.raises(RateLimited):
        a.get_product("x")
    assert a.list_products() == []                                        # faults exhausted


def test_MK6_expired_auth_blocks_everything_and_changes_nothing():
    a = seeded()
    a.expire_auth()
    for call in (lambda: a.list_products(), lambda: a.fetch_signals(T0),
                 lambda: a.create_or_update_listing(listing_input()),
                 lambda: a.send_reply("c1", "hi", uuid.uuid4()), lambda: a.read_conversation("c1")):
        with pytest.raises(AuthExpired):
            call()
    assert len(a._messages["c1"]) == 1 and a._catalog == {} and a.sent_log == []
    h = a.health_check()
    assert (h.ok, h.detail_code) == (False, "AUTH_EXPIRED")
    assert a.preflight_check(action()).verdict is Verdict.PASS             # policy needs no login
    a.renew_auth()
    assert a.list_products() == [] and a.health_check().ok is True


# ----------------------------------------------------------------- preflight (the TOS enforcement point)
def test_MK7_preflight_passes_a_compliant_action_and_has_no_side_effects():
    a = mk()
    v = a.preflight_check(action())
    assert v.verdict is Verdict.PASS and v.reason_codes == ()
    assert a.calls == ["preflight_check"] and a._catalog == {} and a.sent_log == []


@pytest.mark.parametrize("step", sorted(BYPASS_STEPS))
def test_MK7b_bypass_plans_are_blocked(step):
    v = mk().preflight_check(action(execution_plan=(step,)))
    assert (v.verdict, v.reason_codes) == (Verdict.BLOCK, (step.upper(),))


def test_MK7c_message_context_marketplace_and_capability_rules():
    a = mk()
    assert mk().preflight_check(action(payload={"context": "cold_outreach"})).reason_codes == ("CONTEXT_NOT_ALLOWED",)
    assert a.preflight_check(action(payload={})).reason_codes == ("CONTEXT_NOT_ALLOWED",)
    assert a.preflight_check(action(marketplace=Marketplace.GUMROAD)).reason_codes == ("WRONG_MARKETPLACE",)
    both = a.preflight_check(action(marketplace=Marketplace.GUMROAD, execution_plan=("browser_automation",)))
    assert both.reason_codes == ("BROWSER_AUTOMATION", "WRONG_MARKETPLACE")      # sorted, all reasons reported
    no_msg = mk(capabilities=profile(True, True, True, False))
    assert no_msg.preflight_check(action()).reason_codes == ("MESSAGING_UNSUPPORTED",)
    assert no_msg.preflight_check(action(action_type=ActionType.REPLY)).reason_codes == ("MESSAGING_UNSUPPORTED",)
    assert a.preflight_check(action(action_type=ActionType.CREATE_OFFER, payload={})).verdict is Verdict.PASS


# ----------------------------------------------------------------- signals, health, clock, logs, errors
def test_MK8_signals_are_filtered_by_time():
    a = mk()
    for i, t in enumerate((T0, T0 + timedelta(hours=1))):
        a.queue_signal(Signal(signal_ref=f"sale_{i}", kind="SALE", marketplace=Marketplace.ETSY, occurred_at=t))
    assert [s.signal_ref for s in a.fetch_signals(T0 + timedelta(minutes=30))] == ["sale_1"]
    assert len(a.fetch_signals(T0)) == 2


def test_MK9_health_is_scriptable_and_uses_the_injected_clock():
    a = mk()
    h = a.health_check()
    assert (h.ok, h.detail_code, h.checked_at) == (True, None, T0)
    a.set_health(False, "DEGRADED")
    h = a.health_check()
    assert (h.ok, h.detail_code) == (False, "DEGRADED")


def test_MK10_call_log_holds_operation_names_never_bodies():
    a = seeded()
    a.send_reply("c1", "my secret message", uuid.uuid4())
    assert "secret" not in repr(a.calls) and "secret" not in repr(a.sent_log)
    assert all(isinstance(c, str) and " " not in c for c in a.calls)


def test_MK11_errors_are_fixed_strings_with_a_common_base():
    assert RateLimited(3).retry_after_seconds == 3 and RateLimited().retry_after_seconds is None
    assert (str(RateLimited()), str(AuthExpired()), str(NotFound()), str(DuplicateOfferCode())) == \
           ("rate limited", "authentication expired", "not found", "offer code already exists")
    assert all(issubclass(c, AdapterError) for c in (RateLimited, AuthExpired, NotFound, DuplicateOfferCode))

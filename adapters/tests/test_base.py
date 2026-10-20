"""S1-02 tests: the adapter contract's guards, and the canonical models' strictness. No database needed."""
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from adapters.base import MarketplaceAdapter, UnsupportedCapability
from shared.models import (AdapterCapabilities, AdapterHealth, ComplianceVerdict, Discount, ListingInput,
                           ListingStatus, Marketplace, Message, Money, OfferResult, ProposedAction, RateModel,
                           SendResult, Signal, Verdict)

NOW = datetime(2026, 10, 19, tzinfo=timezone.utc)
PRICE = Money(amount_minor=1900, currency="USD")


def caps(**over):
    base = dict(supports_outbound_messaging=False, supports_offer_codes=False, supports_checkout_url=False,
                supports_listing_mutation=False, supports_acp_checkout=False, allowed_message_contexts=(),
                rate_limit_model=RateModel.TOKEN_BUCKET, requires_idempotency_key=False)
    base.update(over)
    return AdapterCapabilities(**base)


class _Base(MarketplaceAdapter):
    """Implements the always-required methods and declares nothing: an abstract intermediate."""
    def list_products(self): return []
    def get_product(self, external_id): raise KeyError(external_id)
    def fetch_signals(self, since): return []
    def preflight_check(self, action): return ComplianceVerdict(verdict=Verdict.PASS)
    def health_check(self): return AdapterHealth(ok=True, checked_at=NOW)


class ReadOnly(_Base):
    """Gumroad-shaped: offer codes and checkout links, no messaging, no listing mutation."""
    marketplace = Marketplace.GUMROAD
    capabilities = caps(supports_offer_codes=True, supports_checkout_url=True)

    def __init__(self):
        self.calls = []

    def _create_offer_code(self, product_external_id, code, discount, expires_at):
        self.calls.append("offer")
        return OfferResult(code=code, checkout_url="https://example.test/c")

    def _build_checkout_url(self, product_external_id, params):
        self.calls.append("url")
        return "https://example.test/c"


class Messaging(_Base):
    marketplace = Marketplace.ETSY
    capabilities = caps(supports_outbound_messaging=True, max_message_length=20,
                        allowed_message_contexts=("buyer_initiated", "order_related"), requires_idempotency_key=True)

    def __init__(self):
        self.calls, self.sent = [], {}

    def _list_conversations(self, since): self.calls.append("list"); return []
    def _read_conversation(self, conversation_id): self.calls.append("read"); return []
    def _draft_reply(self, conversation_id, body): self.calls.append("draft"); raise KeyError

    def _send_reply(self, conversation_id, body, idempotency_key):
        self.calls.append("send")
        if idempotency_key in self.sent:
            return SendResult(external_message_id=self.sent[idempotency_key], deduplicated=True)
        self.sent[idempotency_key] = f"m{len(self.sent) + 1}"
        return SendResult(external_message_id=self.sent[idempotency_key])


# ----------------------------------------------------------------- capability gating
def test_A1_unsupported_operations_raise_before_any_hook_runs():
    a = ReadOnly()
    key = uuid.uuid4()
    for call in (lambda: a.send_reply("c", "hi", key), lambda: a.draft_reply("c", "hi"),
                 lambda: a.list_conversations(NOW), lambda: a.read_conversation("c"),
                 lambda: a.create_or_update_listing(ListingInput(title="t", description="d", price=PRICE)),
                 lambda: a.update_price("p", PRICE), lambda: a.update_status("p", ListingStatus.PAUSED)):
        with pytest.raises(UnsupportedCapability):
            call()
    assert a.calls == []


def test_A2_declared_capabilities_work_through_the_public_methods():
    a = ReadOnly()
    assert a.build_checkout_url("p", {}) == "https://example.test/c"
    assert a.create_offer_code("p", "SAVE10", Discount(kind="PERCENT", percent=10), None).code == "SAVE10"
    assert a.calls == ["url", "offer"]


def test_A3_send_reply_is_idempotent_through_the_contract():
    a, key = Messaging(), uuid.uuid4()
    first, second = a.send_reply("c", "hello", key), a.send_reply("c", "hello", key)
    assert first.external_message_id == second.external_message_id
    assert (first.deduplicated, second.deduplicated) == (False, True)
    assert len(a.sent) == 1


def test_A4_length_limit_is_enforced_before_the_hook():
    a = Messaging()
    with pytest.raises(ValueError):
        a.send_reply("c", "x" * 21, uuid.uuid4())
    with pytest.raises(ValueError):
        a.draft_reply("c", "x" * 21)
    assert a.calls == []
    a.send_reply("c", "x" * 20, uuid.uuid4())      # exactly at the limit is fine
    assert a.calls == ["send"]


# ----------------------------------------------------------------- declared, not inferred
def test_A5_capability_and_hook_mismatches_fail_at_class_definition():
    with pytest.raises(TypeError):                   # declares checkout URLs, implements nothing
        class NoHook(_Base):
            marketplace = Marketplace.PAYHIP
            capabilities = caps(supports_checkout_url=True)

    with pytest.raises(TypeError):                   # implements a hook it did not declare
        class Undeclared(_Base):
            marketplace = Marketplace.PAYHIP
            capabilities = caps()
            def _build_checkout_url(self, product_external_id, params): return "x"

    with pytest.raises(TypeError):                   # partial implementation of a multi-hook capability
        class Partial(_Base):
            marketplace = Marketplace.SHOPIFY
            capabilities = caps(supports_listing_mutation=True)
            def _update_price(self, external_id, price): pass

    with pytest.raises(TypeError):                   # capabilities without marketplace
        class NoMarketplace(_Base):
            capabilities = caps()


def test_A6_subclasses_inherit_declarations_and_intermediates_are_exempt():
    class Variant(ReadOnly):                         # declares nothing new: inherits capabilities and hooks
        pass

    v = Variant()
    assert v.capabilities.supports_offer_codes and v.build_checkout_url("p", {}) == "https://example.test/c"
    assert Variant.marketplace is Marketplace.GUMROAD


def test_A7_required_methods_are_abstract():
    class Incomplete(MarketplaceAdapter):
        def list_products(self): return []

    with pytest.raises(TypeError):
        Incomplete()


# ----------------------------------------------------------------- canonical models
def test_A8_money_and_discount_rules():
    assert Money(amount_minor=0, currency="EUR").amount_minor == 0
    for bad in (dict(amount_minor=-1, currency="USD"), dict(amount_minor=1, currency="usd"),
                dict(amount_minor=1, currency="USDX"), dict(amount_minor=1, currency="USD", extra=1)):
        with pytest.raises(ValidationError):
            Money(**bad)
    Discount(kind="FIXED", fixed_minor=500)
    for bad in (dict(kind="PERCENT", fixed_minor=500), dict(kind="PERCENT", percent=101),
                dict(kind="FIXED", percent=10), dict(kind="FIXED"), dict(kind="PERCENT")):
        with pytest.raises(ValidationError):
            Discount(**bad)


def test_A9_signal_ref_matches_the_database_alphabet():
    ok = dict(kind="SALE", marketplace=Marketplace.GUMROAD, occurred_at=NOW)
    Signal(signal_ref="sale_123:abc.def-1", **ok)
    for bad in ("a@b.co", "two words", "", "x" * 129, "José"):
        with pytest.raises(ValidationError):
            Signal(signal_ref=bad, **ok)


def test_A10_verdicts_and_plans():
    ComplianceVerdict(verdict=Verdict.PASS)
    ComplianceVerdict(verdict=Verdict.BLOCK, reason_codes=("BROWSER_AUTOMATION",))
    for v in (Verdict.BLOCK, Verdict.ESCALATE):
        with pytest.raises(ValidationError):
            ComplianceVerdict(verdict=v)
    with pytest.raises(ValidationError):
        ComplianceVerdict(verdict=Verdict.BLOCK, reason_codes=("not a code",))
    assert ProposedAction(action_type="SEND_MESSAGE", marketplace="ETSY", payload={}).execution_plan == ()
    with pytest.raises(ValidationError):
        ProposedAction(action_type="SEND_MESSAGE", marketplace="ETSY", payload={}, execution_plan=("Browser Automation",))


def test_A11_capabilities_must_be_declared_and_consistent():
    with pytest.raises(ValidationError):             # nothing is inferred: a missing flag is an error
        AdapterCapabilities(supports_outbound_messaging=False)
    with pytest.raises(ValidationError):             # messaging adapter must declare its contexts
        caps(supports_outbound_messaging=True)
    with pytest.raises(ValidationError):             # limits without messaging
        caps(max_message_length=100)
    with pytest.raises(ValidationError):
        caps(allowed_message_contexts=("buyer_initiated",))


def test_A12_models_are_frozen_and_naive_datetimes_are_rejected():
    m = Money(amount_minor=5, currency="USD")
    with pytest.raises(ValidationError):
        m.amount_minor = 6
    with pytest.raises(ValidationError):
        AdapterHealth(ok=True, checked_at=datetime(2026, 10, 19))      # no tzinfo


def test_A13_message_body_is_kept_out_of_repr():
    m = Message(message_id="m1", sender="BUYER", body="my address is 1 Private Street", sent_at=NOW)
    assert "Private" not in repr(m) and "Private" not in str(m)
    assert m.body.startswith("my address")


def test_A14_marketplace_enum_matches_the_database_enum():
    sql = (Path(__file__).resolve().parents[2] / "infra" / "schema" / "migrations" / "sql"
           / "0001_schema_v0.sql").read_text()
    block = re.search(r"CREATE TYPE core\.marketplace AS ENUM\s*\((.*?)\);", sql, re.S).group(1)
    assert [m.value for m in Marketplace] == re.findall(r"'([A-Z_]+)'", block)

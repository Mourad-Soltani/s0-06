"""Scriptable in-memory marketplace adapter for contract tests and orchestrator tests (S1-03).

make_mock_adapter() builds an adapter class whose DECLARED capabilities and implemented hooks agree, so the
base class's declared-not-inferred check passes for any capability profile. Behaviour is scripted through
inject() (faults), expire_auth(), set_health(), add_product(), seed_conversation() and queue_signal().

Every operation passes through _gate() BEFORE it mutates anything, so a fault or an expired login never
leaves a partial effect (spec 9.3 chaos scenarios). The call log keeps operation names only, never bodies.
"""
from __future__ import annotations

import itertools
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional
from urllib.parse import urlencode

from adapters.base import MarketplaceAdapter
from adapters.errors import AuthExpired, DuplicateOfferCode, NotFound
from shared.models import (ActionType, AdapterCapabilities, AdapterHealth, ComplianceVerdict, Conversation,
                           Discount, DraftResult, ListingInput, ListingResult, ListingStatus, Marketplace,
                           Message, Money, OfferResult, ProductSnapshot, ProposedAction, RateModel, SendResult,
                           Signal, Verdict)

# Spec 6.1 "General": no CAPTCHA, device-verification or account-linking bypass, and no browser automation.
BYPASS_STEPS = frozenset({"browser_automation", "captcha_bypass", "device_verification_bypass",
                          "account_linking_bypass"})


def default_capabilities(**overrides) -> AdapterCapabilities:
    """Everything on: the profile that exercises the most code."""
    base = dict(supports_outbound_messaging=True, supports_offer_codes=True, supports_checkout_url=True,
                supports_listing_mutation=True, supports_acp_checkout=False, max_message_length=2000,
                allowed_message_contexts=("buyer_initiated", "order_related"),
                rate_limit_model=RateModel.TOKEN_BUCKET, requires_idempotency_key=True)
    base.update(overrides)
    return AdapterCapabilities(**base)


class _MockCore(MarketplaceAdapter):
    """Declares no capabilities itself (an abstract intermediate); make_mock_adapter() adds them."""

    def __init__(self, *, clock: Optional[Callable[[], datetime]] = None):
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._ids = itertools.count(1)
        self._faults: Dict[str, deque] = defaultdict(deque)
        self._auth_expired = False
        self._health_ok, self._health_code = True, None
        self._catalog: Dict[str, ProductSnapshot] = {}
        self._conversations: Dict[str, Conversation] = {}
        self._messages: Dict[str, List[Message]] = defaultdict(list)
        self._drafts: Dict[str, str] = {}
        self._sent: Dict[object, SendResult] = {}
        self._codes: set = set()
        self._signals: List[Signal] = []
        self.calls: List[str] = []                 # operation names only
        self.sent_log: List[tuple] = []            # (conversation_id, idempotency_key); never a body

    # ------------------------------------------------------------------ scripting
    def inject(self, op: str, exc: Exception, times: int = 1) -> None:
        """Raise exc on the next `times` calls of op ('*' matches any gated operation)."""
        self._faults[op].extend([exc] * times)

    def expire_auth(self) -> None:
        self._auth_expired = True

    def renew_auth(self) -> None:
        self._auth_expired = False

    def set_health(self, ok: bool, detail_code: Optional[str] = None) -> None:
        self._health_ok, self._health_code = ok, detail_code

    def add_product(self, product: ProductSnapshot) -> None:
        self._catalog[product.external_id] = product

    def seed_conversation(self, conversation_id: str, context: str, messages=()) -> None:
        self._conversations[conversation_id] = Conversation(
            conversation_id=conversation_id, context=context, updated_at=self._clock())
        self._messages[conversation_id] = list(messages)

    def queue_signal(self, signal: Signal) -> None:
        self._signals.append(signal)

    # ------------------------------------------------------------------ the gate every operation passes first
    def _gate(self, op: str) -> None:
        self.calls.append(op)
        if self._auth_expired:
            raise AuthExpired()
        for key in (op, "*"):
            if self._faults[key]:
                raise self._faults[key].popleft()

    def _next(self, prefix: str) -> str:
        return f"{prefix}-{next(self._ids)}"

    # ------------------------------------------------------------------ always-required methods
    def list_products(self) -> List[ProductSnapshot]:
        self._gate("list_products")
        return list(self._catalog.values())

    def get_product(self, external_id: str) -> ProductSnapshot:
        self._gate("get_product")
        if external_id not in self._catalog:
            raise NotFound()
        return self._catalog[external_id]

    def fetch_signals(self, since: datetime) -> List[Signal]:
        self._gate("fetch_signals")
        return [s for s in self._signals if s.occurred_at >= since]

    def preflight_check(self, action: ProposedAction) -> ComplianceVerdict:
        """Pure policy: no gate, no side effect, works while auth is expired."""
        self.calls.append("preflight_check")
        reasons = set()
        if action.marketplace != self.marketplace:
            reasons.add("WRONG_MARKETPLACE")
        reasons.update(step.upper() for step in action.execution_plan if step in BYPASS_STEPS)
        if action.action_type in (ActionType.SEND_MESSAGE, ActionType.REPLY):
            if not self.capabilities.supports_outbound_messaging:
                reasons.add("MESSAGING_UNSUPPORTED")
            elif action.payload.get("context") not in self.capabilities.allowed_message_contexts:
                reasons.add("CONTEXT_NOT_ALLOWED")
        if reasons:
            return ComplianceVerdict(verdict=Verdict.BLOCK, reason_codes=tuple(sorted(reasons)))
        return ComplianceVerdict(verdict=Verdict.PASS)

    def health_check(self) -> AdapterHealth:
        self.calls.append("health_check")
        if self._auth_expired:
            return AdapterHealth(ok=False, checked_at=self._clock(), detail_code="AUTH_EXPIRED")
        return AdapterHealth(ok=self._health_ok, checked_at=self._clock(), detail_code=self._health_code)


class _ListingHooks:
    def _create_or_update_listing(self, listing: ListingInput) -> ListingResult:
        self._gate("create_or_update_listing")
        pid = listing.external_id
        if pid is None:
            pid = self._next("prod")
        elif pid not in self._catalog:
            raise NotFound()
        snap = ProductSnapshot(external_id=pid, title=listing.title, description=listing.description,
                               price=listing.price, status=listing.status,
                               checkout_url=f"https://mock.invalid/checkout/{pid}", last_synced_at=self._clock())
        self._catalog[pid] = snap
        return ListingResult(external_id=pid, status=snap.status, checkout_url=snap.checkout_url)

    def _update_price(self, external_id: str, price: Money) -> None:
        self._gate("update_price")
        if external_id not in self._catalog:
            raise NotFound()
        self._catalog[external_id] = self._catalog[external_id].model_copy(update={"price": price})

    def _update_status(self, external_id: str, status: ListingStatus) -> None:
        self._gate("update_status")
        if external_id not in self._catalog:
            raise NotFound()
        self._catalog[external_id] = self._catalog[external_id].model_copy(update={"status": status})


class _OfferHooks:
    def _create_offer_code(self, product_external_id: str, code: str, discount: Discount,
                           expires_at: Optional[datetime]) -> OfferResult:
        self._gate("create_offer_code")
        if product_external_id not in self._catalog:
            raise NotFound()
        if (product_external_id, code) in self._codes:
            raise DuplicateOfferCode()
        self._codes.add((product_external_id, code))
        return OfferResult(code=code, expires_at=expires_at,
                           checkout_url=f"https://mock.invalid/checkout/{product_external_id}?code={code}")


class _CheckoutHooks:
    def _build_checkout_url(self, product_external_id: str, params: Dict[str, str]) -> str:
        self._gate("build_checkout_url")
        if product_external_id not in self._catalog:
            raise NotFound()
        query = urlencode(sorted(params.items()))
        return f"https://mock.invalid/checkout/{product_external_id}" + (f"?{query}" if query else "")


class _MessagingHooks:
    def _list_conversations(self, since: datetime) -> List[Conversation]:
        self._gate("list_conversations")
        return [c for c in self._conversations.values() if c.updated_at >= since]

    def _read_conversation(self, conversation_id: str) -> List[Message]:
        self._gate("read_conversation")
        if conversation_id not in self._conversations:
            raise NotFound()
        return list(self._messages[conversation_id])

    def _draft_reply(self, conversation_id: str, body: str) -> DraftResult:
        self._gate("draft_reply")
        if conversation_id not in self._conversations:
            raise NotFound()
        draft_id = self._next("draft")
        self._drafts[draft_id] = conversation_id        # a draft is recorded, never delivered
        return DraftResult(draft_id=draft_id)

    def _send_reply(self, conversation_id: str, body: str, idempotency_key) -> SendResult:
        self._gate("send_reply")                         # before any mutation: a fault leaves no partial send
        if conversation_id not in self._conversations:
            raise NotFound()
        if idempotency_key in self._sent:
            return self._sent[idempotency_key].model_copy(update={"deduplicated": True})
        result = SendResult(external_message_id=self._next("msg"))
        self._sent[idempotency_key] = result
        self._messages[conversation_id].append(Message(
            message_id=result.external_message_id, sender="SELLER", body=body, sent_at=self._clock()))
        self.sent_log.append((conversation_id, idempotency_key))
        return result


_MIXINS = (("supports_listing_mutation", _ListingHooks), ("supports_offer_codes", _OfferHooks),
           ("supports_checkout_url", _CheckoutHooks), ("supports_outbound_messaging", _MessagingHooks))


def make_mock_adapter(marketplace: Marketplace = Marketplace.ETSY,
                      capabilities: Optional[AdapterCapabilities] = None,
                      clock: Optional[Callable[[], datetime]] = None) -> _MockCore:
    """Build and return a mock adapter instance for the given marketplace and declared capabilities."""
    caps = capabilities or default_capabilities()
    bases = tuple(mixin for flag, mixin in _MIXINS if getattr(caps, flag)) + (_MockCore,)
    cls = type(f"Mock{marketplace.value.title().replace('_', '')}Adapter", bases,
               {"marketplace": marketplace, "capabilities": caps})
    return cls(clock=clock)

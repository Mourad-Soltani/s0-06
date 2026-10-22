"""Harness for the mock adapter, in any capability profile."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from adapters.errors import RateLimited
from contracts.mock_adapter import default_capabilities, make_mock_adapter
from contracts.suite import Harness
from shared.models import (ActionType, AdapterCapabilities, ListingStatus, Marketplace, Money, ProductSnapshot,
                           ProposedAction)

T0 = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)


def profile(listing: bool, offers: bool, url: bool, messaging: bool) -> AdapterCapabilities:
    return default_capabilities(
        supports_listing_mutation=listing, supports_offer_codes=offers, supports_checkout_url=url,
        supports_outbound_messaging=messaging,
        **({} if messaging else dict(max_message_length=None, allowed_message_contexts=())))


def mock_harness(marketplace: Marketplace = Marketplace.ETSY, capabilities: Optional[AdapterCapabilities] = None,
                 name: Optional[str] = None) -> Harness:
    def make():
        return make_mock_adapter(marketplace, capabilities, clock=lambda: T0)

    def seed_products(a, n):
        ids = []
        for i in range(n):
            pid = f"seed-{i}"
            a.add_product(ProductSnapshot(
                external_id=pid, title="Title", description="d", price=Money(amount_minor=1000 + i, currency="USD"),
                status=ListingStatus.ACTIVE, checkout_url=f"https://mock.invalid/checkout/{pid}", last_synced_at=T0))
            ids.append(pid)
        return ids

    def seed_conversation(a):
        if not a.capabilities.supports_outbound_messaging:
            return None
        a.seed_conversation("conv-1", "buyer_initiated")
        return "conv-1"

    def violation(a):
        return ProposedAction(action_type=ActionType.CREATE_OFFER, marketplace=a.marketplace, payload={},
                              execution_plan=("browser_automation",))

    def compliant(a):
        if a.capabilities.supports_outbound_messaging:
            return ProposedAction(action_type=ActionType.SEND_MESSAGE, marketplace=a.marketplace,
                                  payload={"context": "buyer_initiated"})
        return ProposedAction(action_type=ActionType.CREATE_OFFER, marketplace=a.marketplace, payload={})

    return Harness(
        name=name or f"mock-{marketplace.value.lower()}", make=make, seed_products=seed_products,
        seed_conversation=seed_conversation,
        inject_rate_limit=lambda a, op, retry: a.inject(op, RateLimited(retry)),
        known_violation_action=violation, compliant_action=compliant,
        external_effects=lambda a: len(a.sent_log),
        # the mock logs every operation; only the gated ones reach the "marketplace" (preflight and health do not)
        adapter_calls=lambda a: sum(1 for c in a.calls if c not in ("preflight_check", "health_check")))

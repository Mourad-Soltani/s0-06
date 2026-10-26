"""Reusable adapter contract suite (spec 9.2, S1-04). Every real adapter must pass these against its own
Harness; the mock adapter passes them in contracts/tests. A check raises AssertionError with a message that
names what the adapter got wrong. The suite proves it has teeth: contracts/tests also runs each check against
deliberately broken adapters and requires the check to fail.

The kill-switch contract is database-backed and lives in contracts/suite_db.py.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, List, Optional, Tuple
from uuid import uuid4

from adapters.base import MarketplaceAdapter, UnsupportedCapability
from adapters.governed import call_governed
from shared.governor import Backoff, BucketConfig, InProcessGovernor, ThrottleStatus, lane
from shared.models import Discount, ListingInput, ListingStatus, Money, ProductSnapshot, ProposedAction, Verdict


@dataclass
class Harness:
    """How to put one adapter into known states and observe it from outside. For a real adapter these hooks
    drive its sandbox or recorded transport; for the mock they drive the mock's scripting methods."""
    name: str
    make: Callable[[], MarketplaceAdapter]                         # a fresh adapter in a clean state
    seed_products: Callable[[MarketplaceAdapter, int], List[str]]  # creates n ACTIVE products, returns their ids
    seed_conversation: Callable[[MarketplaceAdapter], Optional[str]]  # a conversation id, or None if unsupported
    inject_rate_limit: Callable[[MarketplaceAdapter, str, float], None]  # next call of op gets a 429
    known_violation_action: Callable[[MarketplaceAdapter], ProposedAction]
    compliant_action: Callable[[MarketplaceAdapter], ProposedAction]
    external_effects: Callable[[MarketplaceAdapter], int]          # externally visible messages sent so far
    adapter_calls: Callable[[MarketplaceAdapter], int]             # calls that reached the marketplace


class FakeClock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _expect_unsupported(fn) -> None:
    try:
        fn()
    except UnsupportedCapability:
        return
    raise AssertionError("an operation the adapter does not declare must raise UnsupportedCapability")


# ----------------------------------------------------------------------------------------------------------
def check_list_products_canonical(h: Harness) -> None:
    """list_products returns canonical ProductSnapshot objects for every active listing."""
    a = h.make()
    seeded = set(h.seed_products(a, 3))
    products = a.list_products()
    assert isinstance(products, list), f"list_products must return a list, got {type(products).__name__}"
    for p in products:
        assert isinstance(p, ProductSnapshot), f"{type(p).__name__} is not a ProductSnapshot"
        validate = getattr(type(p), "model_validate", None)
        if validate:                                    # round trip catches objects built without validation
            validate(p.model_dump())
        assert p.price.amount_minor >= 0 and re.fullmatch(r"[A-Z]{3}", p.price.currency), "malformed price"
        assert p.last_synced_at.tzinfo is not None, "last_synced_at must be timezone-aware"
    ids = [p.external_id for p in products]
    assert len(ids) == len(set(ids)), "duplicate external_id in list_products"
    missing = seeded - {p.external_id for p in products if p.status is ListingStatus.ACTIVE}
    assert not missing, f"active listings missing from list_products: {sorted(missing)}"


def check_preflight_blocks_known_violation(h: Harness) -> None:
    """preflight_check blocks a known violation with reason codes, passes a compliant action, and acts on nothing."""
    a = h.make()
    effects, calls = h.external_effects(a), h.adapter_calls(a)
    bad = a.preflight_check(h.known_violation_action(a))
    assert bad.verdict is Verdict.BLOCK, "a known violation must be BLOCKed"
    assert bad.reason_codes, "a BLOCK must carry at least one reason code"
    good = a.preflight_check(h.compliant_action(a))
    assert good.verdict is Verdict.PASS, f"a compliant action was refused: {good.reason_codes}"
    assert h.external_effects(a) == effects and h.adapter_calls(a) == calls, \
        "preflight must have no side effects and must not reach the marketplace"


def check_send_reply_idempotent(h: Harness) -> None:
    """The same idempotency key twice produces exactly one external message."""
    a = h.make()
    conv = h.seed_conversation(a)
    if not a.capabilities.supports_outbound_messaging:
        _expect_unsupported(lambda: a.send_reply("any", "hi", uuid4()))
        assert h.external_effects(a) == 0, "an unsupported send must have no effect"
        return
    key = uuid4()
    first, second = a.send_reply(conv, "hello", key), a.send_reply(conv, "hello", key)
    assert h.external_effects(a) == 1, "the same idempotency key produced more than one external message"
    assert first.external_message_id == second.external_message_id, "a repeat must return the original message id"
    assert first.deduplicated is False and second.deduplicated is True, "SendResult.deduplicated must flag the repeat"
    a.send_reply(conv, "hello again", uuid4())
    assert h.external_effects(a) == 2, "a new key must send a new message"


def check_unsupported_capabilities_fail_before_side_effects(h: Harness) -> None:
    """Every operation the adapter does not declare raises UnsupportedCapability and changes nothing."""
    a = h.make()
    h.seed_products(a, 1)
    h.seed_conversation(a)
    price, effects, calls = Money(amount_minor=100, currency="USD"), h.external_effects(a), h.adapter_calls(a)
    ops = {
        "supports_listing_mutation": [
            lambda: a.create_or_update_listing(ListingInput(title="t", description="d", price=price)),
            lambda: a.update_price("x", price), lambda: a.update_status("x", ListingStatus.PAUSED)],
        "supports_offer_codes": [lambda: a.create_offer_code("x", "C", Discount(kind="FIXED", fixed_minor=1), None)],
        "supports_checkout_url": [lambda: a.build_checkout_url("x", {})],
        "supports_outbound_messaging": [
            lambda: a.list_conversations(datetime(2026, 1, 1, tzinfo=timezone.utc)),
            lambda: a.read_conversation("x"), lambda: a.draft_reply("x", "hi"), lambda: a.send_reply("x", "hi", uuid4())],
    }
    for flag, calls_for_flag in ops.items():
        if not getattr(a.capabilities, flag):
            for call in calls_for_flag:
                _expect_unsupported(call)
    assert h.external_effects(a) == effects and h.adapter_calls(a) == calls, \
        "an undeclared operation must not reach the marketplace"


def _default_governor(clock, ln):
    return InProcessGovernor({ln: BucketConfig(capacity=10, refill_per_second=5)}, clock=clock)


def check_rate_limit_backoff(h: Harness, governor_factory: Callable = _default_governor) -> None:
    """Under a simulated 429 the governor blocks the lane for the computed time, the adapter is not called
    while backing off, and under a throttle report the wait is deficit / restore rate."""
    a, clock = h.make(), FakeClock()
    ln = lane(a.marketplace, "contract-account")
    gov = governor_factory(clock, ln)
    h.inject_rate_limit(a, "list_products", 7.0)
    start = h.adapter_calls(a)

    first = call_governed(gov, ln, a.list_products)
    assert first.backoff == Backoff(7.0), f"a 429 with Retry-After 7 must back off exactly 7s, got {first.backoff}"
    assert h.adapter_calls(a) == start + 1
    assert call_governed(gov, ln, a.list_products).backoff == Backoff(7.0), "the lane must stay blocked"
    clock.advance(3.0)
    assert call_governed(gov, ln, a.list_products).backoff == Backoff(4.0), "backoff must shrink as time passes"
    assert h.adapter_calls(a) == start + 1, "the adapter must not be called while the lane is backing off"
    clock.advance(4.0)
    assert call_governed(gov, ln, a.list_products).ok, "the lane must reopen after the Retry-After"

    gov.observe(ln, ThrottleStatus(currently_available=150, maximum_available=1000, restore_rate_per_second=50))
    res = call_governed(gov, ln, lambda: None, cost=400)
    assert res.backoff == Backoff(5.0), f"backoff must be deficit/restore rate = 250/50 = 5s, got {res.backoff}"


ALL_CHECKS: Tuple[Callable[..., None], ...] = (
    check_list_products_canonical, check_preflight_blocks_known_violation, check_send_reply_idempotent,
    check_unsupported_capabilities_fail_before_side_effects, check_rate_limit_backoff)


def run_all(h: Harness) -> List[Tuple[str, Optional[str]]]:
    """Run every check; returns (check name, failure message or None). For real adapters' own test modules."""
    out = []
    for check in ALL_CHECKS:
        try:
            check(h)
            out.append((check.__name__, None))
        except AssertionError as exc:
            out.append((check.__name__, str(exc) or "assertion failed"))
    return out

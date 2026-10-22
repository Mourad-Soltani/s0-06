"""S1-04: the contract suite passes on the mock adapter in several capability profiles, AND fails on deliberately
broken adapters (a suite that cannot fail proves nothing). No database. Names are CT*."""
import types
from dataclasses import replace

import pytest

from adapters.governed import call_governed
from contracts.harnesses import mock_harness, profile
from contracts.suite import (ALL_CHECKS, check_list_products_canonical, check_preflight_blocks_known_violation,
                             check_rate_limit_backoff, check_send_reply_idempotent,
                             check_unsupported_capabilities_fail_before_side_effects, run_all)
from adapters.errors import RateLimited
from shared.governor import Backoff, BucketConfig, InProcessGovernor, Permit, lane
from shared.models import ComplianceVerdict, ListingResult, ListingStatus, Marketplace, SendResult, Verdict

PROFILES = [(Marketplace.ETSY, profile(True, True, True, True)),          # everything on
            (Marketplace.GUMROAD, profile(False, True, True, False)),     # offer codes and checkout links only
            (Marketplace.SHOPIFY, profile(True, False, True, False))]     # listings and checkout, no offers


@pytest.mark.parametrize("check", ALL_CHECKS)
@pytest.mark.parametrize("marketplace,caps", PROFILES)
def test_CT1_mock_passes_every_contract_check(marketplace, caps, check):
    check(mock_harness(marketplace, caps))


def broken(h, mutate):
    return replace(h, make=lambda: mutate(h.make()))


def non_idempotent_send(a):
    def _send_reply(self, conversation_id, body, idempotency_key):
        self._gate("send_reply")
        self.sent_log.append((conversation_id, idempotency_key))               # ignores the key entirely
        return SendResult(external_message_id=self._next("msg"))
    a._send_reply = types.MethodType(_send_reply, a)
    return a


def test_CT2_idempotency_check_fails_an_adapter_that_ignores_the_key():
    with pytest.raises(AssertionError):
        check_send_reply_idempotent(broken(mock_harness(), non_idempotent_send))


def test_CT3_preflight_check_fails_an_adapter_that_passes_everything():
    def lenient(a):
        a.preflight_check = lambda action: ComplianceVerdict(verdict=Verdict.PASS)
        return a
    with pytest.raises(AssertionError):
        check_preflight_blocks_known_violation(broken(mock_harness(), lenient))


def test_CT3b_preflight_check_fails_an_adapter_that_blocks_without_a_reason_or_blocks_good_actions():
    def paranoid(a):
        a.preflight_check = lambda action: ComplianceVerdict(verdict=Verdict.BLOCK, reason_codes=("ALWAYS",))
        return a
    with pytest.raises(AssertionError):
        check_preflight_blocks_known_violation(broken(mock_harness(), paranoid))


@pytest.mark.parametrize("flaw", ["duplicates", "not_models", "missing"])
def test_CT4_catalog_check_fails_malformed_listings(flaw):
    def mutate(a):
        orig = a.list_products
        a.list_products = {"duplicates": lambda: [p for p in orig() for _ in (0, 1)],
                           "not_models": lambda: [{"external_id": "seed-0"}],
                           "missing": lambda: []}[flaw]
        return a
    with pytest.raises(AssertionError):
        check_list_products_canonical(broken(mock_harness(), mutate))


class NaiveGovernor:
    """Grants everything and learns nothing from a 429."""
    def acquire(self, ln, cost=1.0): return Permit(cost)
    def observe(self, ln, status): pass
    def observe_rate_limit(self, ln, retry_after_seconds=None): pass


class GuessingGovernor:
    """Backs off, but by a fixed guess instead of the computed time."""
    def __init__(self): self.blocked = False
    def acquire(self, ln, cost=1.0): return Backoff(1.0) if self.blocked else Permit(cost)
    def observe(self, ln, status): pass
    def observe_rate_limit(self, ln, retry_after_seconds=None): self.blocked = True


@pytest.mark.parametrize("governor", [NaiveGovernor, GuessingGovernor])
def test_CT5_rate_limit_check_fails_governors_that_do_not_compute_the_backoff(governor):
    with pytest.raises(AssertionError):
        check_rate_limit_backoff(mock_harness(), lambda clock, ln: governor())


def test_CT6_capability_check_fails_an_adapter_whose_undeclared_operation_does_not_refuse():
    def leaky(a):
        a.create_or_update_listing = lambda listing: ListingResult(external_id="x", status=ListingStatus.DRAFT)
        return a
    with pytest.raises(AssertionError):
        check_unsupported_capabilities_fail_before_side_effects(
            broken(mock_harness(Marketplace.GUMROAD, profile(False, True, True, False)), leaky))


def test_CT7_run_all_reports_each_failure_by_name_and_passes_a_good_adapter():
    good = dict(run_all(mock_harness()))
    assert set(good) == {c.__name__ for c in ALL_CHECKS} and all(v is None for v in good.values())
    bad = dict(run_all(broken(mock_harness(), non_idempotent_send)))
    assert bad["check_send_reply_idempotent"] and bad["check_list_products_canonical"] is None


def test_CT8_call_governed_turns_a_429_into_a_backoff_and_does_not_call_while_blocked():
    clock = type("C", (), {"t": 0.0, "__call__": lambda s: s.t})()
    ln = lane(Marketplace.ETSY, "acct")
    gov = InProcessGovernor({ln: BucketConfig(capacity=10, refill_per_second=5)}, clock=clock)
    calls = []

    def op():
        calls.append(1)
        if len(calls) == 1:
            raise RateLimited(4.0)
        return "done"

    assert call_governed(gov, ln, op).backoff == Backoff(4.0) and len(calls) == 1
    assert call_governed(gov, ln, op).backoff == Backoff(4.0) and len(calls) == 1      # blocked: not called
    clock.t = 4.0
    res = call_governed(gov, ln, op)
    assert res.ok and res.value == "done" and len(calls) == 2


def test_CT9_harness_hooks_observe_the_adapter():
    h = mock_harness()
    a = h.make()
    ids = h.seed_products(a, 2)
    assert ids == ["seed-0", "seed-1"] and [p.status for p in a.list_products()] == [ListingStatus.ACTIVE] * 2
    assert h.adapter_calls(a) == 1 and h.external_effects(a) == 0
    conv = h.seed_conversation(a)
    import uuid
    a.send_reply(conv, "hi", uuid.uuid4())
    assert h.external_effects(a) == 1
    assert mock_harness(Marketplace.GUMROAD, profile(False, True, True, False)).seed_conversation(
        mock_harness(Marketplace.GUMROAD, profile(False, True, True, False)).make()) is None

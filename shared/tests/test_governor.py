"""Governor unit tests: no database, no pydantic, deterministic fake clock. Names are G* (cannot collide
with the CI gate's prefixes or the mutation step's -k T10)."""
import enum
import threading

import pytest

from shared.governor import (Backoff, BucketConfig, CostExceedsCapacity, InProcessGovernor, Lane, Permit,
                             ThrottleStatus, UnknownLane, lane)

L = Lane("ETSY", "shop-1")


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def gov(cfg, clock=None, **kw):
    c = clock or Clock()
    return InProcessGovernor({L: cfg}, clock=c, **kw), c


def test_G1_permits_until_empty_then_a_computed_backoff():
    g, c = gov(BucketConfig(capacity=10, refill_per_second=2))
    assert g.acquire(L, 4) == Permit(4) and g.acquire(L, 4) == Permit(4)    # 2 tokens left
    assert g.acquire(L, 4) == Backoff(1.0)                                  # deficit 2 / rate 2
    c.advance(1.0)
    assert g.acquire(L, 4) == Permit(4)


def test_G2_backoff_is_deficit_over_restore_rate_with_a_reserve():
    g, c = gov(BucketConfig(capacity=1000, refill_per_second=50, reserve=200))
    assert g.acquire(L, 800) == Permit(800)                                 # tokens 200, exactly the reserve
    assert g.acquire(L, 100) == Backoff(2.0)                                # (100 + 200 - 200) / 50
    g.observe(L, ThrottleStatus(currently_available=150, maximum_available=1000, restore_rate_per_second=50))
    assert g.acquire(L, 100) == Backoff(3.0)                                # (100 + 200 - 150) / 50: server view wins
    c.advance(3.0)
    assert g.acquire(L, 100) == Permit(100)


def test_G3_refill_is_capped_at_capacity():
    g, c = gov(BucketConfig(capacity=10, refill_per_second=5))
    g.acquire(L, 10)
    c.advance(10_000)
    assert g.snapshot(L)["tokens"] == 10


def test_G4_never_spends_into_the_reserve():
    g, _ = gov(BucketConfig(capacity=100, refill_per_second=1, reserve=30))
    assert g.acquire(L, 70) == Permit(70)
    assert isinstance(g.acquire(L, 1), Backoff)


def test_G5_a_cost_that_can_never_fit_is_an_error_not_a_backoff():
    g, _ = gov(BucketConfig(capacity=1000, refill_per_second=50, reserve=200))
    with pytest.raises(CostExceedsCapacity):
        g.acquire(L, 801)
    assert g.acquire(L, 800) == Permit(800)


def test_G6_a_429_blocks_the_lane_even_with_tokens_left():
    g, c = gov(BucketConfig(capacity=100, refill_per_second=100))
    g.observe_rate_limit(L, 5.0)
    assert g.acquire(L, 1) == Backoff(5.0)
    c.advance(2.0)
    assert g.acquire(L, 1) == Backoff(3.0)
    c.advance(3.0)
    assert g.acquire(L, 1) == Permit(1)
    g.observe_rate_limit(L)                                                 # no Retry-After given: the default
    assert g.acquire(L, 1) == Backoff(1.0)


def test_G6b_the_longer_of_block_and_deficit_wins():
    g, c = gov(BucketConfig(capacity=10, refill_per_second=1))
    g.observe_rate_limit(L, 2.0)                                            # tokens drained to 0
    assert g.acquire(L, 5) == Backoff(5.0)                                  # deficit 5 / 1 outlasts the 2s block
    c.advance(5.0)
    assert g.acquire(L, 5) == Permit(5)


def test_G7_observe_adopts_server_state_and_clamps_to_capacity():
    g, _ = gov(BucketConfig(capacity=1000, refill_per_second=50))
    g.observe(L, ThrottleStatus(currently_available=5000, maximum_available=2000, restore_rate_per_second=100))
    snap = g.snapshot(L)
    assert (snap["capacity"], snap["tokens"]) == (2000, 2000)


class M(str, enum.Enum):
    ETSY = "ETSY"


def test_G8_lanes_are_independent_unknown_lanes_fail_and_enums_normalise():
    other = Lane("SHOPIFY", "s1")
    g = InProcessGovernor({L: BucketConfig(1, 1), other: BucketConfig(1, 1)}, clock=Clock())
    assert g.acquire(L) == Permit(1) and isinstance(g.acquire(L), Backoff)
    assert g.acquire(other) == Permit(1)                                    # untouched by the other lane
    with pytest.raises(UnknownLane):
        g.acquire(Lane("GUMROAD", "x"))
    assert lane(M.ETSY, "shop-1") == L


def test_G9_concurrent_callers_never_overspend():
    g, _ = gov(BucketConfig(capacity=100, refill_per_second=0.001))
    granted, lock = [], threading.Lock()

    def worker():
        for _ in range(50):
            if isinstance(g.acquire(L, 1), Permit):
                with lock:
                    granted.append(1)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(granted) == 100                                              # exactly the budget, no more


def test_G10_validation():
    for bad in (dict(capacity=0, refill_per_second=1), dict(capacity=1, refill_per_second=0),
                dict(capacity=10, refill_per_second=1, reserve=10), dict(capacity=10, refill_per_second=1, reserve=-1),
                dict(capacity=10, refill_per_second=1, initial=11)):
        with pytest.raises(ValueError):
            BucketConfig(**bad)
    g, _ = gov(BucketConfig(capacity=10, refill_per_second=1))
    for bad_cost in (0, -1):
        with pytest.raises(ValueError):
            g.acquire(L, bad_cost)
    with pytest.raises(ValueError):
        g.observe_rate_limit(L, 0)
    for bad_status in (ThrottleStatus(-1, 10, 1), ThrottleStatus(5, 0, 1), ThrottleStatus(5, 10, 0)):
        with pytest.raises(ValueError):
            g.observe(L, bad_status)
    with pytest.raises(ValueError):
        InProcessGovernor(default_retry_after_seconds=0)


def test_G11_initial_tokens_and_add_lane():
    g = InProcessGovernor(clock=Clock())
    g.add_lane(L, BucketConfig(capacity=10, refill_per_second=1, initial=2))
    assert g.acquire(L, 2) == Permit(2) and g.acquire(L, 1) == Backoff(1.0)

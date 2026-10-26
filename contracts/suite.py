from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from adapters.errors import RateLimited
from shared.models import Verdict

@dataclass
class Harness:
    name: str
    make: Callable
    seed_products: Callable
    seed_conversation: Callable
    inject_rate_limit: Callable
    known_violation_action: Callable
    compliant_action: Callable
    external_effects: Callable
    adapter_calls: Callable
    rate_limit_op: str

def _check_preflight_passes_compliant(h: Harness):
    a = h.make()
    v = a.preflight_check(h.compliant_action(a))
    assert v.verdict is Verdict.PASS

def _check_preflight_is_pure(h: Harness):
    a = h.make()
    h.seed_products(a, 1)
    before = h.adapter_calls(a)
    a.preflight_check(h.compliant_action(a))
    assert h.adapter_calls(a) == before

def _check_preflight_blocks_violation(h: Harness):
    a = h.make()
    h.seed_products(a, 1)
    before = h.external_effects(a)
    v = a.preflight_check(h.known_violation_action(a))
    assert v.verdict is Verdict.BLOCK
    assert h.external_effects(a) == before

def _check_health_ok(h: Harness):
    a = h.make()
    assert a.health_check().ok is True

def _check_list_products(h: Harness):
    a = h.make()
    ids = h.seed_products(a, 3)
    got = {p.external_id for p in a.list_products()}
    assert got == set(ids)

def _check_rate_limit_propagates(h: Harness):
    a = h.make()
    h.seed_products(a, 1)
    h.inject_rate_limit(a, h.rate_limit_op, 5.0)
    try:
        a.list_products()
    except RateLimited:
        return
    raise AssertionError("expected RateLimited to propagate")

ALL_CHECKS = [
    _check_preflight_passes_compliant,
    _check_preflight_is_pure,
    _check_preflight_blocks_violation,
    _check_health_ok,
    _check_list_products,
    _check_rate_limit_propagates,
]

def run_all(harness: Harness):
    results = []
    for check in ALL_CHECKS:
        try:
            check(harness)
            results.append((check.__name__, None))
        except AssertionError as e:
            results.append((check.__name__, f"AssertionError: {e}"))
        except Exception as e:
            results.append((check.__name__, f"{type(e).__name__}: {e}"))
    return results

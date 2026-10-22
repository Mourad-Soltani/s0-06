"""Glue between an adapter call and the rate governor: the one place a RateLimited error becomes a Backoff.
Kept out of shared/governor.py so the governor stays free of adapter types."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from adapters.errors import RateLimited
from shared.governor import Backoff, Governor, Lane


@dataclass(frozen=True)
class GovernedResult:
    value: Any = None
    backoff: Optional[Backoff] = None      # set when the call did not (or could not) go ahead

    @property
    def ok(self) -> bool:
        return self.backoff is None


def call_governed(governor: Governor, ln: Lane, fn: Callable[[], Any], cost: float = 1.0) -> GovernedResult:
    """Acquire a permit, make the call, and turn a 429 into a computed backoff. While the lane is blocked the
    adapter is never called at all."""
    permit = governor.acquire(ln, cost)
    if isinstance(permit, Backoff):
        return GovernedResult(backoff=permit)
    try:
        return GovernedResult(value=fn())
    except RateLimited as exc:
        governor.observe_rate_limit(ln, exc.retry_after_seconds)
        nxt = governor.acquire(ln, cost)
        return GovernedResult(backoff=nxt if isinstance(nxt, Backoff) else Backoff(0.0))

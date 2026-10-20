"""Rate governor (spec 5.2, S1-05): one shared budget per marketplace-account lane.

Backoff is CALCULATED from the budget, never guessed: wait = deficit / restore rate. In-process and
single-worker for v1.1 (ADR-13); LaneLock (shared/lane_lock.py) makes a second worker fail fast instead of
silently double-spending a lane. The Governor protocol is the seam for a Redis backend later.
No database, no marketplace, no pydantic imports: lanes are plain strings.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Mapping, Optional, Protocol, Union


class UnknownLane(KeyError):
    """The lane was never configured. Nothing is rate-limited by guesswork."""


class CostExceedsCapacity(ValueError):
    """The requested cost can never be granted (it exceeds capacity minus reserve)."""


@dataclass(frozen=True)
class Lane:
    marketplace: str
    account_id: str


def lane(marketplace, account_id: str) -> Lane:
    """Build a Lane from a Marketplace enum member or a plain string (normalised to its value)."""
    return Lane(str(getattr(marketplace, "value", marketplace)), account_id)


@dataclass(frozen=True)
class BucketConfig:
    capacity: float                   # maximum tokens (requests, or cost points)
    refill_per_second: float          # restore rate
    reserve: float = 0.0              # never spend below this (Shopify: pause under ~200 points)
    initial: Optional[float] = None   # starting tokens, default full

    def __post_init__(self):
        if self.capacity <= 0 or self.refill_per_second <= 0:
            raise ValueError("capacity and refill_per_second must be positive")
        if not 0 <= self.reserve < self.capacity:
            raise ValueError("reserve must be in [0, capacity)")
        if self.initial is not None and not 0 <= self.initial <= self.capacity:
            raise ValueError("initial must be in [0, capacity]")


@dataclass(frozen=True)
class ThrottleStatus:
    """What a cost-based API reports after a call (Shopify: extensions.cost.throttleStatus)."""
    currently_available: float
    maximum_available: float
    restore_rate_per_second: float


@dataclass(frozen=True)
class Permit:
    cost: float


@dataclass(frozen=True)
class Backoff:
    seconds: float


class Governor(Protocol):
    def acquire(self, lane: Lane, estimated_cost: float = 1.0) -> Union[Permit, Backoff]: ...
    def observe(self, lane: Lane, status: ThrottleStatus) -> None: ...
    def observe_rate_limit(self, lane: Lane, retry_after_seconds: Optional[float] = None) -> None: ...


class _Bucket:
    def __init__(self, cfg: BucketConfig, now: float):
        self.capacity, self.refill, self.reserve = cfg.capacity, cfg.refill_per_second, cfg.reserve
        self.tokens = cfg.capacity if cfg.initial is None else cfg.initial
        self.last = now
        self.blocked_until = 0.0

    def refill_to(self, now: float) -> None:
        if now > self.last:
            self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.refill)
            self.last = now


class InProcessGovernor:
    def __init__(self, configs: Optional[Mapping[Lane, BucketConfig]] = None, *,
                 clock: Callable[[], float] = time.monotonic, default_retry_after_seconds: float = 1.0):
        if default_retry_after_seconds <= 0:
            raise ValueError("default_retry_after_seconds must be positive")
        self._clock = clock
        self._default_retry = default_retry_after_seconds
        self._lock = threading.Lock()
        self._buckets: Dict[Lane, _Bucket] = {}
        for ln, cfg in (configs or {}).items():
            self.add_lane(ln, cfg)

    def add_lane(self, ln: Lane, cfg: BucketConfig) -> None:
        with self._lock:
            self._buckets[ln] = _Bucket(cfg, self._clock())

    def _bucket(self, ln: Lane) -> _Bucket:
        try:
            return self._buckets[ln]
        except KeyError:
            raise UnknownLane(f"lane {ln.marketplace}/{ln.account_id} is not configured") from None

    def acquire(self, ln: Lane, estimated_cost: float = 1.0) -> Union[Permit, Backoff]:
        if estimated_cost <= 0:
            raise ValueError("estimated_cost must be positive")
        with self._lock:
            b, now = self._bucket(ln), self._clock()
            if estimated_cost > b.capacity - b.reserve:
                raise CostExceedsCapacity(f"cost {estimated_cost} can never fit capacity {b.capacity}")
            b.refill_to(now)
            waits = []
            if now < b.blocked_until:
                waits.append(b.blocked_until - now)
            deficit = estimated_cost + b.reserve - b.tokens
            if deficit > 0:
                waits.append(deficit / b.refill)
            if waits:
                return Backoff(max(waits))
            b.tokens -= estimated_cost
            return Permit(estimated_cost)

    def observe(self, ln: Lane, status: ThrottleStatus) -> None:
        """Adopt the server's view of the budget (it is authoritative over our estimate)."""
        if min(status.maximum_available, status.restore_rate_per_second) <= 0 or status.currently_available < 0:
            raise ValueError("invalid throttle status")
        with self._lock:
            b, now = self._bucket(ln), self._clock()
            b.capacity, b.refill = status.maximum_available, status.restore_rate_per_second
            b.tokens, b.last = min(status.currently_available, b.capacity), now

    def observe_rate_limit(self, ln: Lane, retry_after_seconds: Optional[float] = None) -> None:
        """The marketplace said no (HTTP 429). Block the lane and drain the bucket."""
        retry = self._default_retry if retry_after_seconds is None else retry_after_seconds
        if retry <= 0:
            raise ValueError("retry_after_seconds must be positive")
        with self._lock:
            b, now = self._bucket(ln), self._clock()
            b.refill_to(now)
            b.tokens = 0.0
            b.blocked_until = max(b.blocked_until, now + retry)

    def snapshot(self, ln: Lane) -> Dict[str, float]:
        """Current budget, for metrics and tests."""
        with self._lock:
            b, now = self._bucket(ln), self._clock()
            b.refill_to(now)
            return {"tokens": b.tokens, "capacity": b.capacity, "blocked_for": max(0.0, b.blocked_until - now)}

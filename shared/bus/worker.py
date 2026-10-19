"""Outbox worker: claim, route to subscribed consumers, ack or fail. No database imports; the store is
injected, so the control flow is unit-testable without Postgres."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Protocol, Tuple

from shared.bus.events import CONSUMER_NAME_RE, KNOWN_EVENT_TYPES, Event, error_code

Handler = Callable[[object, Event], None]


class Store(Protocol):
    def claim(self, worker: str, limit: int, lease_seconds: float, max_attempts: int) -> List[Event]: ...
    def process(self, consumer: str, event: Event, handler: Handler) -> bool: ...
    def ack(self, event_pk: int, worker: str) -> bool: ...
    def fail(self, event_pk: int, worker: str, code: str, max_attempts: int,
             backoff_seconds: float) -> Optional[str]: ...
    def reap(self, max_attempts: int) -> int: ...


@dataclass
class RunResult:
    claimed: int = 0
    done: int = 0       # events acked, including unrouted ones
    retried: int = 0    # failed and rescheduled
    dead: int = 0       # failed and dead-lettered
    unrouted: int = 0   # acked with no subscriber (subset of done)


class Worker:
    def __init__(self, store: Store, worker_id: str, *, limit: int = 10, lease_seconds: float = 60,
                 max_attempts: int = 5, backoff_seconds: float = 30):
        self.store = store
        self.worker_id = worker_id
        self.limit = limit
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds
        self._routes: Dict[str, List[Tuple[str, Handler]]] = {}

    def subscribe(self, event_type: str, consumer: str, handler: Handler) -> None:
        if event_type not in KNOWN_EVENT_TYPES:
            raise ValueError(f"unknown event type {event_type!r}")
        if not CONSUMER_NAME_RE.match(consumer):
            raise ValueError(f"invalid consumer name {consumer!r}")
        routes = self._routes.setdefault(event_type, [])
        if any(name == consumer for name, _ in routes):
            raise ValueError(f"consumer {consumer!r} already subscribed to {event_type!r}")
        routes.append((consumer, handler))

    def run_once(self) -> RunResult:
        res = RunResult()
        events = self.store.claim(self.worker_id, self.limit, self.lease_seconds, self.max_attempts)
        res.claimed = len(events)
        for ev in events:
            routes = self._routes.get(ev.event_type, [])
            try:
                for consumer, handler in routes:
                    self.store.process(consumer, ev, handler)
            except Exception as exc:  # noqa: BLE001 - any handler failure is a retry, never a crash
                status = self.store.fail(ev.id, self.worker_id, error_code(exc),
                                         self.max_attempts, self.backoff_seconds)
                if status == "DEAD":
                    res.dead += 1
                elif status == "PENDING":
                    res.retried += 1
                continue   # status None: the claim was lost to another worker, who now owns this event
            if self.store.ack(ev.id, self.worker_id):
                res.done += 1
                if not routes:
                    res.unrouted += 1
        return res

    def drain(self, max_rounds: int = 100) -> RunResult:
        """Call run_once until a round claims nothing, or max_rounds. Totals the results."""
        total = RunResult()
        for _ in range(max_rounds):
            r = self.run_once()
            total.claimed += r.claimed
            total.done += r.done
            total.retried += r.retried
            total.dead += r.dead
            total.unrouted += r.unrouted
            if r.claimed == 0:
                break
        return total

    def reap(self) -> int:
        """Dead-letter events that exhausted their attempts without an explicit failure (crashed workers)."""
        return self.store.reap(self.max_attempts)

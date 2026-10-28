"""Scout (S2-03): reads inbound signals from adapters and turns each new one into a lead plus a signal.captured event.
It talks to adapters through the MarketplaceAdapter interface only, through the rate governor. In v1.1 every signal
is a structured marketplace fact, so no LLM is involved (ADR-17). Intake is throttled to the reviewer's capacity
(ADR-03): it pauses while the review queue is deep and stops at a daily cap. Both pauses leave the cursor where it
was, so nothing is skipped; duplicates are impossible because the lead's signal_id is unique."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Mapping, Optional, Protocol, Tuple

from adapters.governed import call_governed
from shared.bus.events import error_code
from shared.governor import Governor, lane as make_lane


@dataclass(frozen=True)
class ScoutConfig:
    lookback: timedelta = timedelta(days=7)      # how far back to read when a lane has no cursor yet
    max_review_depth: int = 30                   # ADR-03: the reviewer's upper capacity target, per day
    max_new_leads_per_day: int = 30


class LeadStore(Protocol):
    def queue_depth(self) -> int: ...
    def new_leads_today(self) -> int: ...
    def get_cursor(self, marketplace: str, account_id: str) -> Optional[datetime]: ...
    def ingest(self, signal, marketplace: str) -> bool: ...        # True if a NEW lead was created
    def set_cursor(self, marketplace: str, account_id: str, since: datetime) -> None: ...


@dataclass
class LaneResult:
    marketplace: str
    account_id: str
    status: str = "ok"           # ok | backoff | paused_backpressure | daily_cap | error
    new_leads: int = 0
    seen: int = 0
    error_code: Optional[str] = None
    backoff_seconds: Optional[float] = None


class Scout:
    def __init__(self, store: LeadStore, governor: Governor, lanes: Mapping[Tuple[str, str], object],
                 config: Optional[ScoutConfig] = None, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.store, self.governor, self.lanes = store, governor, dict(lanes)
        self.config, self._clock = config or ScoutConfig(), clock

    def run_once(self) -> List[LaneResult]:
        return [self._run_lane(mp, account, adapter) for (mp, account), adapter in self.lanes.items()]

    def _run_lane(self, mp: str, account: str, adapter) -> LaneResult:
        res, cfg = LaneResult(mp, account), self.config
        try:
            if self.store.queue_depth() >= cfg.max_review_depth:
                res.status = "paused_backpressure"
                return res
            room = cfg.max_new_leads_per_day - self.store.new_leads_today()
            if room <= 0:
                res.status = "daily_cap"
                return res
            since = self.store.get_cursor(mp, account) or (self._clock() - cfg.lookback)
            got = call_governed(self.governor, make_lane(mp, account), lambda: adapter.fetch_signals(since))
            if not got.ok:                                     # backing off: the adapter was not called
                res.status, res.backoff_seconds = "backoff", got.backoff.seconds
                return res
            last = None
            for sig in sorted(got.value, key=lambda s: s.occurred_at):
                if res.new_leads >= room:
                    res.status = "daily_cap"
                    break
                res.seen += 1
                if self.store.ingest(sig, mp):
                    res.new_leads += 1
                last = sig.occurred_at                         # duplicates advance the cursor too
            if last is not None:
                self.store.set_cursor(mp, account, last)
        except Exception as exc:  # noqa: BLE001 - one lane failing must not stop the others
            res.status, res.error_code = "error", error_code(exc)
        return res

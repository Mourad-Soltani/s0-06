"""Scout orchestration with a fake store, fake adapters and a fake clock: no database. Names SO*."""
from datetime import datetime, timedelta, timezone

import pytest

from adapters.errors import AuthExpired, RateLimited
from orchestrator.scout import Scout, ScoutConfig
from shared.governor import BucketConfig, InProcessGovernor, lane
from shared.models import Marketplace, Signal

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
LANE = ("GUMROAD", "acct")


def sig(ref, minutes_ago):
    return Signal(signal_ref=ref, kind="SALE", marketplace=Marketplace.GUMROAD, product_external_id="gr-1",
                  occurred_at=NOW - timedelta(minutes=minutes_ago))


class Adapter:
    def __init__(self, signals=(), fault=None):
        self.signals, self.fault, self.calls = list(signals), fault, []

    def fetch_signals(self, since):
        self.calls.append(since)
        if self.fault:
            raise self.fault
        return [s for s in self.signals if s.occurred_at >= since]


class Store:
    def __init__(self, depth=0, today=0, cursor=None):
        self.depth, self.today, self.cursors, self.leads = depth, today, {}, []
        if cursor:
            self.cursors[LANE] = cursor

    def queue_depth(self):
        return self.depth

    def new_leads_today(self):
        return self.today + len(self.leads)

    def get_cursor(self, mp, account):
        return self.cursors.get((mp, account))

    def ingest(self, signal, mp):
        key = (mp, signal.signal_ref)
        if key in [k for k, _ in self.leads]:
            return False
        self.leads.append((key, signal))
        return True

    def set_cursor(self, mp, account, since):
        self.cursors[(mp, account)] = max(since, self.cursors.get((mp, account), since))


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


def scout(store, adapters, **cfg):
    gov = InProcessGovernor({lane(*k): BucketConfig(capacity=100, refill_per_second=100) for k in adapters}, clock=Clock())
    return Scout(store, gov, adapters, ScoutConfig(**cfg), clock=lambda: NOW), gov


def test_SO1_new_signals_become_leads_in_time_order_and_the_cursor_advances():
    a, st = Adapter([sig("c", 5), sig("a", 50), sig("b", 20)]), Store()
    (r,) = scout(st, {LANE: a})[0].run_once()
    assert (r.status, r.new_leads, r.seen) == ("ok", 3, 3)
    assert [k[1] for k, _ in st.leads] == ["a", "b", "c"]
    assert st.cursors[LANE] == NOW - timedelta(minutes=5)


def test_SO2_duplicates_are_not_counted_but_still_advance_the_cursor():
    a, st = Adapter([sig("a", 50), sig("b", 20)]), Store()
    s, _ = scout(st, {LANE: a})
    s.run_once()
    (r,) = s.run_once()                                       # the cursor is inclusive, so "b" comes back
    assert (r.status, r.new_leads, r.seen) == ("ok", 0, 1) and len(st.leads) == 2
    assert a.calls[1] == NOW - timedelta(minutes=20)          # second read started at the stored cursor


def test_SO3_backpressure_pauses_intake_without_calling_the_adapter():
    a, st = Adapter([sig("a", 5)]), Store(depth=30)
    (r,) = scout(st, {LANE: a}, max_review_depth=30)[0].run_once()
    assert r.status == "paused_backpressure" and a.calls == [] and st.leads == [] and st.cursors == {}
    st.depth = 29
    assert scout(st, {LANE: a}, max_review_depth=30)[0].run_once()[0].new_leads == 1


def test_SO4_the_daily_cap_stops_mid_batch_and_the_cursor_stops_with_it():
    a, st = Adapter([sig("a", 50), sig("b", 40), sig("c", 30)]), Store(today=28)
    (r,) = scout(st, {LANE: a}, max_new_leads_per_day=30)[0].run_once()
    assert (r.status, r.new_leads) == ("daily_cap", 2) and [k[1] for k, _ in st.leads] == ["a", "b"]
    assert st.cursors[LANE] == NOW - timedelta(minutes=40)    # "c" is picked up on the next run
    full = Store(today=30)
    (r2,) = scout(full, {LANE: Adapter([sig("x", 5)])}, max_new_leads_per_day=30)[0].run_once()
    assert r2.status == "daily_cap" and full.leads == []


def test_SO5_a_blocked_lane_backs_off_without_calling_the_adapter():
    a, st = Adapter([sig("a", 5)]), Store()
    s, gov = scout(st, {LANE: a})
    gov.observe_rate_limit(lane(*LANE), 9.0)
    (r,) = s.run_once()
    assert (r.status, r.backoff_seconds) == ("backoff", 9.0) and a.calls == [] and st.leads == []


def test_SO6_an_adapter_429_becomes_a_backoff_and_moves_nothing():
    a, st = Adapter([sig("a", 5)], fault=RateLimited(4.0)), Store()
    s, gov = scout(st, {LANE: a})
    (r,) = s.run_once()
    assert (r.status, r.backoff_seconds) == ("backoff", 4.0) and st.leads == [] and st.cursors == {}
    assert len(a.calls) == 1 and s.run_once()[0].status == "backoff" and len(a.calls) == 1   # blocked: not called again


def test_SO7_one_failing_lane_does_not_stop_the_others():
    bad, good = Adapter(fault=AuthExpired()), Adapter([sig("a", 5)])
    st = Store()
    s, _ = scout(st, {("ETSY", "shop"): bad, LANE: good})
    results = {(r.marketplace, r.account_id): r for r in s.run_once()}
    assert results[("ETSY", "shop")].status == "error" and results[("ETSY", "shop")].error_code == "AUTH_EXPIRED"
    assert results[LANE].status == "ok" and results[LANE].new_leads == 1


def test_SO8_an_unconfigured_lane_is_reported_not_guessed():
    st = Store()
    gov = InProcessGovernor({}, clock=Clock())
    (r,) = Scout(st, gov, {LANE: Adapter([sig("a", 5)])}, clock=lambda: NOW).run_once()
    assert (r.status, r.error_code) == ("error", "UNKNOWN_LANE") and st.leads == []


def test_SO9_the_first_read_goes_back_by_the_lookback():
    a = Adapter()
    scout(Store(), {LANE: a}, lookback=timedelta(days=3))[0].run_once()
    assert a.calls == [NOW - timedelta(days=3)]

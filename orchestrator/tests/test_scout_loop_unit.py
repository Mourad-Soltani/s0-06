"""The Scout loop inside the worker: lanes and throttles, the cycle's logging, failure isolation, and where the adapters read webhook signals from.
No database, no network. Names SC*."""
import io
import logging

import pytest

from orchestrator import worker_main
from orchestrator.worker_main import SCOUT_ACCOUNT, ScoutLoop, build_product_adapters, scout_serve
from shared.config import load_settings
from shared.governor import Permit, lane

GOOD = {
    "DATABASE_URL": "postgresql://sap_worker:s3cret-pw@db:5432/sap", "POSTMARK_SERVER_TOKEN": "pm-token-abcdef", "EMAIL_FROM": "hello@seller.example.com",
    "EMAIL_POSTAL_ADDRESS": "1 Example Street, Paris 75001, France", "EMAIL_UNSUBSCRIBE_URL": "https://seller.example.com/unsubscribe",
    "ALLOWED_LINK_HOSTS": "gumroad.com", "ICP_PRODUCT_IDS": "gr-1", "GUMROAD_ACCESS_TOKEN": "gum-token-xyz"}


class Stop:
    def __init__(self, cycles):
        self.cycles, self.waits = cycles, []

    def is_set(self):
        self.cycles -= 1
        return self.cycles < 0

    def wait(self, t):
        self.waits.append(t)


class Result:
    def __init__(self, marketplace="GUMROAD", status="ok", new_leads=0, seen=0, error_code=None):
        self.marketplace, self.account_id, self.status, self.new_leads, self.seen, self.error_code = marketplace, "default", status, new_leads, seen, error_code


class FakeLoop:
    def __init__(self, *cycles):
        self.cycles, self.runs = list(cycles), 0

    def run_once(self):
        self.runs += 1
        item = self.cycles.pop(0) if self.cycles else []
        if isinstance(item, BaseException):
            raise item
        return item


def capture(fn):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    worker_main.log.addHandler(handler)
    level = worker_main.log.level
    worker_main.log.setLevel(logging.INFO)
    try:
        fn()
    finally:
        worker_main.log.removeHandler(handler)
        worker_main.log.setLevel(level)
    return stream.getvalue()


def test_SC1_the_loop_runs_a_cycle_then_waits_the_interval_until_stopped():
    stop, loop = Stop(3), FakeLoop([], [], [])
    scout_serve(loop, stop, interval=45.0)
    assert loop.runs == 3 and stop.waits == [45.0, 45.0, 45.0]


def test_SC2_only_cycles_that_found_something_or_went_wrong_are_logged_and_only_with_counts_and_codes():
    loop = FakeLoop([Result(new_leads=0, seen=0)], [Result("LEMON_SQUEEZY", new_leads=2, seen=3)], [Result("GUMROAD", status="backoff")],
                    [Result("PAYHIP", status="error", error_code="TRANSPORT_ERROR")])
    text = capture(lambda: scout_serve(loop, Stop(4), interval=1))
    lines = text.strip().splitlines()
    assert lines == ["scout LEMON_SQUEEZY/default: ok new=2 seen=3", "scout GUMROAD/default: backoff new=0 seen=0",
                     "scout PAYHIP/default: error new=0 seen=0 error=TRANSPORT_ERROR"]


def test_SC3_a_cycle_that_raises_is_logged_by_class_only_and_the_next_cycle_still_runs():
    loop = FakeLoop(RuntimeError("jane.doe@example.test must not be logged"), [Result(new_leads=1, seen=1)])
    text = capture(lambda: scout_serve(loop, Stop(2), interval=1))
    assert loop.runs == 2 and "scout cycle failed: RUNTIME_ERROR" in text and "jane.doe" not in text and "new=1" in text


def test_SC4_the_loop_has_a_lane_and_a_throttle_for_each_configured_marketplace_and_nothing_else():
    s = load_settings({**GOOD, "LEMON_SQUEEZY_API_KEY": "k", "LEMON_SQUEEZY_STORE_ID": "7"})
    adapters = {"GUMROAD": object(), "LEMON_SQUEEZY": object()}
    loop = ScoutLoop(s, connect=lambda: None, inbox=object(), adapters=adapters)
    assert set(loop.lanes) == {("GUMROAD", SCOUT_ACCOUNT), ("LEMON_SQUEEZY", SCOUT_ACCOUNT)} and SCOUT_ACCOUNT == "default"
    assert isinstance(loop.governor.acquire(lane("GUMROAD", SCOUT_ACCOUNT)), Permit)
    with pytest.raises(Exception):
        loop.governor.acquire(lane("PAYHIP", SCOUT_ACCOUNT))                       # an unconfigured marketplace has no lane


def test_SC5_webhook_marketplaces_read_the_inbox_and_the_drafting_adapters_do_not(tmp_path):
    catalog = tmp_path / "payhip.json"
    catalog.write_text('[{"key": "RGsF", "name": "Planner", "price_minor": 1900}]')
    s = load_settings({**GOOD, "LEMON_SQUEEZY_API_KEY": "k", "LEMON_SQUEEZY_STORE_ID": "7", "PAYHIP_API_KEY": "p", "PAYHIP_CATALOG_FILE": str(catalog)})
    inbox = object()
    with_inbox, without = build_product_adapters(s, signals=inbox), build_product_adapters(s)
    assert set(with_inbox) == set(without) == {"GUMROAD", "LEMON_SQUEEZY", "PAYHIP"}
    assert with_inbox["LEMON_SQUEEZY"]._signals is inbox and with_inbox["PAYHIP"]._signals is inbox
    assert without["LEMON_SQUEEZY"]._signals is None and without["PAYHIP"]._signals is None


def test_SC6_the_scout_poll_interval_is_a_setting_where_zero_turns_it_off():
    assert load_settings(GOOD).scout_poll_seconds == 30.0
    assert load_settings({**GOOD, "SCOUT_POLL_SECONDS": "0"}).scout_poll_seconds == 0.0
    assert load_settings({**GOOD, "SCOUT_POLL_SECONDS": "120"}).scout_poll_seconds == 120.0
    from shared.config import ConfigError
    for bad in ("-1", "3601", "abc"):
        with pytest.raises(ConfigError):
            load_settings({**GOOD, "SCOUT_POLL_SECONDS": bad})

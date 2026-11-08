"""The worker's composition root and loop, over fakes: what gets wired, that routing is off unless switched on, the sweep timing, the start-up checks,
and that a bad configuration prints names only. No database, no network. Names WM*."""
import contextlib
import io
import json
import threading

import pytest

from adapters.email_channel.fake_transport import FakeEmailTransport
from orchestrator import worker_main
from orchestrator.llm_providers import AnthropicLlmClient, OpenAiLlmClient
from orchestrator.worker_main import NO_MODEL, LogNotifier, build, load_payhip_catalog, main, run_checks, serve
from shared.config import ConfigError, load_settings

GOOD = {
    "DATABASE_URL": "postgresql://sap_worker:s3cret-pw@db:5432/sap", "POSTMARK_SERVER_TOKEN": "pm-token-abcdef", "EMAIL_FROM": "hello@seller.example.com",
    "EMAIL_POSTAL_ADDRESS": "1 Example Street, Paris 75001, France", "EMAIL_UNSUBSCRIBE_URL": "https://seller.example.com/unsubscribe",
    "ALLOWED_LINK_HOSTS": "gumroad.com", "ICP_PRODUCT_IDS": "gr-1", "GUMROAD_ACCESS_TOKEN": "gum-token-xyz"}


class Health:
    def __init__(self, ok=True, boom=False):
        self.ok, self.boom = ok, boom

    def health_check(self):
        if self.boom:
            raise RuntimeError("jane.doe@example.test leaked in a message")
        return self


class Adapter(Health):
    pass


class Conn:
    def __init__(self, answer=1):
        self.answer = answer

    def execute(self, sql, params=()):
        class Cur:
            def fetchone(inner):
                return (self.answer,)
        return Cur()


def settings(**extra):
    return load_settings({**GOOD, **extra})


def parts_for(s, adapters=None):
    return build(s, connect=lambda: None, bus_conn=Conn(), product_adapters=adapters or {"GUMROAD": Adapter()}, email_transport=FakeEmailTransport())


# ------------------------------------------------------------------ build
def test_WM1_the_whole_chain_is_registered_on_one_worker_and_the_default_adapter_is_the_first_marketplace():
    parts = parts_for(settings())
    subs = {etype: [name for name, _ in routes] for etype, routes in parts.worker._routes.items()}
    assert subs == {"signal.captured": ["qualifier"], "lead.qualified": ["drafter"], "action.proposed": ["compliance_guard"], "action.approved": ["closer"]}
    assert parts.default_marketplace == "GUMROAD" and parts.worker.worker_id == "worker-1" and set(parts.adapters) == {"GUMROAD"}


def test_WM2_routing_is_off_unless_switched_on_and_then_both_the_drafter_and_the_closer_get_every_adapter():
    adapters = {"GUMROAD": Adapter(), "LEMON_SQUEEZY": Adapter()}
    off = parts_for(settings(LEMON_SQUEEZY_API_KEY="k", LEMON_SQUEEZY_STORE_ID="1"), adapters)
    assert off.closer.product_adapters == {} and off.worker._routes["lead.qualified"][0][1].__self__.router is None
    on = parts_for(settings(LEMON_SQUEEZY_API_KEY="k", LEMON_SQUEEZY_STORE_ID="1", ROUTING_ENABLED="true"), adapters)
    drafter = on.worker._routes["lead.qualified"][0][1].__self__
    assert set(on.closer.product_adapters) == set(drafter.adapters) == {"GUMROAD", "LEMON_SQUEEZY"} and drafter.router is not None


def test_WM3_without_a_model_every_role_is_template_only_and_nothing_is_priced():
    assert set(NO_MODEL) == {"SCOUT", "QUALIFIER", "COPYWRITER", "CLOSER", "COMPLIANCE_GUARD", "ANALYST"} and not any(NO_MODEL.values())


def test_WM4_the_worker_id_and_the_guards_settings_come_from_the_environment():
    parts = parts_for(settings(WORKER_ID="w-9", ALLOWED_LINK_HOSTS="gumroad.com,buy.example.com", MAX_DISCOUNT_PCT="15"))
    guard = parts.closer.guard
    assert parts.worker.worker_id == "w-9" and guard.config.allowed_link_hosts == {"gumroad.com", "buy.example.com"} and str(guard.config.max_discount_pct) == "15"


# ------------------------------------------------------------------ the loop
class Stop:
    def __init__(self, polls):
        self.polls, self.waits = polls, []

    def is_set(self):
        self.polls -= 1
        return self.polls < 0

    def wait(self, t):
        self.waits.append(t)


class FakeWorker:
    def __init__(self, claims):
        self.claims = list(claims)

    def run_once(self):
        class R:
            pass
        r = R()
        r.claimed = self.claims.pop(0) if self.claims else 0
        return r


class FakeCloser:
    def __init__(self, boom=False):
        self.calls, self.boom = 0, boom

    def sweep(self):
        self.calls += 1
        if self.boom:
            raise RuntimeError("jane.doe@example.test must not reach the log")
        return []


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += 10.0
        return self.t


def test_WM5_the_loop_waits_only_when_idle_and_sweeps_on_its_interval():
    stop, closer = Stop(6), FakeCloser()
    serve(type("P", (), {"worker": FakeWorker([3, 0, 2, 0, 0, 0]), "closer": closer})(), stop, poll_interval=0.5, sweep_seconds=25, clock=Clock())
    assert stop.waits == [0.5, 0.5, 0.5, 0.5] and closer.calls >= 1


def test_WM6_a_sweep_of_zero_never_sweeps():
    closer = FakeCloser()
    serve(type("P", (), {"worker": FakeWorker([]), "closer": closer})(), Stop(5), poll_interval=0.1, sweep_seconds=0, clock=Clock())
    assert closer.calls == 0


def test_WM7_a_failing_sweep_is_logged_by_class_only_and_does_not_stop_the_loop(caplog=None):
    stream, handler = io.StringIO(), None
    import logging
    handler = logging.StreamHandler(stream)
    worker_main.log.addHandler(handler)
    try:
        closer = FakeCloser(boom=True)
        serve(type("P", (), {"worker": FakeWorker([]), "closer": closer})(), Stop(4), poll_interval=0.1, sweep_seconds=5, clock=Clock())
    finally:
        worker_main.log.removeHandler(handler)
    assert closer.calls >= 2 and "RUNTIME_ERROR" in stream.getvalue() and "jane.doe" not in stream.getvalue()


# ------------------------------------------------------------------ checks, catalogue, configuration errors
def test_WM8_the_start_up_check_reports_each_dependency_by_name_and_a_raised_error_is_just_a_fail():
    s = settings()
    ok = run_checks(s, Conn(), type("P", (), {"email": Health(), "adapters": {"GUMROAD": Adapter(), "PAYHIP": Adapter(ok=False)}})())
    assert ok == [("database", True), ("email_provider", True), ("marketplace_gumroad", True), ("marketplace_payhip", False)]
    bad = run_checks(s, Conn(answer=0), type("P", (), {"email": Health(boom=True), "adapters": {}})())
    assert bad == [("database", False), ("email_provider", False)]


def test_WM9_the_payhip_catalogue_is_read_and_a_bad_one_is_a_configuration_error_naming_only_the_setting(tmp_path):
    good = tmp_path / "payhip.json"
    good.write_text(json.dumps([{"key": "RGsF", "name": "Planner", "price_minor": 1900}, {"key": "x_1", "name": "Pack", "price_minor": 500, "currency": "EUR"}]))
    products = load_payhip_catalog(str(good))
    assert [(p.key, p.price_minor, p.currency) for p in products] == [("RGsF", 1900, "USD"), ("x_1", 500, "EUR")]
    bad = tmp_path / "bad.json"
    for content in ("not json", '{"key": "x"}', '[{"key": "bad key!", "name": "n", "price_minor": 1}]', '[{"name": "n"}]'):
        bad.write_text(content)
        with pytest.raises(ConfigError) as e:
            load_payhip_catalog(str(bad))
        assert e.value.problems == ("PAYHIP_CATALOG_FILE: must be a readable JSON list of {key, name, price_minor}",) and "bad key" not in str(e.value)
    with pytest.raises(ConfigError):
        load_payhip_catalog(str(tmp_path / "missing.json"))


def test_WM10_a_bad_configuration_stops_the_process_with_names_only_and_never_opens_a_connection():
    err, out = io.StringIO(), io.StringIO()
    env = {**GOOD, "DATABASE_URL": "mysql://u:leaked-pw@h/db", "POSTMARK_SERVER_TOKEN": ""}
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
        code = main(["run"], environ=env, env_file=None)
    assert code == 2 and "CONFIG ERROR DATABASE_URL" in err.getvalue() and "CONFIG ERROR POSTMARK_SERVER_TOKEN" in err.getvalue()
    assert "leaked-pw" not in err.getvalue() + out.getvalue()


LLM = {"LLM_PROVIDER": "openai", "LLM_API_KEY": "sk-test-123456", "LLM_MODEL": "model-x", "LLM_INPUT_PRICE_MICRO_PER_MTOK": "2500000",
       "LLM_OUTPUT_PRICE_MICRO_PER_MTOK": "10000000", "LLM_MAX_RETRIES": "1"}


def test_WM11_without_a_language_model_the_copywriter_has_no_model_and_the_template_is_used():
    cw = parts_for(settings()).copywriter
    assert cw.llm.__class__.__name__ == "NotConfiguredLlmClient" and not any(cw.policy._models.values())


def test_WM12_with_a_language_model_only_the_copywriter_role_uses_it_at_the_configured_price():
    parts = parts_for(settings(**LLM))
    cw = parts.copywriter
    assert isinstance(cw.llm, OpenAiLlmClient) and cw.llm._max_retries == 1
    assert cw.policy._models == {**NO_MODEL, "COPYWRITER": "model-x"}
    assert cw.tracker.pricebook.estimate_micro("model-x", 1_000_000, 1_000_000) == 12_500_000                     # exactly the configured prices
    assert "sk-test-123456" not in repr(cw.llm) + repr(parts)


def test_WM13_the_provider_setting_selects_the_client_and_a_test_client_can_be_injected():
    assert isinstance(parts_for(settings(**{**LLM, "LLM_PROVIDER": "anthropic"})).copywriter.llm, AnthropicLlmClient)
    marker = object()
    parts = build(settings(**LLM), connect=lambda: None, bus_conn=Conn(), product_adapters={"GUMROAD": Adapter()}, email_transport=FakeEmailTransport(), llm_client=marker)
    assert parts.copywriter.llm is marker


def test_WM14_the_cost_ledger_uses_its_own_connection_so_a_rolled_back_event_cannot_erase_money_already_spent():
    bus, cost = Conn(), Conn()
    parts = build(settings(**LLM), connect=lambda: None, bus_conn=bus, cost_conn=cost, product_adapters={"GUMROAD": Adapter()}, email_transport=FakeEmailTransport())
    assert parts.copywriter.tracker.conn is cost and parts.worker.store.conn is bus


def test_WM15_a_cost_alert_is_written_to_the_log_with_numbers_only():
    import logging
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    worker_main.log.addHandler(handler)
    try:
        LogNotifier().notify(type("A", (), {"threshold_pct": 80, "spent_micro_usd": 240_000_000, "ceiling_micro_usd": 300_000_000})())
    finally:
        worker_main.log.removeHandler(handler)
    assert stream.getvalue().strip() == "COST_ALERT threshold_pct=80 spent_micro_usd=240000000 ceiling_micro_usd=300000000"

"""The real LLM client with the Copywriter and the cost ledger on Postgres: provider-reported tokens become ledger rows at the configured price,
the monthly ceiling stops a call before it is made, the alert fires once, and a rolled-back event cannot erase money already spent.
The provider is a scripted opener: no network. Needs TEST_ADMIN_DSN. Names LC*."""
import json
import logging
import uuid

import psycopg

from orchestrator.copy_schema import DraftBrief, ProductFacts
from orchestrator.copywriter import Copywriter
from orchestrator.llm_providers import OpenAiLlmClient
from orchestrator.tests.test_router_pg import seed_lead
from orchestrator.worker_main import LogNotifier
from shared.cost import CostTracker, ModelPrice, PriceBook, RoutingPolicy

PRICES = PriceBook({"m-1": ModelPrice(2_500_000, 10_000_000)})                  # test fixture, not a real price
ROUTES = {"SCOUT": None, "QUALIFIER": None, "COPYWRITER": "m-1", "CLOSER": None, "COMPLIANCE_GUARD": None, "ANALYST": None}
TEXT = json.dumps({"variants": [{"subject": "Hello", "body": "Hi there https://shop.example.test/l/gr-1"}], "confidence": 0.8})
ANSWER = {"choices": [{"message": {"content": TEXT}}], "usage": {"prompt_tokens": 412, "completion_tokens": 87}}
EXPECTED_MICRO = -(-(412 * 2_500_000 + 87 * 10_000_000) // 1_000_000)           # 1,900


class Resp:
    def __init__(self, body):
        self.raw = json.dumps(body).encode()

    def read(self):
        return self.raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Opener:
    def __init__(self, *script):
        self.script, self.calls = list(script), 0

    def __call__(self, req, timeout):
        self.calls += 1
        return self.script.pop(0)


def setup(db, opener, notifier=None):
    lead = seed_lead(db, "gr-1")
    brief = DraftBrief(lead_id=lead, marketplace="EMAIL", channel="EMAIL", context="consented_outreach", signal_kind="SALE", contact_permission_id=uuid.uuid4(),
                       product=ProductFacts("gr-1", "Budget Planner", 1900, "USD", "https://shop.example.test/l/gr-1"))
    tracker = CostTracker(db.conn, PRICES, notifier or LogNotifier())
    return lead, brief, tracker, Copywriter(RoutingPolicy(ROUTES, PRICES), tracker, OpenAiLlmClient("sk-test-123456", opener=opener, sleep=lambda s: None))


def ledger(db):
    return db.conn.execute("SELECT category::text, role::text, model, input_tokens, output_tokens, cost_micro_usd, lead_id FROM core.cost_ledger").fetchall()


def test_LC1_a_draft_is_written_by_the_model_and_the_reported_tokens_are_recorded_at_the_configured_price(db):
    opener = Opener(Resp(ANSWER))
    lead, brief, _, writer = setup(db, opener)
    result = writer.draft(brief)
    assert (result.source, result.model, opener.calls) == ("LLM", "m-1", 1)
    assert ledger(db) == [("LLM", "COPYWRITER", "m-1", 412, 87, EXPECTED_MICRO, lead)] and EXPECTED_MICRO == 1900


def test_LC2_a_call_that_would_pass_the_ceiling_is_never_made_and_the_draft_is_the_template(db):
    db.conn.execute("UPDATE core.cost_config SET monthly_ceiling_micro_usd = 100")                 # an administrator's change, not the application's
    opener = Opener(Resp(ANSWER))
    _, brief, _, writer = setup(db, opener)
    result = writer.draft(brief)
    assert (result.source, result.fallback_reason, opener.calls, ledger(db)) == ("TEMPLATE", "BUDGET_EXHAUSTED", 0, [])


def test_LC3_crossing_the_alert_threshold_logs_one_alert_per_month_and_does_not_repeat(db):
    db.conn.execute("UPDATE core.cost_config SET monthly_ceiling_micro_usd = 2000, alert_pct = 80")        # one call (1,900) is 95% of 2,000
    stream = []
    handler = logging.Handler()
    handler.emit = lambda record: stream.append(record.getMessage())
    logging.getLogger("worker").addHandler(handler)
    try:
        _, brief, tracker, writer = setup(db, Opener(Resp(ANSWER)))
        writer.draft(brief)
        assert [m.split()[0] for m in stream] == ["COST_ALERT"] and "threshold_pct=80" in stream[0]
        assert db.conn.execute("SELECT pct FROM core.cost_alert").fetchall() == [(80,)]
        tracker.check_alerts()                                                                           # nothing new crossed: nothing fires again
        assert len(stream) == 1
    finally:
        logging.getLogger("worker").removeHandler(handler)


def test_LC4_a_failure_after_the_provider_was_paid_does_not_erase_the_ledger_row_when_the_ledger_has_its_own_connection(db):
    lead = seed_lead(db, "gr-1")
    bus = psycopg.connect(db.dsn, autocommit=True)                                                       # the event's connection
    try:
        tracker = CostTracker(db.conn, PRICES, LogNotifier())                                            # the ledger's own connection
        try:
            with bus.transaction():
                tracker.record_llm_call(uuid.uuid4(), "COPYWRITER", "m-1", 412, 87, lead_id=lead, trace_id=lead)
                raise RuntimeError("the handler failed after the call was paid")
        except RuntimeError:
            pass
        assert len(ledger(db)) == 1                                                                      # the money is still on the books
        shared = CostTracker(bus, PRICES, LogNotifier())                                                 # the same connection: the row WOULD have been lost
        try:
            with bus.transaction():
                shared.record_llm_call(uuid.uuid4(), "COPYWRITER", "m-1", 1, 1, lead_id=lead, trace_id=lead)
                raise RuntimeError("again")
        except RuntimeError:
            pass
        assert len(ledger(db)) == 1
    finally:
        bus.close()

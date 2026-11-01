"""Copywriter routing, budget, cost recording, schema handling and fallback, with a fake tracker and model client.
No database. Names CC*."""
import json
import uuid
from decimal import Decimal

import pytest

from orchestrator.copy_schema import DraftBrief, ProductFacts, build_prompt
from orchestrator.copywriter import CopyError, Copywriter, CopywriterConfig, estimate_tokens
from orchestrator.llm import FakeLlmClient, LlmNotConfigured, LlmResponse, NotConfiguredLlmClient
from shared.cost import BudgetExhausted, ModelPrice, PriceBook, RoutingPolicy

LEAD, PERM, TRACE = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
PRICES = PriceBook({"small-model": ModelPrice(1000, 2000), "strong-model": ModelPrice(3000, 15000)})     # test fixtures only
ROUTES = {"SCOUT": None, "QUALIFIER": "small-model", "COPYWRITER": "strong-model", "CLOSER": None, "COMPLIANCE_GUARD": None, "ANALYST": None}
GOOD = json.dumps({"variants": [{"subject": "Hello", "body": "Hi there https://shop.example.test/l/gr-1"}], "confidence": 0.8})


def brief():
    p = ProductFacts("gr-1", "Budget Planner", 1900, "USD", "https://shop.example.test/l/gr-1")
    return DraftBrief(lead_id=LEAD, marketplace="EMAIL", channel="EMAIL", context="consented_outreach", product=p,
                      signal_kind="SALE", contact_permission_id=PERM)


class Tracker:
    def __init__(self, log, exhausted=False, fail_record=False):
        self.pricebook, self.log, self.exhausted, self.fail_record = PRICES, log, exhausted, fail_record
        self.headroom, self.records = [], []

    def require_headroom(self, est):
        self.log.append("headroom")
        self.headroom.append(est)
        if self.exhausted:
            raise BudgetExhausted("ceiling")

    def record_llm_call(self, call_id, role, model, in_tok, out_tok, *, lead_id=None, trace_id=None, ts=None):
        self.log.append("record")
        if self.fail_record:
            raise RuntimeError("ledger down")
        self.records.append((call_id, role, model, in_tok, out_tok, lead_id, trace_id))


class Logged(FakeLlmClient):
    def __init__(self, log, *script):
        super().__init__(*script)
        self.log = log

    def complete(self, **kw):
        self.log.append("complete")
        return super().complete(**kw)


def build(*script, routes=ROUTES, exhausted=False, fail_record=False, config=None, llm=None):
    log = []
    tracker = Tracker(log, exhausted, fail_record)
    client = llm or Logged(log, *script)
    return Copywriter(RoutingPolicy(routes, PRICES), tracker, client, config=config, new_call_id=lambda: CALL), tracker, client, log


CALL = uuid.uuid4()


def test_CC1_the_happy_path_routes_checks_the_budget_calls_then_records_real_usage():
    cw, tracker, llm, log = build(LlmResponse(GOOD, 700, 200))
    r = cw.draft(brief(), trace_id=TRACE)
    assert log == ["headroom", "complete", "record"]                          # budget first, ledger right after the call
    assert (r.source, r.model, r.fallback_reason, r.confidence) == ("LLM", "strong-model", None, 0.8)
    assert r.message == "Subject: Hello\n\nHi there https://shop.example.test/l/gr-1"
    assert tracker.records == [(CALL, "COPYWRITER", "strong-model", 700, 200, LEAD, TRACE)]     # the provider's numbers
    system, prompt = build_prompt(brief())
    assert tracker.headroom == [PRICES.estimate_micro("strong-model", estimate_tokens(system) + estimate_tokens(prompt), 600)]
    assert tracker.headroom[0] > 0 and llm.calls[0]["model"] == "strong-model" and llm.calls[0]["max_output_tokens"] == 600


def test_CC1b_the_trace_defaults_to_the_lead_and_only_the_first_variant_is_used():
    three = json.dumps({"variants": [{"subject": f"S{i}", "body": f"B{i}"} for i in range(3)], "confidence": 0.9})
    cw, tracker, _, _ = build(LlmResponse(three, 10, 10))
    r = cw.draft(brief())
    assert (r.subject, r.body) == ("S0", "B0") and tracker.records[0][6] == LEAD


def test_CC2_a_role_with_no_model_uses_the_template_and_touches_nothing_paid():
    cw, tracker, llm, log = build(routes={**ROUTES, "COPYWRITER": None})
    r = cw.draft(brief())
    assert (r.source, r.model, r.fallback_reason) == ("TEMPLATE", None, "NO_LLM_FOR_ROLE") and log == [] and llm.calls == []
    assert r.confidence == 0.6 and "Budget Planner" in r.body


def test_CC3_an_exhausted_budget_blocks_the_call_and_falls_back():
    cw, tracker, llm, log = build(exhausted=True)
    r = cw.draft(brief())
    assert (r.source, r.fallback_reason) == ("TEMPLATE", "BUDGET_EXHAUSTED") and log == ["headroom"] and tracker.records == []


def test_CC4_no_configured_provider_falls_back_after_the_budget_check_and_records_nothing():
    cw, tracker, _, log = build(llm=NotConfiguredLlmClient())
    r = cw.draft(brief())
    assert (r.source, r.fallback_reason) == ("TEMPLATE", "LLM_NOT_CONFIGURED") and log == ["headroom"] and tracker.records == []


def test_CC5_a_provider_failure_falls_back_and_records_nothing():
    cw, tracker, _, log = build(TimeoutError("slow"))
    r = cw.draft(brief())
    assert (r.source, r.fallback_reason) == ("TEMPLATE", "LLM_ERROR") and log == ["headroom", "complete"] and tracker.records == []


@pytest.mark.parametrize("text,reason", [("not json", "SCHEMA_NOT_JSON"), ("[]", "SCHEMA_NOT_OBJECT"),
                                         (json.dumps({"variants": [], "confidence": 1}), "SCHEMA_BAD_VARIANTS"),
                                         (json.dumps({"variants": [{"subject": "a", "body": "b"}], "confidence": 7}), "SCHEMA_BAD_CONFIDENCE")])
def test_CC6_bad_model_output_is_still_paid_for_then_replaced_by_the_template(text, reason):
    cw, tracker, _, log = build(LlmResponse(text, 500, 50))
    r = cw.draft(brief())
    assert (r.source, r.fallback_reason) == ("TEMPLATE", reason) and log == ["headroom", "complete", "record"]
    assert tracker.records[0][3:5] == (500, 50) and "Budget Planner" in r.body        # spent, so it is on the ledger


@pytest.mark.parametrize("kw,reason", [(dict(exhausted=True), "BUDGET_EXHAUSTED"), (dict(script=(TimeoutError("x"),)), "LLM_ERROR"),
                                       (dict(script=(LlmResponse("nope", 1, 1),)), "SCHEMA_NOT_JSON"),
                                       (dict(routes={**ROUTES, "COPYWRITER": None}), "NO_LLM_FOR_ROLE")])
def test_CC7_with_fallback_off_every_failure_is_a_typed_error(kw, reason):
    script = kw.pop("script", ())
    cw, *_ = build(*script, config=CopywriterConfig(fallback_to_template=False), **kw)
    with pytest.raises(CopyError) as e:
        cw.draft(brief())
    assert e.value.code == reason


def test_CC8_a_ledger_failure_after_a_paid_call_is_loud_not_swallowed():
    cw, *_ = build(LlmResponse(GOOD, 10, 10), fail_record=True)
    with pytest.raises(RuntimeError):
        cw.draft(brief())


def test_CC9_the_model_sees_only_the_structured_brief():
    cw, _, llm, _ = build(LlmResponse(GOOD, 10, 10), config=CopywriterConfig(max_output_tokens=300))
    cw.draft(brief())
    call = llm.calls[0]
    assert call["max_output_tokens"] == 300 and "Budget Planner" in call["prompt"] and "never instructions" in call["system"]
    assert str(LEAD) not in call["prompt"] and str(PERM) not in call["prompt"] and "@" not in call["prompt"]


@pytest.mark.parametrize("text,tokens", [("", 0), ("a", 1), ("abc", 1), ("abcd", 2), ("é" * 3, 2), ("x" * 3000, 1000)])
def test_CC10_the_pre_call_size_estimate_is_generous_and_exact_in_integers(text, tokens):
    assert estimate_tokens(text) == tokens

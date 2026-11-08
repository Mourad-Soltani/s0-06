"""The real language-model clients against a scripted opener: what they send, how they read answers, how they retry, and that the Copywriter and the
ledger get the provider-reported tokens. No network. Names LP*."""
import json
import urllib.error
import uuid

import pytest

from adapters.errors import AuthExpired, MalformedResponse, RateLimited, Rejected, TransportError
from orchestrator.copy_schema import DraftBrief, ProductFacts
from orchestrator.copywriter import Copywriter
from orchestrator.llm import LlmResponse
from orchestrator.llm_providers import MAX_WAIT_SECONDS, AnthropicLlmClient, OpenAiLlmClient, make_llm_client
from shared.cost import ModelPrice, PriceBook, RoutingPolicy

KEY = "sk-test-0123456789abcdef"
SYSTEM, PROMPT = "You write short emails.", 'Brief: {"product": {"title": "Budget Planner"}}'
GOOD_TEXT = json.dumps({"variants": [{"subject": "Hello", "body": "Hi there https://shop.example.test/l/gr-1"}], "confidence": 0.8})
OPENAI_OK = {"choices": [{"message": {"role": "assistant", "content": GOOD_TEXT}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 412, "completion_tokens": 87}}
ANTHROPIC_OK = {"content": [{"type": "text", "text": GOOD_TEXT}], "usage": {"input_tokens": 400, "output_tokens": 90}, "stop_reason": "end_turn"}


class Resp:
    def __init__(self, body):
        self.raw = body if isinstance(body, bytes) else json.dumps(body).encode()

    def read(self):
        return self.raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Body:
    def read(self, *a):
        raise AssertionError("an HTTP error body must never be read")

    def close(self):
        pass


def http_error(code, headers=None):
    return urllib.error.HTTPError("https://api.example.test", code, "x", headers or {}, Body())


class Opener:
    def __init__(self, *script):
        self.script, self.requests = list(script), []

    def __call__(self, req, timeout):
        self.requests.append((req, timeout))
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def client(cls, *script, **kw):
    op, sleeps = Opener(*script), []
    return cls(KEY, opener=op, sleep=sleeps.append, **kw), op, sleeps


def call(c, **kw):
    return c.complete(**{"model": "m-1", "system": SYSTEM, "prompt": PROMPT, "max_output_tokens": 600, **kw})


# ------------------------------------------------------------------ what is sent, what is read
def test_LP1_openai_sends_one_system_and_one_user_message_and_returns_the_reported_tokens():
    c, op, _ = client(OpenAiLlmClient, Resp(OPENAI_OK))
    assert call(c) == LlmResponse(text=GOOD_TEXT, input_tokens=412, output_tokens=87)
    (req, timeout), = op.requests
    assert (req.full_url, req.get_method(), timeout) == ("https://api.openai.com/v1/chat/completions", "POST", 30.0)
    assert req.get_header("Authorization") == f"Bearer {KEY}" and req.get_header("Content-type") == "application/json"
    assert json.loads(req.data) == {"model": "m-1", "max_completion_tokens": 600,
                                    "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": PROMPT}]}
    assert KEY not in req.data.decode()                                        # no user id, no store flag, no key in the body


def test_LP2_anthropic_sends_the_system_prompt_separately_and_joins_the_text_blocks():
    two_blocks = {**ANTHROPIC_OK, "content": [{"type": "thinking", "thinking": "x"}, {"type": "text", "text": '{"a":'}, {"type": "text", "text": ' 1}'}]}
    c, op, _ = client(AnthropicLlmClient, Resp(ANTHROPIC_OK), Resp(two_blocks))
    assert call(c) == LlmResponse(text=GOOD_TEXT, input_tokens=400, output_tokens=90)
    req = op.requests[0][0]
    assert req.full_url == "https://api.anthropic.com/v1/messages" and req.get_header("X-api-key") == KEY and req.get_header("Anthropic-version") == "2023-06-01"
    assert json.loads(req.data) == {"model": "m-1", "max_tokens": 600, "system": SYSTEM, "messages": [{"role": "user", "content": PROMPT}]}
    assert call(c).text == '{"a": 1}'


# ------------------------------------------------------------------ rate limits and transient failures
def test_LP3_a_rate_limit_is_waited_out_honouring_retry_after_and_the_call_then_succeeds():
    c, op, sleeps = client(OpenAiLlmClient, http_error(429, {"Retry-After": "3"}), http_error(429, {"Retry-After": "0"}), Resp(OPENAI_OK))
    assert call(c).output_tokens == 87 and sleeps == [3.0, 0.0] and len(op.requests) == 3


def test_LP3b_without_retry_after_the_wait_doubles_and_server_errors_are_retried_too():
    c, op, sleeps = client(OpenAiLlmClient, http_error(429), http_error(503), Resp(OPENAI_OK))
    assert call(c).input_tokens == 412 and sleeps == [1.0, 2.0]
    c2, _, sleeps2 = client(AnthropicLlmClient, http_error(529), urllib.error.URLError("dns"), Resp(ANTHROPIC_OK))
    assert call(c2).input_tokens == 400 and sleeps2 == [1.0, 2.0]


def test_LP4_when_the_retries_run_out_the_failure_is_raised_by_kind_and_the_attempt_count_is_bounded():
    c, op, sleeps = client(OpenAiLlmClient, http_error(429, {"Retry-After": "2"}), http_error(429, {"Retry-After": "2"}), http_error(429, {"Retry-After": "9"}))
    with pytest.raises(RateLimited) as e:
        call(c)
    assert e.value.retry_after_seconds == 9.0 and len(op.requests) == 3 and sleeps == [2.0, 2.0]
    c2, op2, _ = client(OpenAiLlmClient, http_error(500), http_error(502), http_error(504))
    with pytest.raises(TransportError):
        call(c2)
    assert len(op2.requests) == 3
    c3, op3, sleeps3 = client(OpenAiLlmClient, http_error(429), max_retries=0)
    with pytest.raises(RateLimited):
        call(c3)
    assert len(op3.requests) == 1 and sleeps3 == []


def test_LP5_a_wait_longer_than_the_cap_is_not_held_the_call_fails_at_once():
    c, op, sleeps = client(OpenAiLlmClient, http_error(429, {"Retry-After": str(MAX_WAIT_SECONDS + 1)}))
    with pytest.raises(RateLimited):
        call(c)
    assert len(op.requests) == 1 and sleeps == []


@pytest.mark.parametrize("code,expected", [(400, Rejected), (404, Rejected), (413, Rejected), (422, Rejected), (401, AuthExpired), (403, AuthExpired)])
def test_LP6_a_request_that_is_wrong_is_never_retried_and_the_error_body_is_never_read(code, expected):
    c, op, sleeps = client(OpenAiLlmClient, http_error(code))
    with pytest.raises(expected) as e:
        call(c)
    assert len(op.requests) == 1 and sleeps == [] and KEY not in str(e.value)


def test_LP7_network_trouble_is_retried_then_reported_as_a_transport_error():
    c, op, sleeps = client(AnthropicLlmClient, TimeoutError(), OSError(), urllib.error.URLError("x"))
    with pytest.raises(TransportError):
        call(c)
    assert len(op.requests) == 3 and sleeps == [1.0, 2.0]


# ------------------------------------------------------------------ answers that cannot be trusted
@pytest.mark.parametrize("body", [
    b"<html>", b"[]", b"\xff\xfe", {}, {"choices": []}, {"choices": [{"message": {"content": GOOD_TEXT}}]},                         # no usage: spend cannot be recorded
    {"choices": [{"message": {"content": None, "refusal": "no"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
    {"choices": [{"message": {"content": "   "}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
    {"choices": [{"message": {"content": "x"}}], "usage": {"prompt_tokens": True, "completion_tokens": 1}},
    {"choices": [{"message": {"content": "x"}}], "usage": {"prompt_tokens": -1, "completion_tokens": 1}},
    {"choices": [{"message": {"content": "x"}}], "usage": {"prompt_tokens": "5", "completion_tokens": 1}},
    {"choices": "nope", "usage": {"prompt_tokens": 1, "completion_tokens": 1}}])
def test_LP8_an_openai_answer_without_text_or_usage_is_refused_not_guessed(body):
    c, _, _ = client(OpenAiLlmClient, Resp(body))
    with pytest.raises(MalformedResponse):
        call(c)


@pytest.mark.parametrize("body", [
    {}, {"content": [], "usage": {"input_tokens": 1, "output_tokens": 1}}, {"content": [{"type": "text", "text": ""}], "usage": {"input_tokens": 1, "output_tokens": 1}},
    {"content": [{"type": "text", "text": "x"}]}, {"content": [{"type": "text", "text": "x"}], "usage": {"input_tokens": 1.5, "output_tokens": 1}},
    {"content": "text", "usage": {"input_tokens": 1, "output_tokens": 1}}])
def test_LP8b_an_anthropic_answer_without_text_or_usage_is_refused_not_guessed(body):
    c, _, _ = client(AnthropicLlmClient, Resp(body))
    with pytest.raises(MalformedResponse):
        call(c)


# ------------------------------------------------------------------ construction and arguments
def test_LP9_bad_construction_and_bad_arguments_fail_before_any_request_and_the_key_never_prints():
    for cls in (OpenAiLlmClient, AnthropicLlmClient):
        for key in ("", "  ", None):
            with pytest.raises(ValueError):
                cls(key)
        for retries in (-1, 6, True, 1.5):
            with pytest.raises(ValueError):
                cls(KEY, max_retries=retries)
        c, op, _ = client(cls)
        assert KEY not in repr(c) and repr(c) == f"{cls.__name__}(max_retries=2)"
        for bad in ({"model": ""}, {"prompt": " "}, {"max_output_tokens": 0}, {"max_output_tokens": True}, {"max_output_tokens": 32001}, {"system": None}):
            with pytest.raises(ValueError):
                call(c, **bad)
        assert op.requests == []


def test_LP10_the_factory_maps_the_provider_name_and_refuses_an_unknown_one():
    assert isinstance(make_llm_client("openai", KEY), OpenAiLlmClient) and isinstance(make_llm_client("anthropic", KEY, max_retries=0), AnthropicLlmClient)
    with pytest.raises(ValueError):
        make_llm_client("acme", KEY)


# ------------------------------------------------------------------ with the Copywriter and the ledger
PRICES = PriceBook({"m-1": ModelPrice(2_500_000, 10_000_000)})                  # test fixture, not a real price
ROUTES = {"SCOUT": None, "QUALIFIER": None, "COPYWRITER": "m-1", "CLOSER": None, "COMPLIANCE_GUARD": None, "ANALYST": None}


class Tracker:
    pricebook = PRICES

    def __init__(self):
        self.records = []

    def require_headroom(self, estimate):
        pass

    def record_llm_call(self, call_id, role, model, in_tok, out_tok, *, lead_id=None, trace_id=None, ts=None):
        self.records.append((role, model, in_tok, out_tok, PRICES.estimate_micro(model, in_tok, out_tok)))


def brief():
    p = ProductFacts("gr-1", "Budget Planner", 1900, "USD", "https://shop.example.test/l/gr-1")
    return DraftBrief(lead_id=uuid.uuid4(), marketplace="EMAIL", channel="EMAIL", context="consented_outreach", product=p, signal_kind="SALE",
                      contact_permission_id=uuid.uuid4())


def test_LP11_a_real_answer_becomes_an_llm_draft_and_the_ledger_gets_the_reported_tokens_at_the_configured_price():
    c, _, _ = client(OpenAiLlmClient, Resp(OPENAI_OK))
    tracker = Tracker()
    result = Copywriter(RoutingPolicy(ROUTES, PRICES), tracker, c).draft(brief())
    assert (result.source, result.model, result.subject) == ("LLM", "m-1", "Hello")
    assert tracker.records == [("COPYWRITER", "m-1", 412, 87, -(-(412 * 2_500_000 + 87 * 10_000_000) // 1_000_000))]          # 1,900 micro-USD: exact, rounded up


def test_LP12_a_provider_that_keeps_refusing_costs_nothing_and_the_draft_falls_back_to_the_template():
    c, op, sleeps = client(OpenAiLlmClient, http_error(429), http_error(429), http_error(429))
    tracker = Tracker()
    result = Copywriter(RoutingPolicy(ROUTES, PRICES), tracker, c).draft(brief())
    assert result.source == "TEMPLATE" and result.fallback_reason == "LLM_ERROR" and tracker.records == [] and len(op.requests) == 3


def test_LP13_an_answer_that_is_not_the_required_json_is_still_paid_for_and_recorded_then_falls_back():
    junk = {"choices": [{"message": {"content": "Sure! Here is an email."}}], "usage": {"prompt_tokens": 300, "completion_tokens": 20}}
    c, _, _ = client(OpenAiLlmClient, Resp(junk))
    tracker = Tracker()
    result = Copywriter(RoutingPolicy(ROUTES, PRICES), tracker, c).draft(brief())
    assert result.source == "TEMPLATE" and result.fallback_reason.startswith("SCHEMA_") and [r[2:4] for r in tracker.records] == [(300, 20)]

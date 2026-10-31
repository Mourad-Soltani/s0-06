"""Compliance Guard rules: pure Python, no database. Names CG*."""
from decimal import Decimal

import pytest

from orchestrator.guard_rules import (BLOCK, ESCALATE, PASS, DraftFacts, Finding, GuardConfig, merge, run_rules, verdict_of)

CFG = GuardConfig(allowed_link_hosts=frozenset({"shop.example.test"}), max_discount_pct=Decimal("20"))
GOOD = "Subject: Your guide is ready\n\nHi there,\n\nHere is your guide: https://shop.example.test/l/gr-1\n\nThanks!"


def facts(body=GOOD, **kw):
    base = dict(context="consented_outreach", has_current_permission=True)
    base.update(kw)
    return DraftFacts(body=body, **base)


def codes(f, cfg=CFG):
    return [x.code for x in run_rules(f, cfg)]


def sev(f, code, cfg=CFG):
    return {x.code: x.severity for x in run_rules(f, cfg)}[code]


def msg(text, subject="Hello"):
    return f"Subject: {subject}\n\n{text}"


def test_CG1_a_clean_draft_has_no_findings_and_passes():
    assert run_rules(facts(), CFG) == () and verdict_of(run_rules(facts(), CFG)) == PASS


@pytest.mark.parametrize("body,expected", [("", "MISSING_BODY"), ("   \n ", "MISSING_BODY"), (None, "MISSING_BODY"), (5, "MISSING_BODY"),
                                           ("Hello, no subject line", "INVALID_MESSAGE_FORMAT"),
                                           ("Subject: x\nno blank line", "INVALID_MESSAGE_FORMAT")])
def test_CG2_a_missing_or_malformed_message_is_blocked(body, expected):
    assert expected in codes(facts(body)) and sev(facts(body), expected) == BLOCK


def test_CG3_outreach_needs_a_current_permission_but_a_buyers_own_message_does_not():
    assert codes(facts(has_current_permission=False)) == ["NO_CURRENT_PERMISSION"]
    assert codes(facts(has_current_permission=False, context="buyer_initiated")) == []
    assert codes(facts(has_current_permission=False, context="order_related")) == []


@pytest.mark.parametrize("discount,body,expected", [
    (None, GOOD, []), (Decimal("10"), GOOD, []), (Decimal("20"), GOOD, []),                     # the cap itself is allowed
    (Decimal("20.01"), GOOD, ["DISCOUNT_ABOVE_CAP"]), (Decimal("50"), GOOD, ["DISCOUNT_ABOVE_CAP"]),
    (Decimal("0"), GOOD, ["INVALID_DISCOUNT"]), (Decimal("-5"), GOOD, ["INVALID_DISCOUNT"]),
    (Decimal("101"), GOOD, ["INVALID_DISCOUNT"]), (20.0, GOOD, ["INVALID_DISCOUNT"]),
    (Decimal("20"), msg("Get 20% off: https://shop.example.test/l/1"), []),
    (Decimal("10"), msg("Get 20% off: https://shop.example.test/l/1"), ["DISCOUNT_MISMATCH"]),
    (None, msg("Get 20% off: https://shop.example.test/l/1"), ["DISCOUNT_MISMATCH"]),
    (Decimal("25"), msg("Get 25 % off: https://shop.example.test/l/1"), ["DISCOUNT_ABOVE_CAP"])])
def test_CG4_discounts_obey_the_cap_and_the_text_promises_exactly_the_offer(discount, body, expected):
    assert sorted(codes(facts(body, discount_pct=discount))) == sorted(expected)


def test_CG4b_without_a_configured_cap_any_discount_goes_to_a_human():
    cfg = GuardConfig(allowed_link_hosts=CFG.allowed_link_hosts)
    f = facts(discount_pct=Decimal("5"))
    assert codes(f, cfg) == ["DISCOUNT_CAP_NOT_CONFIGURED"] and sev(f, "DISCOUNT_CAP_NOT_CONFIGURED", cfg) == ESCALATE
    assert codes(facts(), cfg) == []


@pytest.mark.parametrize("text", ["Hi {{first_name}}", "Hi [[name]]", "Hi %FIRST_NAME%", "Hi {first_name}", "Hi <FIRST_NAME>"])
def test_CG5_unresolved_placeholders_are_blocked(text):
    assert codes(facts(msg(text))) == ["UNRESOLVED_PLACEHOLDER"]


def test_CG5b_ordinary_braces_and_percent_signs_are_fine():
    assert codes(facts(msg("Use the {} operator, 50% done, a < b > c"))) == []


@pytest.mark.parametrize("subject,context,flagged", [
    ("Re: your order", "consented_outreach", True), ("RE : your order", "consented_outreach", True),
    ("Fwd: details", "consented_outreach", True), ("FW: details", "consented_outreach", True),
    ("Re: your order", "buyer_initiated", False), ("Reminder: your guide", "consented_outreach", False),
    ("Regarding your guide", "consented_outreach", False)])
def test_CG6_fake_reply_and_forward_subjects_are_deceptive_unless_it_is_a_real_reply(subject, context, flagged):
    f = facts(msg("Hello", subject), context=context, has_current_permission=True)
    assert ("DECEPTIVE_SUBJECT" in codes(f)) is flagged


@pytest.mark.parametrize("url,expected", [
    ("https://shop.example.test/l/1", []), ("https://checkout.shop.example.test/l/1", []),
    ("https://shop.example.test/l/1.", []), ("http://shop.example.test/l/1", ["LINK_NOT_HTTPS"]),
    ("https://evil.test/x", ["LINK_HOST_NOT_ALLOWED"]),
    ("https://shop.example.test.evil.com/x", ["LINK_HOST_NOT_ALLOWED"]),       # lookalike suffix
    ("https://evilshop.example.test/x", ["LINK_HOST_NOT_ALLOWED"]),            # not a real subdomain
    ("https://bit.ly/abc", ["LINK_HOST_NOT_ALLOWED", "LINK_SHORTENER"]),
    ("https://user:pw@shop.example.test/x", ["LINK_HAS_CREDENTIALS"]),
    ("https://192.168.0.1/x", ["LINK_IP_HOST"]), ("www.evil.com/x", ["LINK_HOST_NOT_ALLOWED", "LINK_NOT_HTTPS"])])
def test_CG7_links_must_be_https_to_an_allowed_host(url, expected):
    assert sorted(codes(facts(msg(f"See {url}")))) == sorted(expected)


def test_CG7b_every_link_is_checked_and_no_configured_hosts_means_no_links():
    assert codes(facts(msg("A https://shop.example.test/1 and B https://evil.test/2"))) == ["LINK_HOST_NOT_ALLOWED"]
    assert codes(facts(), GuardConfig()) == ["LINK_HOST_NOT_ALLOWED"]
    assert codes(facts(msg("No links here")), GuardConfig()) == []


def test_CG8_personal_data_in_a_subject():
    assert "PII_IN_SUBJECT" in codes(facts(msg("Hi", "For jane.doe@example.test")))
    assert sev(facts(msg("Hi", "Order 1234567 ready")), "POSSIBLE_PII_IN_SUBJECT") == ESCALATE
    assert codes(facts(msg("Hi", "Your 2026 guide"))) == []
    assert codes(facts(msg("Write to jane.doe@example.test"))) == []                 # the body may contain an address


@pytest.mark.parametrize("text", ["Guaranteed income in 30 days", "Get rich with this", "Totally risk-free", "No risk at all",
                                  "This cures anxiety", "FDA approved method", "Make $500 a day", "Make 5000 per week", "100% safe"])
def test_CG9_prohibited_claims_are_blocked(text):
    assert codes(facts(msg(text))) == ["PROHIBITED_CLAIM"]


@pytest.mark.parametrize("text", ["A secure checkout", "Accurate planning", "Helps you plan the week", "Results vary"])
def test_CG9b_benign_words_are_not_claims(text):
    assert codes(facts(msg(text))) == []


@pytest.mark.parametrize("text", ["Act now", "This is your last chance", "Only 3 left", "Expires tonight", "Hurry up", "Don't miss out"])
def test_CG10_pressure_language_goes_to_a_human(text):
    f = facts(msg(text))
    assert codes(f) == ["URGENCY_PRESSURE"] and sev(f, "URGENCY_PRESSURE") == ESCALATE


def test_CG10b_shouting_spam_phrases_and_long_subjects():
    assert codes(facts(msg("Hi", "LIMITED OFFER TODAY"))) == ["SHOUTING"] and codes(facts(msg("Hi", "Wow!!"))) == ["SHOUTING"]
    assert codes(facts(msg("Great! Great! Great! Great! Great!"))) == ["SHOUTING"] and codes(facts(msg("Hi", "OK"))) == []
    assert codes(facts(msg("Click here to buy now"))) == ["SPAM_PHRASES"] and codes(facts(msg("Click here please"))) == []
    assert codes(facts(msg("Hi", "x" * 78))) == [] and codes(facts(msg("Hi", "x" * 79))) == ["SUBJECT_TOO_LONG"]


def test_CG11_low_confidence_and_risk_flags():
    assert codes(facts(confidence=0.49)) == ["LOW_CONFIDENCE"] and codes(facts(confidence=0.5)) == [] and codes(facts(confidence=None)) == []
    assert codes(facts(confidence=0.3), GuardConfig(allowed_link_hosts=CFG.allowed_link_hosts, min_confidence=0.2)) == []
    assert codes(facts(risk_flags=("TOS_RISK",))) == ["RISK_FLAG_TOS"] and codes(facts(risk_flags=("CONSENT_RISK",))) == []


def test_CG12_the_verdict_is_the_strictest_severity_and_findings_are_merged_and_sorted():
    assert verdict_of([]) == PASS
    assert verdict_of([Finding("A", ESCALATE)]) == ESCALATE
    assert verdict_of([Finding("A", ESCALATE), Finding("B", BLOCK)]) == BLOCK
    m = merge([Finding("B", ESCALATE), Finding("A", BLOCK), Finding("B", BLOCK), Finding("A", ESCALATE)])
    assert m == (Finding("A", BLOCK), Finding("B", BLOCK))
    both = facts(msg("Act now https://evil.test/x", "Re: hi"), has_current_permission=False)
    r = run_rules(both, CFG)
    assert [x.code for x in r] == sorted(x.code for x in r) and verdict_of(r) == BLOCK
    assert run_rules(both, CFG) == r                                                  # deterministic


@pytest.mark.parametrize("body", ["\x00\x01", "é" * 5000, "Subject: \u202e\n\n\u202e", "http://", "https://", "https://[::1", "%%", "{{", "Subject:\n\n",
                                  "https://" + "a" * 5000 + ".test", "\n\n\n"])
def test_CG13_odd_input_never_crashes_the_rules(body):
    assert verdict_of(run_rules(facts(body), CFG)) in (PASS, ESCALATE, BLOCK)

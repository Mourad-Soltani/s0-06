"""What the reviewer sees: queue rows, the draft view, approval rules, texts, privacy. Pure Python. Names VM*."""
import re
from pathlib import Path

import pytest

from ui.fake_client import GOOD_BODY, make_detail
from ui.view_model import (FINDING_LABELS, REJECT_REASONS, detail_view, friendly_error, queue_rows, split_message)

ROOT = Path(__file__).resolve().parents[2]


def test_VM1_queue_rows_keep_order_and_describe_each_item_in_words():
    a, b = make_detail(age_minutes=5, verdict="BLOCK", tier=1), make_detail(verdict=None, tier=0, marketplace="ETSY")
    rows = queue_rows([{k: d[k] for k in ("proposal_id", "marketplace", "autonomy_tier", "confidence", "compliance_verdict", "created_at")} for d in (a, b)])
    assert [r.proposal_id for r in rows] == [a["proposal_id"], b["proposal_id"]]
    assert rows[0].label == "02 Nov 11:55 UTC · EMAIL · Blocked by the checks · tier 1" and rows[0].confidence_pct == 60
    assert rows[1].label.endswith("ETSY · Not checked yet · tier 0") and rows[1].verdict is None


def test_VM1b_rows_without_an_id_are_dropped_and_odd_values_do_not_crash():
    rows = queue_rows([{"marketplace": "EMAIL"}, {"proposal_id": "p1", "created_at": "garbage", "compliance_verdict": "WAT"}, {"proposal_id": 5}])
    assert len(rows) == 1 and "unknown time" in rows[0].label and "Unknown" in rows[0].label and rows[0].confidence_pct is None


def test_VM2_every_api_error_code_has_a_plain_message_and_unknown_ones_get_a_safe_default():
    for code in ("UNAUTHENTICATED", "NOT_FOUND", "NOT_PENDING", "NOT_APPROVABLE", "EDIT_UNAVAILABLE", "INVALID_REQUEST", "UNREACHABLE", "BAD_RESPONSE", "INTERNAL"):
        assert friendly_error(code) != friendly_error("SOMETHING_NEW") and friendly_error(code).endswith((".", "?"))
    assert friendly_error("SOMETHING_NEW") == friendly_error(None) == "Something went wrong. Please try again."


def test_VM2b_rejection_reasons_are_valid_unique_codes_with_labels():
    codes = [c for c, _ in REJECT_REASONS]
    assert len(codes) == len(set(codes)) and all(re.match(r"^[A-Z][A-Z0-9_]{0,63}$", c) for c in codes) and all(label for _, label in REJECT_REASONS)


@pytest.mark.parametrize("body,expected", [
    ("Subject: Hello\n\nHi there", ("Hello", "Hi there")), ("Subject:  padded  \n\nBody\n\nmore", ("padded", "Body\n\nmore")),
    ("No subject line", ("(no subject)", "No subject line")), ("Subject: a\nb\n\ntext", ("(no subject)", "Subject: a\nb\n\ntext")),
    (None, ("(no subject)", "")), (5, ("(no subject)", ""))])
def test_VM3_the_subject_is_split_from_the_message(body, expected):
    assert split_message(body) == expected


def test_VM4_the_detail_view_tells_the_reviewer_what_the_draft_is_and_why_it_is_here():
    d = make_detail(verdict="PASS", tier=0, source="LLM", findings=())
    d["payload"].update(discount_pct="20", offer_code="SAVE20")
    d["lead"]["risk_flags"] = ["CONSENT_RISK", "TOS_RISK", "NEW_THING"]
    v = detail_view(d)
    assert (v.title, v.subject, v.source_label, v.offer_line) == ("Draft for EMAIL (email)", "Your guide is ready", "Written by the model", "20% off with code SAVE20")
    assert v.body.startswith("Hi,\n\nHere it is: https://shop.example.test/l/gr-1") and v.verdict_label == "Checks passed" and v.writer_confidence_pct == 60
    assert v.score_headline == "75/100" and v.scores[0] == ("Fit with your ideal customer", "90/100") and len(v.scores) == 5
    assert v.lead_summary == ("Source: An imported contact", "Product: gr-1", "Scoring confidence: 80%")
    assert v.risk_flags == ("No consent on record for this lead", "Might break a marketplace rule", "new thing")
    assert "Nothing is sent" in v.tier_note and "eligible to be sent" in detail_view(make_detail(tier=1)).tier_note
    assert detail_view(make_detail(source="TEMPLATE")).source_label.startswith("Written from the standard template")
    assert detail_view(make_detail(source="???")).source_label == "Source unknown"


@pytest.mark.parametrize("kw,can,why", [
    (dict(verdict="PASS"), True, None), (dict(verdict="BLOCK"), False, "The checks blocked this draft. Reject it, or have it edited and re-checked."),
    (dict(verdict="ESCALATE"), False, "The checks cannot decide this one. Reject it, or have it edited and re-checked."),
    (dict(verdict=None), False, "The checks have not run yet."), (dict(verdict="PASS", status="APPROVED"), False, "Someone already decided this one."),
    (dict(verdict="BLOCK", status="REJECTED"), False, "Someone already decided this one.")])
def test_VM5_a_draft_can_be_approved_only_when_pending_and_passed(kw, can, why):
    v = detail_view(make_detail(**kw))
    assert (v.can_approve, v.approve_blocked_reason) == (can, why)


def test_VM6_findings_get_plain_labels_and_unknown_codes_still_show():
    v = detail_view(make_detail(verdict="BLOCK", findings=[("LINK_HOST_NOT_ALLOWED", "BLOCK"), ("URGENCY_PRESSURE", "ESCALATE"), ("BRAND_NEW_RULE", "BLOCK")]))
    assert [(f.code, f.label, f.severity) for f in v.findings] == [
        ("LINK_HOST_NOT_ALLOWED", "A link goes to a site that is not allowed", "BLOCK"), ("URGENCY_PRESSURE", "It uses pressure language", "ESCALATE"),
        ("BRAND_NEW_RULE", "brand new rule", "BLOCK")]


def test_VM7_missing_or_malformed_parts_never_crash_the_view():
    v = detail_view({})
    assert (v.subject, v.body, v.score_headline, v.scores, v.findings, v.verdict_label, v.can_approve) == (
        "(no subject)", "", "Not scored", (), (), "Not checked yet", False)
    odd = detail_view({"payload": "text", "lead": "x", "findings": [None, {"rule_code": 5}, {"rule_code": "OK_CODE"}], "status": "PROPOSED", "compliance_verdict": "PASS"})
    assert [f.code for f in odd.findings] == ["OK_CODE"] and odd.scores == () and odd.can_approve is True
    unscored = detail_view(make_detail(lead={"status": "SIGNAL_CAPTURED", "composite_score": None, "confidence": None, "icp_match_score": None,
                                             "risk_flags": None, "product_external_id": None, "source_type": "X"}))
    assert unscored.score_headline == "Not scored" and unscored.scores[0] == ("Fit with your ideal customer", "not available") and unscored.risk_flags == ()


def test_VM8_the_view_never_shows_a_consent_reference_a_lead_id_or_an_address():
    d = make_detail()
    d["payload"].update(consent_ref="11111111-2222-3333-4444-555555555555", contact="jane.doe@example.test")
    text = repr(detail_view(d))
    assert "11111111" not in text and d["lead_id"] not in text and "jane.doe" not in text and d["proposal_id"] in text


def test_VM9_every_rule_code_the_guard_can_produce_has_a_plain_label():
    codes = set()
    for name in ("guard_rules.py", "guard.py"):
        codes |= set(re.findall(r'Finding\("([A-Z_]+)"', (ROOT / "orchestrator" / name).read_text()))
    codes |= {"MISSING_CONSENT_REF", "CONTEXT_NOT_ALLOWED", "MESSAGE_TOO_LONG", "MESSAGING_UNSUPPORTED", "WRONG_MARKETPLACE",
              "LISTING_MUTATION_UNSUPPORTED", "OFFER_CODES_UNSUPPORTED", "BROWSER_AUTOMATION", "CAPTCHA_BYPASS", "DEVICE_VERIFICATION_BYPASS",
              "ACCOUNT_LINKING_BYPASS"}
    assert len(codes) >= 25 and codes - set(FINDING_LABELS) == set()


def test_VM10_a_good_draft_round_trips_through_the_fake_detail():
    v = detail_view(make_detail(body=GOOD_BODY))
    assert v.subject == "Your guide is ready" and "Thanks!" in v.body

"""Deterministic template baseline, including that it passes the Compliance Guard's own rules. Names TP*."""
import uuid
from decimal import Decimal

import pytest

from orchestrator.copy_schema import SUBJECT_MAX, DraftBrief, ProductFacts
from orchestrator.guard_rules import DraftFacts, GuardConfig, run_rules
from orchestrator.templates import TEMPLATE_CONFIDENCE, TemplateWriter

CFG = GuardConfig(allowed_link_hosts=frozenset({"shop.example.test"}), max_discount_pct=Decimal("20"))


def brief(title="Budget Planner", context="consented_outreach", **kw):
    p = ProductFacts("gr-1", title, 1900, "USD", "https://shop.example.test/l/gr-1")
    base = dict(lead_id=uuid.uuid4(), marketplace="EMAIL", channel="EMAIL", context=context, product=p, signal_kind="SALE",
                contact_permission_id=uuid.uuid4())
    base.update(kw)
    return DraftBrief(**base)


def message(d):
    return f"Subject: {d.subject}\n\n{d.body}"


def test_TP1_the_outreach_template_is_deterministic_and_names_the_product_and_link():
    b = brief()
    d = TemplateWriter().write(b)
    assert d == TemplateWriter().write(b) and d.subject == "About Budget Planner" and d.confidence == TEMPLATE_CONFIDENCE == 0.6
    assert "Budget Planner" in d.body and "https://shop.example.test/l/gr-1" in d.body and "reply to this email" in d.body
    assert "unsubscribe" not in d.body.lower() and "{" not in d.body                 # the adapter adds the footer


def test_TP2_a_buyers_thread_gets_the_reply_wording():
    d = TemplateWriter().write(brief(context="buyer_initiated", contact_permission_id=None))
    assert "Thanks for getting in touch about Budget Planner" in d.body and "I thought you might be interested" not in d.body


def test_TP3_a_long_title_is_cut_to_a_valid_subject():
    d = TemplateWriter().write(brief(title="A" * 120))
    assert len(d.subject) <= SUBJECT_MAX and d.subject.endswith("...") and d.subject.startswith("About A")


@pytest.mark.parametrize("pct,shown", [(Decimal("20"), "20"), (Decimal("20.0"), "20"), (Decimal("12.5"), "12.5"), (Decimal("5"), "5")])
def test_TP4_a_discount_line_states_exactly_the_percentage_and_the_code(pct, shown):
    d = TemplateWriter().write(brief(discount_pct=pct, offer_code="SAVE"))
    assert f"Use code SAVE for {shown}% off." in d.body


@pytest.mark.parametrize("context", ["consented_outreach", "buyer_initiated", "order_related"])
@pytest.mark.parametrize("discount", [None, Decimal("20"), Decimal("20.0")])
def test_TP5_the_baseline_passes_the_guards_rules_with_no_findings(context, discount):
    b = brief(context=context, discount_pct=discount, offer_code="SAVE" if discount else None,
              contact_permission_id=uuid.uuid4() if context == "consented_outreach" else None)
    d = TemplateWriter().write(b)
    facts = DraftFacts(context=context, body=message(d), has_current_permission=True, discount_pct=discount, confidence=d.confidence)
    assert run_rules(facts, CFG) == ()


def test_TP6_hostile_titles_cannot_inject_markup_or_break_the_message():
    d = TemplateWriter().write(brief(title="<script>alert(1)</script> `x`\nSubject: evil"))
    text = message(d)
    assert not any(c in text for c in "<>`") and d.subject.count("\n") == 0
    assert text.startswith("Subject: About ") and not any(ln.startswith("Subject:") for ln in text.splitlines()[1:])   # one header only

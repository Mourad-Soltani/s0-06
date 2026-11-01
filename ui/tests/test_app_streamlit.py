"""The Streamlit screen itself, driven with Streamlit's own test harness (AppTest). These tests need streamlit installed
(ui/requirements.txt); they import it at the top on purpose, so a CI that forgot to install it fails loudly instead of
skipping. The logic behind the screen is tested without Streamlit in test_dashboard.py. Names AT*."""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from ui.dashboard import Dashboard
from ui.fake_client import FakeReviewClient, make_detail

APP = str(Path(__file__).resolve().parents[1] / "app.py")
TOKEN = "reviewer-token-0123456789"


def fresh():
    return AppTest.from_file(APP, default_timeout=30)


def signed_in(client):
    dash = Dashboard(client)
    dash.refresh()
    at = fresh()
    at.session_state["dashboard"] = dash
    return at.run()


def texts(at):
    return [t.value for t in at.text]


def test_AT1_signed_out_the_screen_asks_for_the_token_and_nothing_else():
    at = fresh().run()
    assert not at.exception and at.title[0].value == "Review queue"
    assert at.text_input(key="token") is not None and at.button(key="sign_in") is not None
    assert len(at.button) == 1                                              # no Approve or Reject before signing in


def test_AT2_a_valid_token_signs_in_and_shows_the_queue(monkeypatch):
    fake = FakeReviewClient(make_detail())
    monkeypatch.setattr("ui.review_client.ReviewClient", lambda url, token: fake)
    at = fresh().run()
    at.text_input(key="token").set_value(TOKEN)
    at.button(key="sign_in").click().run()
    assert not at.exception and "Your guide is ready" in texts(at) and at.button(key="approve") is not None


def test_AT3_a_rejected_token_shows_an_error_and_stays_signed_out(monkeypatch):
    monkeypatch.setattr("ui.review_client.ReviewClient", lambda url, token: FakeReviewClient(make_detail(), token_ok=False))
    at = fresh().run()
    at.text_input(key="token").set_value("wrong-token-0123456789")
    at.button(key="sign_in").click().run()
    assert not at.exception and any("not accepted" in e.value for e in at.error)
    with pytest.raises(KeyError):                                           # AppTest raises for a widget that is not drawn
        at.button(key="approve")


def test_AT4_the_reviewer_sees_the_draft_the_scores_and_the_checks_and_approves():
    d = make_detail(findings=[], verdict="PASS", tier=0)
    fake = FakeReviewClient(d)
    at = signed_in(fake)
    assert not at.exception and "Your guide is ready" in texts(at) and any("Here it is:" in t for t in texts(at))
    assert any("Fit with your ideal customer: 90/100" in t for t in texts(at)) and "Checks passed" in texts(at)
    assert at.button(key="approve").disabled is False
    at.button(key="approve").click().run()
    assert ("approve", d["proposal_id"]) in fake.calls and not at.exception
    assert any(s.value.startswith("Approved.") for s in at.success) and any("Nothing is waiting" in i.value for i in at.info)


def test_AT5_a_blocked_draft_cannot_be_approved_and_the_reason_is_shown():
    fake = FakeReviewClient(make_detail(verdict="BLOCK", findings=[("LINK_HOST_NOT_ALLOWED", "BLOCK")]))
    at = signed_in(fake)
    assert at.button(key="approve").disabled is True
    assert any("site that is not allowed" in e.value for e in at.error) and any("blocked this draft" in c.value for c in at.caption)
    assert not any(c[0] == "approve" for c in fake.calls)


def test_AT6_rejecting_sends_the_chosen_reason():
    d = make_detail(verdict="ESCALATE")
    fake = FakeReviewClient(d)
    at = signed_in(fake)
    at.selectbox(key="reject_reason").select("TONE_OFF")
    at.button(key="reject").click().run()
    assert ("reject", d["proposal_id"], "TONE_OFF") in fake.calls and any(s.value.startswith("Rejected.") for s in at.success)


def test_AT7_signing_out_returns_to_the_token_form():
    at = signed_in(FakeReviewClient(make_detail()))
    at.button(key="sign_out").click().run()
    assert not at.exception and at.text_input(key="token") is not None
    with pytest.raises(KeyError):
        at.button(key="approve")


def test_AT8_text_from_a_draft_is_shown_as_plain_text_never_as_markup():
    hostile = "Subject: <b>Hi</b> **now**\n\n<script>alert(1)</script> [click](https://evil.test) **bold**"
    at = signed_in(FakeReviewClient(make_detail(body=hostile)))
    assert not at.exception and "<b>Hi</b> **now**" in texts(at) and any("<script>alert(1)</script>" in t for t in texts(at))
    assert not any("<script>" in m.value for m in at.markdown)                   # nothing from the draft went through markdown

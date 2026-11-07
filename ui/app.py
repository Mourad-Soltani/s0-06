"""The review dashboard (S3-04). Run from the repository root:
    REVIEW_API_URL=http://127.0.0.1:8080 streamlit run ui/app.py --server.address 127.0.0.1
It signs in with a review token (typed into a password field, kept only in this browser session, never written anywhere) and
talks to the review API (S2-05). All behaviour lives in ui/dashboard.py and ui/view_model.py; this file only draws it. Everything
that comes from a draft is shown as plain text, so nothing in a message is ever interpreted as markup."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st  # noqa: E402

from ui.dashboard import Dashboard, LoggedOut  # noqa: E402
from ui.review_client import ReviewClient, validate_base_url  # noqa: E402
from ui.view_model import REJECT_REASONS  # noqa: E402

st.set_page_config(page_title="Review queue", layout="wide")


def sign_in() -> None:
    st.title("Review queue")
    st.write("Sign in with your review token.")
    url = st.text_input("Review service address", value=os.environ.get("REVIEW_API_URL", "http://127.0.0.1:8080"), key="api_url")
    token = st.text_input("Review token", type="password", key="token")
    if st.button("Sign in", key="sign_in"):
        try:
            dash = Dashboard(ReviewClient(validate_base_url(url), token))
            dash.refresh()
        except ValueError as exc:                     # fixed texts from the client: safe to show
            st.error(str(exc))
            return
        except LoggedOut:
            st.error("That token was not accepted.")
            return
        notice = dash.take_notice()
        if notice is not None and notice.kind == "error":
            st.error(notice.text)
            return
        st.session_state["dashboard"] = dash
        st.rerun()                                    # the next run no longer draws the token field, so its value is dropped


def render(dash: Dashboard) -> None:
    head = st.columns([6, 1, 1])
    head[0].title("Review queue")
    if head[1].button("Refresh", key="refresh"):
        dash.refresh()
        st.rerun()
    if head[2].button("Sign out", key="sign_out"):
        st.session_state.pop("dashboard", None)
        st.rerun()
    notice = dash.take_notice()
    if notice is not None:
        {"success": st.success, "error": st.error}.get(notice.kind, st.info)(notice.text)
    if not dash.rows:
        st.info("Nothing is waiting for review.")
        return
    ids = [r.proposal_id for r in dash.rows]
    labels = {r.proposal_id: r.label for r in dash.rows}
    chosen = st.radio("Waiting for review", ids, index=ids.index(dash.selected) if dash.selected in ids else 0,
                      format_func=lambda i: labels[i], key="queue_choice")
    if chosen != dash.selected:
        dash.select(chosen)
    v = dash.detail
    if v is None:
        st.info("This draft could not be loaded.")
        return

    left, right = st.columns([3, 2])
    with left:
        st.subheader(v.title)
        st.caption(v.source_label)
        st.markdown("**Subject**")
        st.text(v.subject)
        st.markdown("**Message**")
        st.text(v.body)
        if v.offer_line:
            st.caption(v.offer_line)
        if v.route_line:
            st.caption(v.route_line)
    with right:
        st.subheader("Why it is here")
        st.metric("Lead score", v.score_headline)
        for label, value in v.scores:
            st.text(f"{label}: {value}")
        for line in v.lead_summary:
            st.caption(line)
        for flag in v.risk_flags:
            st.warning(flag)
        st.subheader("Checks")
        st.text(v.verdict_label)
        for f in v.findings:
            (st.error if f.severity == "BLOCK" else st.warning)(f.label)
        if v.writer_confidence_pct is not None:
            st.caption(f"The writer's confidence: {v.writer_confidence_pct}%")

    st.divider()
    st.info(v.tier_note)
    a, r = st.columns(2)
    with a:
        if st.button("Approve", key="approve", type="primary", disabled=not v.can_approve):
            dash.approve()
            st.rerun()
        if not v.can_approve and v.approve_blocked_reason:
            st.caption(v.approve_blocked_reason)
    with r:
        labels_by_code = dict(REJECT_REASONS)
        reason = st.selectbox("Reason for rejecting", list(labels_by_code), format_func=labels_by_code.get, key="reject_reason")
        if st.button("Reject", key="reject"):
            dash.reject(reason)
            st.rerun()


dashboard = st.session_state.get("dashboard")
if dashboard is None:
    sign_in()
else:
    try:
        render(dashboard)
    except LoggedOut:
        st.session_state.pop("dashboard", None)
        st.warning("Your session ended. Please sign in again.")
        sign_in()

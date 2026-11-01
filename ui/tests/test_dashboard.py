"""The dashboard's behaviour with a fake client: selection, approving, rejecting, failures. No Streamlit. Names DB*."""
import pytest

from ui.dashboard import Dashboard, LoggedOut
from ui.fake_client import FakeReviewClient, make_detail
from ui.review_client import ReviewApiError, Unreachable


def setup(*details, **kw):
    client = FakeReviewClient(*details, **kw)
    dash = Dashboard(client)
    dash.refresh()
    return dash, client


def names(client):
    return [c[0] for c in client.calls]


def test_DB1_refresh_lists_the_oldest_first_selects_it_and_loads_its_draft():
    old, new = make_detail(age_minutes=30), make_detail(age_minutes=1)
    dash, client = setup(new, old)
    assert [r.proposal_id for r in dash.rows] == [old["proposal_id"], new["proposal_id"]] and dash.selected == old["proposal_id"]
    assert dash.detail.proposal_id == old["proposal_id"] and dash.take_notice() is None


def test_DB2_a_refresh_keeps_the_selection_while_it_exists_and_moves_on_when_it_does_not():
    a, b = make_detail(age_minutes=30), make_detail(age_minutes=1)
    dash, client = setup(a, b)
    dash.select(b["proposal_id"])
    dash.refresh()
    assert dash.selected == b["proposal_id"]
    client.items[b["proposal_id"]]["status"] = "APPROVED"                                # decided elsewhere
    dash.refresh()
    assert dash.selected == a["proposal_id"]
    client.items[a["proposal_id"]]["status"] = "REJECTED"
    dash.refresh()
    assert (dash.rows, dash.selected, dash.detail) == ([], None, None)


def test_DB3_selecting_an_item_that_is_not_in_the_queue_is_refused_with_a_notice():
    a = make_detail()
    dash, client = setup(a)
    dash.select("not-in-the-queue")
    n = dash.take_notice()
    assert n.kind == "error" and "no longer in the queue" in n.text and dash.selected == a["proposal_id"]


def test_DB4_approving_a_passed_draft_calls_the_api_says_what_it_means_and_moves_to_the_next():
    a, b = make_detail(age_minutes=30, tier=0), make_detail(age_minutes=1)
    dash, client = setup(a, b)
    dash.approve()
    assert ("approve", a["proposal_id"]) in client.calls and client.items[a["proposal_id"]]["status"] == "APPROVED"
    n = dash.take_notice()
    assert n.kind == "success" and n.text.startswith("Approved. Shadow mode") and dash.selected == b["proposal_id"]
    assert dash.take_notice() is None                                                    # a notice is shown once


@pytest.mark.parametrize("verdict,text", [("BLOCK", "blocked this draft"), ("ESCALATE", "cannot decide"), (None, "have not run")])
def test_DB5_a_draft_the_checks_did_not_pass_is_refused_without_calling_the_api(verdict, text):
    dash, client = setup(make_detail(verdict=verdict))
    dash.approve()
    n = dash.take_notice()
    assert n.kind == "error" and text in n.text and "approve" not in names(client)


def test_DB6_if_the_servers_view_changed_the_refusal_is_explained_and_the_queue_reloaded():
    a = make_detail()
    dash, client = setup(a)
    client.items[a["proposal_id"]]["compliance_verdict"] = "BLOCK"                      # the Guard re-ran meanwhile
    dash.approve()
    n = dash.take_notice()
    assert n.kind == "error" and "checks have not passed" in n.text and names(client).count("list") == 2


def test_DB7_a_draft_someone_else_already_decided_is_reported_and_removed():
    a, b = make_detail(age_minutes=30), make_detail(age_minutes=1)
    dash, client = setup(a, b)
    client.items[a["proposal_id"]]["status"] = "APPROVED"
    dash.approve()
    n = dash.take_notice()
    assert n.kind == "error" and n.text == "Someone already decided this one." and dash.selected == b["proposal_id"]


def test_DB8_rejecting_sends_the_reason_and_unknown_reasons_never_reach_the_api():
    a = make_detail(verdict="BLOCK")
    dash, client = setup(a)
    with pytest.raises(ValueError):
        dash.reject("made up reason")
    assert "reject" not in names(client)
    dash.reject("TONE_OFF")
    assert ("reject", a["proposal_id"], "TONE_OFF") in client.calls and dash.take_notice().kind == "success" and dash.rows == []


def test_DB9_nothing_selected_means_a_notice_not_a_call():
    dash, client = setup()
    dash.approve()
    assert dash.take_notice().text == "Nothing is selected."
    dash.reject("TONE_OFF")
    assert dash.take_notice().text == "Nothing is selected." and names(client) == ["list"]


@pytest.mark.parametrize("action", ["refresh", "select", "approve", "reject"])
def test_DB10_a_rejected_token_logs_the_person_out_wherever_it_is_noticed(action):
    a = make_detail()
    dash, client = setup(a)
    client.token_ok = False
    with pytest.raises(LoggedOut):
        {"refresh": dash.refresh, "select": lambda: dash.select(a["proposal_id"]), "approve": dash.approve, "reject": lambda: dash.reject("TONE_OFF")}[action]()


def test_DB11_an_unreachable_service_keeps_the_last_queue_and_says_so():
    a = make_detail()
    dash, client = setup(a)
    client.fail_with = Unreachable()
    dash.refresh()
    n = dash.take_notice()
    assert n.kind == "error" and "cannot be reached" in n.text and [r.proposal_id for r in dash.rows] == [a["proposal_id"]]


def test_DB12_a_draft_that_cannot_be_loaded_leaves_no_detail_and_a_notice():
    a = make_detail()
    dash, client = setup(a)
    del client.items[a["proposal_id"]]
    dash.select(a["proposal_id"])
    assert dash.detail is None and dash.take_notice().text == "That item no longer exists."


def test_DB13_a_failed_action_is_reported_as_the_actions_result_not_hidden_by_the_reload():
    a = make_detail()
    dash, client = setup(a)
    original = client.approve

    def failing(pid):
        client.calls.append(("approve", str(pid)))
        raise ReviewApiError("INTERNAL", 500)

    client.approve = failing
    dash.approve()
    n = dash.take_notice()
    assert n.kind == "error" and "problem" in n.text and dash.selected == a["proposal_id"]

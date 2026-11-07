"""ProposalWriter with a route (S4-03): what is recorded, and that the proposal's identity does not move with the fees. No database. Names PR*."""
from orchestrator.copy_schema import ProductFacts
from orchestrator.proposals import idempotency_key
from orchestrator.router import MOST_PROFITABLE, Route
from orchestrator.tests.test_proposals_unit import PERM, PID, REF, Cur, brief, make

ROUTE = Route("LEMON_SQUEEZY", MOST_PROFITABLE, "gr-1")
LS_BRIEF = dict(product=ProductFacts("ls-1", "Budget Planner", 1900, "USD", "https://buy.example.test/ls-1"))


def test_PR1_the_route_is_recorded_in_the_payload_next_to_the_product_it_chose():
    w, conn, pubs, _ = make(Cur(None), Cur((REF,)), Cur((PID,)))
    assert w.create(brief(**LS_BRIEF), route=ROUTE) == (PID, True)
    payload = conn.calls[1][1][1].obj
    assert payload == {"context": "consented_outreach", "body": "Subject: Hello\n\nHi there", "source": "LLM", "product_external_id": "ls-1",
                       "discount_pct": None, "offer_code": None, "consent_ref": str(PERM), "route_marketplace": "LEMON_SQUEEZY",
                       "route_reason": MOST_PROFITABLE}


def test_PR2_without_a_route_the_payload_and_the_key_are_exactly_what_they_were():
    w, conn, pubs, _ = make(Cur(None), Cur((REF,)), Cur((PID,)))
    w.create(brief())
    payload = conn.calls[1][1][1].obj
    assert "route_marketplace" not in payload and "route_reason" not in payload
    assert conn.calls[0][1] == (idempotency_key(brief()),) == (idempotency_key(brief(), None),)


def test_PR3_the_key_follows_the_product_the_lead_named_not_the_listing_that_won():
    legacy = idempotency_key(brief())                                              # gr-1, as before routing existed
    assert idempotency_key(brief(**LS_BRIEF), "gr-1") == legacy
    assert idempotency_key(brief(**LS_BRIEF)) != legacy                            # without the lead's id the listing would change the key
    w, conn, _, _ = make(Cur(None), Cur((REF,)), Cur((PID,)))
    w.create(brief(**LS_BRIEF), route=ROUTE)
    assert conn.calls[0][1] == (legacy,) and conn.calls[2][1][8] == legacy


def test_PR4_a_lead_already_drafted_is_not_drafted_again_when_a_fee_change_would_pick_another_marketplace():
    w, conn, pubs, log = make(Cur((PID,)), boom=True)                              # the lookup finds the earlier proposal: no model call
    assert w.create(brief(**LS_BRIEF), route=ROUTE) == (PID, False)
    assert conn.calls[0][1] == (idempotency_key(brief()),) and pubs == [] and "draft" not in log

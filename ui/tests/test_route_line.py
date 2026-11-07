"""Which marketplace a draft is routed to, as the reviewer reads it (S4-04). Pure Python, no Streamlit. Names VR*."""
import re
from pathlib import Path

import pytest

from orchestrator.router import REASONS
from ui.fake_client import make_detail
from ui.view_model import ROUTE_REASONS, detail_view, route_line

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("marketplace,reason,expected", [
    ("LEMON_SQUEEZY", "MOST_PROFITABLE", "Checkout on Lemon Squeezy: it leaves the most money after the marketplace's fees"),
    ("GUMROAD", "LEAD_ORIGIN", "Checkout on Gumroad: this is the marketplace the lead came from"),
    ("PAYHIP", "FALLBACK", "Checkout on Payhip: the preferred listing could not be shown"),
    ("GUMROAD", "ONLY_LISTING", "Checkout on Gumroad: it is the only active listing for this product"),
    ("PAYHIP", "FEE_UNKNOWN", "Checkout on Payhip: fees are missing for some listings, so profit could not be compared"),
    ("GUMROAD", "TIE_BREAK", "Checkout on Gumroad: listings tie on profit, so the first by name was chosen")])
def test_VR1_the_detail_says_where_the_checkout_link_points_and_why_in_plain_words(marketplace, reason, expected):
    v = detail_view(make_detail(route=(marketplace, reason)))
    assert v.route_line == expected


def test_VR2_a_draft_that_was_not_routed_says_so_instead_of_staying_silent():
    assert detail_view(make_detail()).route_line == "Checkout marketplace: not recorded"


@pytest.mark.parametrize("payload,expected", [
    ({"route_marketplace": "SHOPIFY"}, "Checkout on Shopify"), ({"route_marketplace": "NEW_SHOP", "route_reason": "BRAND_NEW"}, "Checkout on New Shop"),
    ({"route_marketplace": "GUMROAD", "route_reason": 5}, "Checkout on Gumroad"), ({"route_marketplace": 5}, "Checkout marketplace: not recorded"),
    ({"route_marketplace": ""}, "Checkout marketplace: not recorded"), ({"route_reason": "LEAD_ORIGIN"}, "Checkout marketplace: not recorded"), ({}, "Checkout marketplace: not recorded")])
def test_VR3_odd_or_unknown_values_never_crash_and_unknown_names_are_made_readable(payload, expected):
    assert route_line(payload) == expected


def test_VR4_every_reason_the_router_can_give_has_a_sentence_and_no_stale_ones_remain():
    assert set(ROUTE_REASONS) == set(REASONS) and all(s and s == s.strip() and not s.endswith(".") for s in ROUTE_REASONS.values())


def test_VR5_the_route_line_is_the_same_for_a_draft_that_is_already_decided_and_shows_no_personal_data():
    v = detail_view(make_detail(route=("LEMON_SQUEEZY", "MOST_PROFITABLE"), status="APPROVED"))
    assert v.can_approve is False and v.route_line.startswith("Checkout on Lemon Squeezy")
    assert not re.search(r"@|https?://", v.route_line)


def test_VR6_the_screen_draws_the_route_line_and_only_from_the_view_model():
    src = (ROOT / "ui" / "app.py").read_text()
    assert "v.route_line" in src and "route_marketplace" not in src and "route_reason" not in src

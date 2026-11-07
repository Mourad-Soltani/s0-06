"""The listing router with fakes: the profit arithmetic, the ranking, the lead's own listing, ambiguity. No database. Names RT*."""
import itertools
import uuid

import pytest

from orchestrator.router import (FALLBACK, FEE_UNKNOWN, LEAD_ORIGIN, MOST_PROFITABLE, ONLY_LISTING, REASONS, TIE_BREAK, Fee, Listing, Match,
                                 PgCatalog, Route, Router, net_minor, rank)

LEAD = uuid.uuid4()
P1, P2 = str(uuid.uuid4()), str(uuid.uuid4())
GR, LS, PH = Listing("GUMROAD", "gr-1"), Listing("LEMON_SQUEEZY", "ls-1"), Listing("PAYHIP", "RGsF")
FEES = {"GUMROAD": Fee(1000, 50, "USD"), "LEMON_SQUEEZY": Fee(500, 50, "USD"), "PAYHIP": Fee(0, 0, "USD")}


def ranked(listings, fees=FEES, origin=None, price=1900, currency="USD"):
    return rank(listings, price_minor=price, currency=currency, fees=fees, origin=origin)


def order(choices):
    return [(c.marketplace, c.reason) for c in choices]


# ------------------------------------------------------------------ the arithmetic
@pytest.mark.parametrize("price,currency,fee,expected", [
    (1900, "USD", Fee(500, 50, "USD"), 1755),          # 5% = 95, plus 50
    (1900, "USD", Fee(1000, 50, "USD"), 1660),
    (1900, "USD", Fee(0, 0, "USD"), 1900),
    (999, "USD", Fee(333, 0, "EUR"), 965),             # 33.27 rounds UP to 34: the marketplace's cut is never understated
    (1000, "USD", Fee(500, 0, "USD"), 950),            # exact, no rounding
    (1900, "USD", Fee(10000, 0, "USD"), 0),
    (100, "USD", Fee(0, 150, "USD"), -50),             # a fee larger than the price is a loss, not zero
    (1900, "USD", Fee(500, 50, "EUR"), None),          # a fixed fee in another currency cannot be compared: no exchange rate is invented
    (1900, "USD", None, None)])
def test_RT1_what_the_seller_keeps_is_exact_integer_arithmetic_and_unknown_when_it_cannot_be_told(price, currency, fee, expected):
    assert net_minor(price, currency, fee) == expected


# ------------------------------------------------------------------ the ranking
def test_RT2_the_most_profitable_listing_is_first_and_the_others_follow_in_profit_order():
    got = ranked([GR, LS, PH])
    assert order(got) == [("PAYHIP", MOST_PROFITABLE), ("LEMON_SQUEEZY", FALLBACK), ("GUMROAD", FALLBACK)]
    assert [c.net_minor for c in got] == [1900, 1755, 1660] and [c.external_id for c in got] == ["RGsF", "ls-1", "gr-1"]


def test_RT3_the_leads_own_listing_beats_a_more_profitable_one():
    assert order(ranked([GR, LS, PH], origin=GR)) == [("GUMROAD", LEAD_ORIGIN), ("PAYHIP", FALLBACK), ("LEMON_SQUEEZY", FALLBACK)]


def test_RT3b_the_leads_listing_is_first_even_when_its_fee_is_unknown_or_it_is_the_only_one():
    assert order(ranked([GR, LS], fees={"LEMON_SQUEEZY": FEES["LEMON_SQUEEZY"]}, origin=GR))[0] == ("GUMROAD", LEAD_ORIGIN)
    assert order(ranked([GR], origin=GR)) == [("GUMROAD", LEAD_ORIGIN)]


def test_RT4_equal_profit_is_broken_by_marketplace_name_and_says_so():
    fees = {"GUMROAD": Fee(500, 0, "USD"), "PAYHIP": Fee(500, 0, "USD")}
    assert order(ranked([PH, GR], fees=fees)) == [("GUMROAD", TIE_BREAK), ("PAYHIP", FALLBACK)]


def test_RT4b_a_tie_below_the_top_is_not_reported_as_a_tie():
    fees = {"GUMROAD": Fee(500, 0, "USD"), "PAYHIP": Fee(0, 0, "USD"), "LEMON_SQUEEZY": Fee(500, 0, "USD")}
    assert order(ranked([GR, LS, PH], fees=fees))[0] == ("PAYHIP", MOST_PROFITABLE)


def test_RT5_a_listing_without_fee_data_ranks_below_every_listing_with_data_and_the_reason_says_profit_was_not_established():
    got = ranked([PH, GR, LS], fees={"GUMROAD": FEES["GUMROAD"]})
    assert order(got) == [("GUMROAD", FEE_UNKNOWN), ("LEMON_SQUEEZY", FALLBACK), ("PAYHIP", FALLBACK)]     # unknowns by name, after the known
    assert [c.net_minor for c in got] == [1660, None, None]


def test_RT5b_when_no_fee_is_known_the_order_is_by_name_and_says_so():
    assert order(ranked([PH, LS, GR], fees={})) == [("GUMROAD", FEE_UNKNOWN), ("LEMON_SQUEEZY", FALLBACK), ("PAYHIP", FALLBACK)]


def test_RT5c_a_fee_in_the_wrong_currency_counts_as_unknown():
    got = ranked([GR, LS], fees={"GUMROAD": Fee(1000, 50, "EUR"), "LEMON_SQUEEZY": FEES["LEMON_SQUEEZY"]})
    assert order(got) == [("LEMON_SQUEEZY", FEE_UNKNOWN), ("GUMROAD", FALLBACK)]


def test_RT6_one_active_listing_is_the_only_choice():
    assert order(ranked([LS])) == [("LEMON_SQUEEZY", ONLY_LISTING)] and ranked([]) == ()


def test_RT7_the_decision_never_depends_on_the_order_the_rows_came_in():
    for perm in itertools.permutations([GR, LS, PH]):
        assert ranked(list(perm)) == ranked([GR, LS, PH])
        assert ranked(list(perm), origin=LS) == ranked([GR, LS, PH], origin=LS)


def test_RT8_two_listings_on_one_marketplace_are_ordered_by_external_id():
    a, b = Listing("GUMROAD", "gr-2"), Listing("GUMROAD", "gr-1")
    assert [c.external_id for c in ranked([a, b])] == ["gr-1", "gr-2"] and ranked([a, b])[0].reason == TIE_BREAK
    assert ranked([a, b], origin=a)[0].external_id == "gr-2"            # the lead's own listing, not just its marketplace


def test_RT9_a_route_needs_a_marketplace_a_known_reason_and_the_leads_product():
    assert Route("LEMON_SQUEEZY", MOST_PROFITABLE, "gr-1").marketplace == "LEMON_SQUEEZY"
    for args in (("lemon", MOST_PROFITABLE, "gr-1"), ("GUMROAD", "BEST", "gr-1"), ("GUMROAD", FALLBACK, ""), ("", FALLBACK, "gr-1"),
                 ("GUMROAD", FALLBACK, "has space")):
        with pytest.raises(ValueError):
            Route(*args)
    assert set(REASONS) == {LEAD_ORIGIN, ONLY_LISTING, MOST_PROFITABLE, TIE_BREAK, FEE_UNKNOWN, FALLBACK}


# ------------------------------------------------------------------ the planner, over a fake catalog
class Catalog:
    def __init__(self, matches=(), source=None, product=(1900, "USD"), listings=(), fees=None):
        self._matches, self._source, self._product, self._listings, self._fees = list(matches), source, product, list(listings), fees if fees is not None else FEES
        self.asked = []

    def matches(self, external_id):
        self.asked.append(("matches", external_id))
        return self._matches

    def lead_source(self, lead_id):
        self.asked.append(("lead_source", lead_id))
        return self._source

    def product(self, product_id):
        self.asked.append(("product", product_id))
        return self._product

    def active_listings(self, product_id):
        self.asked.append(("active_listings", product_id))
        return self._listings

    def fees(self, marketplaces):
        self.asked.append(("fees", tuple(marketplaces)))
        return {m: f for m, f in self._fees.items() if m in marketplaces}


def plan(lead_product="gr-1", **kw):
    cat = Catalog(**kw)
    return Router(lambda conn: cat).plan(object(), LEAD, lead_product), cat


def test_RT10_a_product_the_catalog_does_not_know_is_skipped_not_guessed():
    p, cat = plan(matches=[])
    assert (p.choices, p.skip) == ((), "PRODUCT_NOT_FOUND") and cat.asked == [("matches", "gr-1")]


def test_RT11_the_leads_own_listing_is_found_through_the_product_id_it_names():
    p, _ = plan(matches=[Match("GUMROAD", "gr-1", P1)], listings=[GR, LS, PH])
    assert p.skip is None and order(p.choices)[0] == ("GUMROAD", LEAD_ORIGIN)


def test_RT11b_an_inactive_leads_listing_is_not_the_origin_the_best_active_one_is_used():
    p, _ = plan(matches=[Match("GUMROAD", "gr-1", P1)], listings=[LS, PH])
    assert order(p.choices) == [("PAYHIP", MOST_PROFITABLE), ("LEMON_SQUEEZY", FALLBACK)]


def test_RT12_a_product_with_no_active_listing_is_skipped_with_its_own_code():
    p, _ = plan(matches=[Match("GUMROAD", "gr-1", P1)], listings=[])
    assert (p.choices, p.skip) == ((), "NO_ACTIVE_LISTING")


def test_RT13_the_same_external_id_on_two_products_is_ambiguous_unless_the_leads_source_settles_it():
    both = [Match("GUMROAD", "x-1", P1), Match("PAYHIP", "x-1", P2)]
    assert plan("x-1", matches=both, source="EMAIL", listings=[GR])[0].skip == "PRODUCT_NOT_FOUND"
    assert plan("x-1", matches=both, source=None, listings=[GR])[0].skip == "PRODUCT_NOT_FOUND"
    p, cat = plan("x-1", matches=both, source="PAYHIP", listings=[GR, Listing("PAYHIP", "x-1")])
    assert p.skip is None and ("product", P2) in cat.asked and ("active_listings", P2) in cat.asked and order(p.choices)[0] == ("PAYHIP", LEAD_ORIGIN)


def test_RT14_one_product_listed_under_the_same_id_on_two_marketplaces_uses_the_leads_source_or_no_origin():
    both = [Match("GUMROAD", "same", P1), Match("PAYHIP", "same", P1)]
    listed = [Listing("GUMROAD", "same"), Listing("PAYHIP", "same")]
    assert order(plan("same", matches=both, source="PAYHIP", listings=listed)[0].choices)[0] == ("PAYHIP", LEAD_ORIGIN)
    assert order(plan("same", matches=both, source="GUMROAD", listings=listed)[0].choices)[0] == ("GUMROAD", LEAD_ORIGIN)
    p, _ = plan("same", matches=both, source="EMAIL", listings=listed)               # no way to tell which one: no origin, profit decides
    assert order(p.choices)[0] == ("PAYHIP", MOST_PROFITABLE)


def test_RT15_only_the_fees_of_the_listed_marketplaces_are_read_and_the_price_comes_from_the_product_row():
    p, cat = plan("e-1", matches=[Match("ETSY", "e-1", P1)], listings=[LS, GR], product=(2500, "USD"))      # the lead came from a listing that is no longer active
    assert ("fees", ("GUMROAD", "LEMON_SQUEEZY")) in cat.asked
    assert [(c.marketplace, c.net_minor) for c in p.choices] == [("LEMON_SQUEEZY", 2500 - 125 - 50), ("GUMROAD", 2500 - 250 - 50)]


def test_RT16_a_product_row_that_has_vanished_is_skipped():
    assert plan(matches=[Match("GUMROAD", "gr-1", P1)], product=None, listings=[GR])[0].skip == "PRODUCT_NOT_FOUND"


# ------------------------------------------------------------------ the catalog's SQL
class Cur:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class SqlConn:
    def __init__(self, *results):
        self.queue, self.calls = list(results), []

    def execute(self, sql, params=()):
        self.calls.append((" ".join(sql.split()), params))
        return Cur(self.queue.pop(0) if self.queue else [])


def test_RT17_the_catalog_only_reads_passes_values_as_parameters_and_touches_no_personal_table():
    conn = SqlConn([("GUMROAD", "gr-1", uuid.UUID(P1))], [("EMAIL",)], [(1900, "USD")], [("GUMROAD", "gr-1")], [("GUMROAD", 1000, 50, "USD")])
    cat = PgCatalog(conn)
    assert cat.matches("gr-1") == [Match("GUMROAD", "gr-1", P1)] and cat.lead_source(LEAD) == "EMAIL" and cat.product(P1) == (1900, "USD")
    assert cat.active_listings(P1) == [GR] and cat.fees(["GUMROAD"]) == {"GUMROAD": Fee(1000, 50, "USD")}
    for sql, params in conn.calls:
        low = sql.lower()
        assert low.startswith("select") and not any(w in low for w in ("insert", "update", "delete", "pii.", "contact", "email"))
        assert "gr-1" not in sql and str(LEAD) not in sql                  # values travel as parameters, never inside the text
    assert [c[1] for c in conn.calls] == [("gr-1",), (LEAD,), (P1,), (P1,), (["GUMROAD"],)]


def test_RT17b_the_catalog_trims_char_padding_and_tolerates_a_missing_lead_or_product():
    cat = PgCatalog(SqlConn([], [], [("GUMROAD", 0, 0, "USD ")]))
    assert cat.lead_source(LEAD) is None and cat.product(P1) is None and cat.fees(["GUMROAD"])["GUMROAD"].currency == "USD"

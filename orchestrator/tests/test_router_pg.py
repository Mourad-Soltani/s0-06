"""The router against Postgres: the catalog queries on real rows, the whole plan, and that the application role is enough to run it.
Needs TEST_ADMIN_DSN. Names RTP*."""
import uuid

from orchestrator.router import (FALLBACK, FEE_UNKNOWN, LEAD_ORIGIN, MOST_PROFITABLE, Fee, Listing, Match, PgCatalog, Router)


def seed_product(db, price=1900, currency="USD", title="Planner"):
    return db.conn.execute("INSERT INTO core.product(canonical_title, canonical_description, base_price_minor, currency) VALUES (%s,'d',%s,%s) RETURNING id",
                           (title, price, currency)).fetchone()[0]


def seed_listing(db, product, marketplace, external_id, status="ACTIVE"):
    db.conn.execute("INSERT INTO core.marketplace_listing_ref(product_id, marketplace, external_id, status) VALUES (%s, %s::core.marketplace, %s, %s::core.listing_status)",
                    (product, marketplace, external_id, status))


def seed_fee(db, marketplace, bps, fixed=0, currency="USD"):
    db.conn.execute("INSERT INTO core.marketplace_fee(marketplace, percent_bps, fixed_minor, currency) VALUES (%s::core.marketplace, %s, %s, %s)", (marketplace, bps, fixed, currency))


def seed_lead(db, product_id, source="EMAIL"):
    ref = uuid.uuid4().hex[:12]
    return db.conn.execute("INSERT INTO core.lead(source_type, source_marketplace, signal_ref, signal_id, product_external_id) "
                           "VALUES ('INBOUND', %s::core.marketplace, %s, %s, %s) RETURNING id", (source, ref, uuid.uuid4().hex * 2, product_id)).fetchone()[0]


def shop(db):
    """One product on three marketplaces, one archived listing, and fees for two of the three."""
    p = seed_product(db)
    seed_listing(db, p, "GUMROAD", "gr-1")
    seed_listing(db, p, "LEMON_SQUEEZY", "ls-1")
    seed_listing(db, p, "PAYHIP", "RGsF")
    seed_listing(db, p, "GUMROAD", "gr-old", status="ARCHIVED")
    seed_fee(db, "GUMROAD", 1000, 50)
    seed_fee(db, "LEMON_SQUEEZY", 500, 50)
    return p


def test_RTP1_the_catalog_reads_real_rows_into_plain_values(db):
    p = shop(db)
    cat = PgCatalog(db.conn)
    assert cat.matches("gr-old") == [Match("GUMROAD", "gr-old", str(p))]
    assert cat.matches("nothing") == [] and cat.product(str(p)) == (1900, "USD")
    assert cat.active_listings(str(p)) == [Listing("GUMROAD", "gr-1"), Listing("LEMON_SQUEEZY", "ls-1"), Listing("PAYHIP", "RGsF")]        # archived is not offered
    assert cat.fees(["GUMROAD", "PAYHIP", "LEMON_SQUEEZY"]) == {"GUMROAD": Fee(1000, 50, "USD"), "LEMON_SQUEEZY": Fee(500, 50, "USD")}      # Payhip: no row
    assert cat.lead_source(seed_lead(db, "gr-1", source="GUMROAD")) == "GUMROAD" and cat.lead_source(uuid.uuid4()) is None


def test_RTP2_a_lead_from_an_active_listing_is_routed_back_to_it(db):
    shop(db)
    plan = Router().plan(db.conn, seed_lead(db, "gr-1"), "gr-1")
    assert plan.skip is None and [(c.marketplace, c.external_id, c.reason) for c in plan.choices][0] == ("GUMROAD", "gr-1", LEAD_ORIGIN)
    assert [c.marketplace for c in plan.choices] == ["GUMROAD", "LEMON_SQUEEZY", "PAYHIP"]


def test_RTP3_a_lead_whose_listing_is_archived_gets_the_best_active_one_and_a_missing_fee_is_said_aloud(db):
    shop(db)
    plan = Router().plan(db.conn, seed_lead(db, "gr-old"), "gr-old")
    got = [(c.marketplace, c.reason, c.net_minor) for c in plan.choices]
    assert got == [("LEMON_SQUEEZY", FEE_UNKNOWN, 1755), ("GUMROAD", FALLBACK, 1660), ("PAYHIP", FALLBACK, None)]       # Payhip has no fee row


def test_RTP4_with_every_fee_known_the_most_profitable_listing_wins(db):
    shop(db)
    seed_fee(db, "PAYHIP", 0, 0)
    plan = Router().plan(db.conn, seed_lead(db, "gr-old"), "gr-old")
    assert [(c.marketplace, c.reason) for c in plan.choices] == [("PAYHIP", MOST_PROFITABLE), ("LEMON_SQUEEZY", FALLBACK), ("GUMROAD", FALLBACK)]


def test_RTP5_only_active_listings_count_and_a_product_with_none_is_skipped(db):
    p = seed_product(db)
    for status in ("DRAFT", "PAUSED", "ARCHIVED"):
        seed_listing(db, p, "GUMROAD", f"gr-{status}", status=status)
    lead = seed_lead(db, "gr-PAUSED")
    assert Router().plan(db.conn, lead, "gr-PAUSED").skip == "NO_ACTIVE_LISTING"
    assert Router().plan(db.conn, lead, "unknown-id").skip == "PRODUCT_NOT_FOUND"


def test_RTP6_one_external_id_on_two_products_is_ambiguous_unless_the_leads_own_marketplace_settles_it(db):
    a, b = seed_product(db, title="A"), seed_product(db, title="B")
    seed_listing(db, a, "GUMROAD", "x-1")
    seed_listing(db, b, "PAYHIP", "x-1")
    seed_listing(db, b, "LEMON_SQUEEZY", "ls-b")
    assert Router().plan(db.conn, seed_lead(db, "x-1", source="EMAIL"), "x-1").skip == "PRODUCT_NOT_FOUND"
    plan = Router().plan(db.conn, seed_lead(db, "x-1", source="PAYHIP"), "x-1")
    assert plan.skip is None and {c.external_id for c in plan.choices} == {"x-1", "ls-b"} and plan.choices[0].marketplace == "PAYHIP"


def test_RTP7_the_application_role_alone_can_route_and_planning_writes_nothing(db):
    shop(db)
    lead = seed_lead(db, "gr-1")
    before = [db.conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("core.outbox_event", "audit.event", "core.action_proposal", "core.lead")]
    db.conn.execute("SET ROLE app_rw")
    try:
        plan = Router().plan(db.conn, lead, "gr-1")
    finally:
        db.conn.execute("RESET ROLE")
    assert plan.skip is None and plan.choices
    assert before == [db.conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("core.outbox_event", "audit.event", "core.action_proposal", "core.lead")]

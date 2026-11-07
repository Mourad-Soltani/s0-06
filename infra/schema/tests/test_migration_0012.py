"""Migration 0012 (fees and routing indexes). Needs TEST_ADMIN_DSN. Names are RM*."""
import psycopg
import pytest
from alembic import command
from psycopg import errors

from conftest import dsn_for
from test_alembic_roundtrip import _cfg, fresh_db  # noqa: F401  (fresh_db is a fixture)

FEE = "INSERT INTO core.marketplace_fee(marketplace, percent_bps, fixed_minor, currency) VALUES (%s::core.marketplace, %s, %s, %s)"


def test_RM1_downgrade_to_0011_removes_only_the_fee_table_and_the_two_indexes(fresh_db):
    cfg, name = _cfg(fresh_db), fresh_db
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0011")
    with psycopg.connect(dsn_for(name), autocommit=True) as c:
        assert c.execute("SELECT to_regclass('core.marketplace_fee') IS NULL").fetchone()[0] is True
        assert c.execute("SELECT to_regclass('core.signal_inbox') IS NOT NULL").fetchone()[0] is True
        names = {r[0] for r in c.execute("SELECT indexname FROM pg_indexes WHERE schemaname='core' AND tablename='marketplace_listing_ref'").fetchall()}
        assert not names & {"listing_ref_external_idx", "listing_ref_active_product_idx"}
    command.upgrade(cfg, "head")
    with psycopg.connect(dsn_for(name), autocommit=True) as c:
        names = {r[0] for r in c.execute("SELECT indexname FROM pg_indexes WHERE schemaname='core' AND tablename='marketplace_listing_ref'").fetchall()}
        assert {"listing_ref_external_idx", "listing_ref_active_product_idx"} <= names
        assert c.execute("SELECT count(*) FROM core.marketplace_fee").fetchone()[0] == 0          # no seed rows: a fee is the seller's own number


def test_RM2_the_application_role_can_read_the_fees_but_not_change_them(fresh_db):
    command.upgrade(_cfg(fresh_db), "head")
    with psycopg.connect(dsn_for(fresh_db), autocommit=True) as c:
        assert c.execute("SELECT has_table_privilege('app_rw','core.marketplace_fee','SELECT')").fetchone()[0] is True
        for priv in ("INSERT", "UPDATE", "DELETE"):
            assert c.execute("SELECT has_table_privilege('app_rw','core.marketplace_fee', %s)", (priv,)).fetchone()[0] is False, priv


def test_RM3_the_fee_table_refuses_nonsense(fresh_db):
    command.upgrade(_cfg(fresh_db), "head")
    with psycopg.connect(dsn_for(fresh_db), autocommit=True) as c:
        c.execute(FEE, ("LEMON_SQUEEZY", 500, 50, "USD"))
        c.execute(FEE, ("PAYHIP", 0, 0, "EUR"))
        c.execute(FEE, ("GUMROAD", 10000, 0, "USD"))                                                 # the boundary is allowed
        for args in (("ETSY", 10001, 0, "USD"), ("ETSY", -1, 0, "USD"), ("ETSY", 100, -1, "USD"), ("ETSY", 100, 0, "usd"),
                     ("ETSY", 100, 0, "US"), ("ETSY", 100, 0, "US1")):
            with pytest.raises(errors.CheckViolation):
                c.execute(FEE, args)
        with pytest.raises(errors.UniqueViolation):
            c.execute(FEE, ("LEMON_SQUEEZY", 100, 0, "USD"))                                        # one row per marketplace
        with pytest.raises(errors.InvalidTextRepresentation):
            c.execute(FEE, ("NOT_A_MARKETPLACE", 100, 0, "USD"))


def test_RM4_the_leads_product_id_lookup_and_the_active_listings_lookup_use_their_indexes(fresh_db):
    command.upgrade(_cfg(fresh_db), "head")
    with psycopg.connect(dsn_for(fresh_db), autocommit=True) as c:
        pid = c.execute("INSERT INTO core.product(canonical_title, canonical_description, base_price_minor, currency) VALUES ('P','d',1900,'USD') RETURNING id").fetchone()[0]
        for i in range(300):                                                                        # enough rows that a scan is not the cheapest plan
            c.execute("INSERT INTO core.marketplace_listing_ref(product_id, marketplace, external_id, status) VALUES (%s, 'GUMROAD', %s, 'ARCHIVED')", (pid, f"old-{i}"))
        c.execute("ANALYZE core.marketplace_listing_ref")
        c.execute("SET enable_seqscan = off")                                                       # prove the index CAN serve the query, whatever the planner prefers
        by_id = " ".join(r[0] for r in c.execute("EXPLAIN SELECT marketplace::text, external_id, product_id FROM core.marketplace_listing_ref WHERE external_id = 'old-7'").fetchall())
        active = " ".join(r[0] for r in c.execute("EXPLAIN SELECT marketplace::text, external_id FROM core.marketplace_listing_ref WHERE product_id = %s AND status = 'ACTIVE'", (pid,)).fetchall())
        assert "listing_ref_external_idx" in by_id and "listing_ref_active_product_idx" in active

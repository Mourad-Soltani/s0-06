"""Migration 0010. Needs TEST_ADMIN_DSN. Names are AQ*."""
import psycopg
import pytest
from alembic import command
from psycopg import errors

from conftest import dsn_for
from test_alembic_roundtrip import _cfg, fresh_db  # noqa: F401  (fresh_db is a fixture)


def test_AQ1_downgrade_to_0009_removes_the_column_and_the_index_only(fresh_db):
    cfg, name = _cfg(fresh_db), fresh_db
    command.upgrade(cfg, "head")
    has_col = "SELECT count(*) FROM information_schema.columns WHERE table_schema='core' AND table_name='action_proposal' AND column_name='provider_ref'"
    has_idx = "SELECT count(*) FROM pg_indexes WHERE schemaname='core' AND indexname='action_proposal_sending_idx'"
    with psycopg.connect(dsn_for(name), autocommit=True) as c:
        assert (c.execute(has_col).fetchone()[0], c.execute(has_idx).fetchone()[0]) == (1, 1)
    command.downgrade(cfg, "0009")
    with psycopg.connect(dsn_for(name), autocommit=True) as c:
        assert (c.execute(has_col).fetchone()[0], c.execute(has_idx).fetchone()[0]) == (0, 0)
        assert c.execute("SELECT count(*) FROM information_schema.columns WHERE table_schema='core' AND table_name='lead' AND column_name='product_external_id'").fetchone()[0] == 1
    command.upgrade(cfg, "head")


def test_AQ2_the_provider_reference_must_be_32_hex_characters(fresh_db):
    command.upgrade(_cfg(fresh_db), "head")
    sql = ("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = 'core.action_proposal'::regclass AND contype = 'c' "
           "AND pg_get_constraintdef(oid) LIKE '%provider_ref%'")
    with psycopg.connect(dsn_for(fresh_db), autocommit=True) as c:
        (definition,) = c.execute(sql).fetchone()
    assert "[0-9a-f]{32}" in definition and "IS NULL" in definition

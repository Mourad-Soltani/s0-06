"""Migration 0011. Needs TEST_ADMIN_DSN. Names are AS*."""
import psycopg
import pytest
from alembic import command
from psycopg import errors

from conftest import dsn_for
from test_alembic_roundtrip import _cfg, fresh_db  # noqa: F401  (fresh_db is a fixture)


def test_AS1_downgrade_to_0010_removes_only_the_inbox(fresh_db):
    cfg, name = _cfg(fresh_db), fresh_db
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0010")
    with psycopg.connect(dsn_for(name), autocommit=True) as c:
        assert c.execute("SELECT to_regclass('core.signal_inbox') IS NULL").fetchone()[0] is True
        assert c.execute("SELECT count(*) FROM information_schema.columns WHERE table_schema='core' AND table_name='action_proposal' AND column_name='provider_ref'").fetchone()[0] == 1
    command.upgrade(cfg, "head")
    with psycopg.connect(dsn_for(name), autocommit=True) as c:
        assert c.execute("SELECT has_table_privilege('app_rw','core.signal_inbox','UPDATE')").fetchone()[0] is False
        assert c.execute("SELECT has_table_privilege('app_rw','core.signal_inbox','DELETE')").fetchone()[0] is False


def test_AS2_the_inbox_refuses_malformed_rows(fresh_db):
    command.upgrade(_cfg(fresh_db), "head")
    ins = "INSERT INTO core.signal_inbox(marketplace, signal_ref, kind, product_external_id, occurred_at) VALUES ('PAYHIP', %s, %s, %s, now())"
    with psycopg.connect(dsn_for(fresh_db), autocommit=True) as c:
        c.execute(ins, ("ph_tx_1_0", "SALE", "RGsF"))
        for ref, kind, product in (("has space", "SALE", None), ("x", "WAT", None), ("y", "SALE", "bad product!"), ("ph_tx_1_0", "SALE", None)):
            with pytest.raises((errors.CheckViolation, errors.UniqueViolation)):
                c.execute(ins, (ref, kind, product))

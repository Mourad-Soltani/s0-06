"""Migration 0009. Needs TEST_ADMIN_DSN. Names are AP*."""
import psycopg
import pytest
from alembic import command
from psycopg import errors

from conftest import dsn_for
from test_alembic_roundtrip import _cfg, fresh_db  # noqa: F401  (fresh_db is a fixture)


def test_AP1_downgrade_to_0008_removes_only_the_product_column(fresh_db):
    cfg, name = _cfg(fresh_db), fresh_db
    command.upgrade(cfg, "head")
    has = "SELECT count(*) FROM information_schema.columns WHERE table_schema='core' AND table_name='lead' AND column_name='product_external_id'"
    with psycopg.connect(dsn_for(name), autocommit=True) as c:
        assert c.execute(has).fetchone()[0] == 1
        with pytest.raises(errors.CheckViolation):
            c.execute("INSERT INTO core.lead(source_type,signal_ref,signal_id,product_external_id) VALUES ('INBOUND','x',repeat('a',64),'has space')")
    command.downgrade(cfg, "0008")
    with psycopg.connect(dsn_for(name), autocommit=True) as c:
        assert c.execute(has).fetchone()[0] == 0
        assert c.execute("SELECT to_regclass('core.compliance_finding') IS NOT NULL").fetchone()[0] is True
    command.upgrade(cfg, "head")

"""Migration 0008 downgrade. Needs TEST_ADMIN_DSN. Names are AG*."""
import psycopg
from alembic import command

from conftest import dsn_for
from test_alembic_roundtrip import _cfg, fresh_db  # noqa: F401  (fresh_db is a fixture)


def test_AG1_downgrade_to_0007_removes_only_compliance_finding(fresh_db):
    cfg, name = _cfg(fresh_db), fresh_db
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0007")
    with psycopg.connect(dsn_for(name), autocommit=True) as c:
        assert c.execute("SELECT to_regclass('core.compliance_finding') IS NULL").fetchone()[0] is True
        assert c.execute("SELECT to_regclass('core.review_edit') IS NOT NULL").fetchone()[0] is True
    command.upgrade(cfg, "head")
    with psycopg.connect(dsn_for(name), autocommit=True) as c:
        assert c.execute("SELECT has_table_privilege('app_rw','core.compliance_finding','UPDATE')").fetchone()[0] is False
        assert c.execute("SELECT has_table_privilege('app_rw','core.compliance_finding','DELETE')").fetchone()[0] is False

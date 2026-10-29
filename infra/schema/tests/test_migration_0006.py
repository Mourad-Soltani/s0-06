"""Migration 0006 downgrade. Needs TEST_ADMIN_DSN. Names are AR*."""
import psycopg
from alembic import command

from conftest import dsn_for
from test_alembic_roundtrip import _cfg, fresh_db  # noqa: F401  (fresh_db is a fixture)


def test_AR1_downgrade_to_0005_removes_only_review_decision(fresh_db):
    cfg, name = _cfg(fresh_db), fresh_db
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0005")
    with psycopg.connect(dsn_for(name), autocommit=True) as c:
        assert c.execute("SELECT to_regclass('core.review_decision') IS NULL").fetchone()[0] is True
        assert c.execute("SELECT to_regclass('core.scout_cursor') IS NOT NULL").fetchone()[0] is True
    command.upgrade(cfg, "head")
    with psycopg.connect(dsn_for(name), autocommit=True) as c:
        assert c.execute("SELECT has_table_privilege('app_rw','core.review_decision','UPDATE')").fetchone()[0] is False

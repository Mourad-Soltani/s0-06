"""Migration 0005 downgrade. Needs TEST_ADMIN_DSN. Names are AN*."""
import psycopg
from alembic import command

from conftest import dsn_for
from test_alembic_roundtrip import _cfg, fresh_db  # noqa: F401  (fresh_db is a fixture)


def test_AN1_downgrade_to_0004_removes_only_the_cursor_table(fresh_db):
    cfg, name = _cfg(fresh_db), fresh_db
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0004")
    with psycopg.connect(dsn_for(name), autocommit=True) as c:
        q = lambda sql: c.execute(sql).fetchone()[0]  # noqa: E731
        assert q("SELECT to_regclass('core.scout_cursor') IS NULL") is True
        assert q("SELECT to_regprocedure('audit.record_event(uuid,text,core.marketplace,uuid,uuid,text,bytea,text,smallint,text,bytea,uuid)') IS NOT NULL") is True
    command.upgrade(cfg, "head")
    with psycopg.connect(dsn_for(name), autocommit=True) as c:
        assert c.execute("SELECT to_regclass('core.scout_cursor') IS NOT NULL").fetchone()[0] is True

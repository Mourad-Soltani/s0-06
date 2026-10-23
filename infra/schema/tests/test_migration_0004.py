"""Migration 0004 downgrade. Needs TEST_ADMIN_DSN. Names are AM*."""
import psycopg
from alembic import command

from conftest import dsn_for
from test_alembic_roundtrip import _cfg, fresh_db  # noqa: F401  (fresh_db is a fixture)


def test_AM1_downgrade_to_0003_removes_0004_and_restores_the_old_grants(fresh_db):
    cfg, name = _cfg(fresh_db), fresh_db
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0003")
    with psycopg.connect(dsn_for(name), autocommit=True) as c:
        q = lambda sql: c.execute(sql).fetchone()[0]  # noqa: E731
        assert q("SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                 "WHERE n.nspname='audit' AND p.proname IN ('hash_for','record_event')") == 0
        assert q("SELECT count(*) FROM pg_constraint WHERE conrelid='audit.event'::regclass "
                 "AND conname LIKE 'audit_event_%'") == 0
        assert q("SELECT has_table_privilege('app_rw','audit.event','INSERT')") is True
        assert q("SELECT has_schema_privilege('audit_writer','core','USAGE')") is False
        assert q("SELECT to_regprocedure('core.outbox_paused(core.marketplace,text)') IS NOT NULL") is True   # 0003 intact
    command.upgrade(cfg, "head")
    with psycopg.connect(dsn_for(name), autocommit=True) as c:
        assert c.execute("SELECT has_table_privilege('app_rw','audit.event','INSERT')").fetchone()[0] is False
        assert c.execute("SELECT has_schema_privilege('audit_writer','core','USAGE')").fetchone()[0] is True

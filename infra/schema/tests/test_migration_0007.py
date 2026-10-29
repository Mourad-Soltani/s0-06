"""Migration 0007 downgrade. Needs TEST_ADMIN_DSN. Names are AE*."""
import psycopg
from alembic import command

from conftest import dsn_for
from test_alembic_roundtrip import _cfg, fresh_db  # noqa: F401  (fresh_db is a fixture)


def test_AE1_downgrade_to_0006_removes_the_table_and_the_role_memberships(fresh_db):
    cfg, name = _cfg(fresh_db), fresh_db
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0006")
    with psycopg.connect(dsn_for(name), autocommit=True) as c:
        q = lambda sql: c.execute(sql).fetchone()[0]  # noqa: E731
        assert q("SELECT to_regclass('core.review_edit') IS NULL") is True
        assert q("SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid "
                 "JOIN pg_roles g ON g.oid = m.member WHERE g.rolname = 'review_svc'") == 0
        assert q("SELECT to_regclass('core.review_decision') IS NOT NULL") is True
    command.upgrade(cfg, "head")
    with psycopg.connect(dsn_for(name), autocommit=True) as c:
        assert c.execute("SELECT pg_has_role('review_svc', 'app_rw', 'MEMBER') AND pg_has_role('review_svc', 'pii_svc', 'MEMBER')").fetchone()[0] is True

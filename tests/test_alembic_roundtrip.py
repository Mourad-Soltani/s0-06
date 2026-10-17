"""Alembic apply / rollback: clean DB, and a DB one migration behind (generic over all revisions)."""
import pathlib
import uuid

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.engine import URL

from conftest import ADMIN, ROOT, create_db, drop_db, dsn_for


def _cfg(db_name: str) -> Config:
    # psycopg3 SQLAlchemy URL built from the admin DSN's parts
    info = psycopg.conninfo.conninfo_to_dict(dsn_for(db_name))
    url = URL.create(
        "postgresql+psycopg",
        username=info.get("user"), password=info.get("password"),
        host=info.get("host", "localhost"), port=int(info.get("port", 5432)), database=db_name,
    ).render_as_string(hide_password=False)
    c = Config(str(ROOT / "alembic.ini"))
    c.set_main_option("script_location", str(ROOT / "migrations"))
    # ConfigParser interpolation: a literal % (URL-encoded chars) must be doubled
    c.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return c


def _revisions():
    script = ScriptDirectory(str(ROOT / "migrations"))
    return [r.revision for r in reversed(list(script.walk_revisions()))]  # oldest first


def _schemas(db_name):
    with psycopg.connect(dsn_for(db_name), autocommit=True) as c:
        return {r[0] for r in c.execute(
            "SELECT nspname FROM pg_namespace WHERE nspname IN ('core','pii','audit')").fetchall()}


@pytest.fixture()
def fresh_db():
    if not ADMIN:
        pytest.skip("TEST_ADMIN_DSN not set")
    name = f"s006_alembic_{uuid.uuid4().hex[:8]}"
    create_db(name)
    yield name
    drop_db(name)


def test_clean_db_up_down_up(fresh_db):
    cfg = _cfg(fresh_db)
    command.upgrade(cfg, "head")
    assert _schemas(fresh_db) == {"core", "pii", "audit"}
    command.downgrade(cfg, "base")
    assert _schemas(fresh_db) == set()
    command.upgrade(cfg, "head")                       # re-apply after rollback
    assert _schemas(fresh_db) == {"core", "pii", "audit"}


@pytest.mark.parametrize("behind", range(len(_revisions())))
def test_db_one_migration_behind(fresh_db, behind):
    """For each revision N: bring DB to N-1 (or base), then upgrade to head, then roll all the way back."""
    cfg = _cfg(fresh_db)
    revs = _revisions()
    target = "base" if behind == 0 else revs[behind - 1]
    if target != "base":
        command.upgrade(cfg, target)
    command.upgrade(cfg, "head")
    assert _schemas(fresh_db) == {"core", "pii", "audit"}
    command.downgrade(cfg, "base")
    assert _schemas(fresh_db) == set()

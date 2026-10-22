import os
import pathlib
import uuid

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

ROOT = pathlib.Path(__file__).resolve().parents[1]
MIG_SQL_DIR = ROOT / "migrations" / "sql"


def schema_files():
    """Every NNNN_*.sql in order. For the CI mutation checks (infra/ci/scripts/mutation_check.py) a file can be
    replaced: SCHEMA_SQL replaces 0001, SCHEMA_SQL_0002 replaces 0002, SCHEMA_SQL_0003 replaces 0003.
    Never set these otherwise."""
    out = []
    for f in sorted(MIG_SQL_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql")):
        override = os.environ.get("SCHEMA_SQL_" + f.name[:4])
        if not override and f.name.startswith("0001_"):
            override = os.environ.get("SCHEMA_SQL")
        out.append(pathlib.Path(override) if override else f)
    return out
ADMIN = os.environ.get("TEST_ADMIN_DSN")  # superuser DSN, e.g. postgresql://postgres:pw@localhost:5432/postgres


def dsn_for(db: str) -> str:
    return make_conninfo(ADMIN, dbname=db)


def create_db(name: str, template: str | None = None):
    with psycopg.connect(ADMIN, autocommit=True) as a:
        q = sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name))
        if template:
            q += sql.SQL(" TEMPLATE {}").format(sql.Identifier(template))
        a.execute(q)


def drop_db(name: str):
    with psycopg.connect(ADMIN, autocommit=True) as a:
        a.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture(scope="session")
def base_db():
    if not ADMIN:
        pytest.skip("TEST_ADMIN_DSN not set")
    name = f"s006_base_{uuid.uuid4().hex[:8]}"
    create_db(name)
    with psycopg.connect(dsn_for(name), autocommit=True) as c:
        for f in schema_files():
            c.execute(f.read_text())
    yield name
    drop_db(name)


class DB:
    def __init__(self, name):
        self.name = name
        self.dsn = dsn_for(name)
        self.conn = psycopg.connect(self.dsn, autocommit=True)


@pytest.fixture()
def db(base_db):
    name = f"s006_t_{uuid.uuid4().hex[:8]}"
    create_db(name, template=base_db)
    d = DB(name)
    yield d
    d.conn.close()
    drop_db(name)

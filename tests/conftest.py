import os
import pathlib
import uuid

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

ROOT = pathlib.Path(__file__).resolve().parents[1]
# SCHEMA_SQL override exists for the CI mutation check (scripts/mutation_check.py); never set it otherwise
SQL_FILE = pathlib.Path(os.environ.get("SCHEMA_SQL") or ROOT / "migrations" / "sql" / "0001_schema_v0.sql")
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
        c.execute(SQL_FILE.read_text())
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

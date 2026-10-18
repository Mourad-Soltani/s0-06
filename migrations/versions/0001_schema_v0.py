"""schema v0: core / pii / audit

Revision ID: 0001
Revises:
"""
from pathlib import Path

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

SQL_FILE = Path(__file__).resolve().parent.parent / "sql" / "0001_schema_v0.sql"


def upgrade():
    # Raw DBAPI cursor, no params: psycopg3 skips placeholder parsing (the SQL contains
    # '%' in RAISE EXCEPTION) and allows multiple statements in one execute().
    cur = op.get_bind().connection.cursor()
    cur.execute(SQL_FILE.read_text())


def downgrade():
    # Roles are cluster-level and intentionally left in place.
    op.execute("DROP SCHEMA IF EXISTS audit, core, pii CASCADE")

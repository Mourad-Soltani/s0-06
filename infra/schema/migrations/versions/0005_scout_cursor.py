"""scout cursor (S2-03)

Revision ID: 0005
Revises: 0004
"""
from pathlib import Path

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

SQL_FILE = Path(__file__).resolve().parent.parent / "sql" / "0005_scout_cursor.sql"
DOWNGRADE_SQL = "DROP TABLE IF EXISTS core.scout_cursor;"   # in Python: conftest applies every NNNN_*.sql


def upgrade():
    cur = op.get_bind().connection.cursor()
    cur.execute(SQL_FILE.read_text())


def downgrade():
    cur = op.get_bind().connection.cursor()
    cur.execute(DOWNGRADE_SQL)

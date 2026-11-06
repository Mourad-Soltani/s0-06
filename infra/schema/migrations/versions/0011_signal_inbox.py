"""signal inbox (S4-01, S4-02)

Revision ID: 0011
Revises: 0010
"""
from pathlib import Path

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

SQL_FILE = Path(__file__).resolve().parent.parent / "sql" / "0011_signal_inbox.sql"
DOWNGRADE_SQL = "DROP TABLE IF EXISTS core.signal_inbox;"   # in Python: conftest applies every NNNN_*.sql


def upgrade():
    cur = op.get_bind().connection.cursor()
    cur.execute(SQL_FILE.read_text())


def downgrade():
    cur = op.get_bind().connection.cursor()
    cur.execute(DOWNGRADE_SQL)

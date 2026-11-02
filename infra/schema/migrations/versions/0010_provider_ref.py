"""provider reference and the stuck-send index (BL-04)

Revision ID: 0010
Revises: 0009
"""
from pathlib import Path

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

SQL_FILE = Path(__file__).resolve().parent.parent / "sql" / "0010_provider_ref.sql"
DOWNGRADE_SQL = """
DROP INDEX IF EXISTS core.action_proposal_sending_idx;
ALTER TABLE core.action_proposal DROP COLUMN IF EXISTS provider_ref;
"""   # in Python: conftest applies every NNNN_*.sql


def upgrade():
    cur = op.get_bind().connection.cursor()
    cur.execute(SQL_FILE.read_text())


def downgrade():
    cur = op.get_bind().connection.cursor()
    cur.execute(DOWNGRADE_SQL)

"""compliance findings (S3-02)

Revision ID: 0008
Revises: 0007
"""
from pathlib import Path

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

SQL_FILE = Path(__file__).resolve().parent.parent / "sql" / "0008_compliance_finding.sql"
DOWNGRADE_SQL = "DROP TABLE IF EXISTS core.compliance_finding;"   # in Python: conftest applies every NNNN_*.sql


def upgrade():
    cur = op.get_bind().connection.cursor()
    cur.execute(SQL_FILE.read_text())


def downgrade():
    cur = op.get_bind().connection.cursor()
    cur.execute(DOWNGRADE_SQL)

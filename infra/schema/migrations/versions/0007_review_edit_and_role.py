"""review edits and the review_svc role (S2-05)

Revision ID: 0007
Revises: 0006
"""
from pathlib import Path

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

SQL_FILE = Path(__file__).resolve().parent.parent / "sql" / "0007_review_edit_and_role.sql"
# In Python: conftest applies every NNNN_*.sql. The role itself is cluster-level and is left in place.
DOWNGRADE_SQL = """
DROP TABLE IF EXISTS core.review_edit;
REVOKE app_rw FROM review_svc;
REVOKE pii_svc FROM review_svc;
"""


def upgrade():
    cur = op.get_bind().connection.cursor()
    cur.execute(SQL_FILE.read_text())


def downgrade():
    cur = op.get_bind().connection.cursor()
    cur.execute(DOWNGRADE_SQL)

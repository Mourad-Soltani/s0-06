"""marketplace fees and routing indexes (S4-03)

Revision ID: 0012
Revises: 0011
"""
from pathlib import Path

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

SQL_FILE = Path(__file__).resolve().parent.parent / "sql" / "0012_listing_routing.sql"
DOWNGRADE_SQL = """
DROP INDEX IF EXISTS core.listing_ref_active_product_idx;
DROP INDEX IF EXISTS core.listing_ref_external_idx;
DROP TABLE IF EXISTS core.marketplace_fee;
"""   # in Python: conftest applies every NNNN_*.sql


def upgrade():
    cur = op.get_bind().connection.cursor()
    cur.execute(SQL_FILE.read_text())


def downgrade():
    cur = op.get_bind().connection.cursor()
    cur.execute(DOWNGRADE_SQL)
